#!/usr/bin/env python3
import os
import time
import threading
import subprocess
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gst

import hailo

# cairooverlay uses pycairo
import cairo

Gst.init(None)

# -----------------------
# USER SETTINGS
# -----------------------
CAM0_NAME = "/base/axi/pcie@1000120000/rp1/i2c@88000/imx708@1a"
CAM1_NAME = "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a"

PREVIEW_W, PREVIEW_H = 1280, 720
PREVIEW_FPS = 10

OUT_DIR = Path.home() / "Videos"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Cropper library used by Hailo examples
CROP_SO = "/usr/lib/aarch64-linux-gnu/hailo/tappas/post_processes/cropping_algorithms/libwhole_buffer.so"

# YOLO postprocess
POSTPROCESS_SO = "/usr/local/hailo/resources/so/libyolo_hailortpp_postprocess.so"
POSTPROCESS_FN = "filter_letterbox"


def ts():
    return time.strftime("%Y%m%d_%H%M%S")


def make(name: str) -> Gst.Element:
    e = Gst.ElementFactory.make(name, None)
    if e is None:
        raise RuntimeError(
            f"Missing GStreamer element '{name}'. Install plugins:\n"
            f"  sudo apt install -y gstreamer1.0-plugins-good "
            f"gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav"
        )
    return e


def link_chain(*elems):
    for a, b in zip(elems, elems[1:]):
        if not a.link(b):
            raise RuntimeError(f"Failed to link {a.get_name()} -> {b.get_name()}")


def pick_detection_hef() -> str:
    """
    Pick a YOLO *detection* HEF (avoid pose/seg).
    """
    base = Path("/usr/local/hailo/resources/models/hailo8l")
    if not base.exists():
        raise RuntimeError(f"HEF directory not found: {base}")

    hefs = sorted(base.glob("*.hef"))
    if not hefs:
        raise RuntimeError(f"No .hef files found in: {base}")

    preferred = [
        "yolov8s_h8l.hef",
        "yolov8n_h8l.hef",
        "yolov8m_h8l.hef",
        "yolov6n.hef",
        "yolov6s.hef",
        "yolov8s.hef",
        "yolov8n.hef",
    ]
    for n in preferred:
        p = base / n
        if p.exists():
            return str(p)

    for p in hefs:
        name = p.name.lower()
        if "yolo" in name and ("pose" not in name) and ("seg" not in name) and ("depth" not in name):
            return str(p)

    return str(hefs[0])


HEF_PATH = pick_detection_hef()


def _bbox_get(bbox, name: str):
    """
    Supports bbox.xmin() or bbox.xmin attribute style.
    """
    if hasattr(bbox, name):
        v = getattr(bbox, name)
        return v() if callable(v) else v
    alt = {"xmin": "x_min", "ymin": "y_min", "width": "w", "height": "h"}.get(name)
    if alt and hasattr(bbox, alt):
        v = getattr(bbox, alt)
        return v() if callable(v) else v
    return None


class CamRunner:
    """
    One camera pipeline:
      libcamerasrc -> hailo pipeline -> identity(probe reads detections) -> videoconvert -> BGRA -> cairooverlay(draw boxes) -> tee
         tee -> gtksink (preview)
         tee -> record bin (mkv)
    """
    def __init__(self, cam_name: str, idx: int):
        self.cam_name = cam_name
        self.idx = idx

        self.pipeline = None
        self.gtksink = None
        self.identity = None
        self.cairooverlay = None
        self._tee = None

        self._tee_pad = None
        self._rec_bin = None
        self._recording = False
        self._last_mkv = None

        self._frame_count = 0

        # store latest boxes in pixel coords: [(x0,y0,x1,y1), ...]
        self._boxes = []
        self._lock = threading.Lock()

    def build(self):
        pipe = f"""
            libcamerasrc camera-name="{self.cam_name}" name=source_{self.idx} !
            video/x-raw,format=NV12,width={PREVIEW_W},height={PREVIEW_H},framerate={PREVIEW_FPS}/1 !
            queue name=source_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
            videoconvert n-threads=2 qos=false !
            video/x-raw, pixel-aspect-ratio=1/1, format=RGB, width={PREVIEW_W}, height={PREVIEW_H} !
            queue name=inference_wrapper_input_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
            hailocropper name=inference_wrapper_crop_{self.idx}
                so-path="{CROP_SO}" function-name=create_crops
                use-letterbox=true resize-method=inter-area internal-offset=true
            hailoaggregator name=inference_wrapper_agg_{self.idx}

            inference_wrapper_crop_{self.idx}. !
                queue name=inference_wrapper_bypass_q_{self.idx} leaky=no max-size-buffers=20 max-size-bytes=0 max-size-time=0 !
                inference_wrapper_agg_{self.idx}.sink_0

            inference_wrapper_crop_{self.idx}. !
                queue name=inference_scale_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                videoscale n-threads=2 qos=false !
                queue name=inference_convert_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                videoconvert n-threads=2 qos=false !
                queue name=inference_hailonet_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                hailonet name=inference_hailonet_{self.idx}
                    hef-path="{HEF_PATH}"
                    batch-size=2
                    vdevice-group-id=1
                    output-format-type=HAILO_FORMAT_TYPE_FLOAT32
                    force-writable=true !
                queue name=inference_hailofilter_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                hailofilter name=inference_hailofilter_{self.idx}
                    so-path="{POSTPROCESS_SO}"
                    function-name={POSTPROCESS_FN}
                    qos=false !
                queue name=inference_output_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                inference_wrapper_agg_{self.idx}.sink_1

            inference_wrapper_agg_{self.idx}. !
                queue name=postagg_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                identity name=identity_callback_{self.idx} !
                videoconvert n-threads=2 qos=false !
                video/x-raw,format=BGRA !
                cairooverlay name=cairo_{self.idx} !
                tee name=tee_{self.idx}

            tee_{self.idx}. !
                queue name=preview_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                gtksink name=gtksink_{self.idx} sync=true
        """

        self.pipeline = Gst.parse_launch(pipe)
        self.gtksink = self.pipeline.get_by_name(f"gtksink_{self.idx}")
        self.identity = self.pipeline.get_by_name(f"identity_callback_{self.idx}")
        self.cairooverlay = self.pipeline.get_by_name(f"cairo_{self.idx}")
        self._tee = self.pipeline.get_by_name(f"tee_{self.idx}")

        if not self.gtksink or not self.identity or not self.cairooverlay or not self._tee:
            raise RuntimeError("Failed to build pipeline elements (gtksink/identity/cairooverlay/tee missing).")

        # Probe reads detections (no writing)
        srcpad = self.identity.get_static_pad("src")
        srcpad.add_probe(Gst.PadProbeType.BUFFER, self._on_buffer_read_dets)

        # Cairo overlay draws boxes (no text)
        self.cairooverlay.connect("draw", self._on_cairo_draw)

    def widget(self) -> Gtk.Widget:
        return self.gtksink.get_property("widget")

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    def _on_buffer_read_dets(self, pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK

        self._frame_count += 1

        roi = hailo.get_roi_from_buffer(buf)
        detections = roi.get_objects_typed(hailo.HAILO_DETECTION)

        boxes = []
        for det in detections:
            bbox = det.get_bbox()
            xmin = _bbox_get(bbox, "xmin")
            ymin = _bbox_get(bbox, "ymin")
            bw = _bbox_get(bbox, "width")
            bh = _bbox_get(bbox, "height")
            if xmin is None or ymin is None or bw is None or bh is None:
                continue

            x0 = int(max(0, min(PREVIEW_W - 1, xmin * PREVIEW_W)))
            y0 = int(max(0, min(PREVIEW_H - 1, ymin * PREVIEW_H)))
            x1 = int(max(0, min(PREVIEW_W - 1, (xmin + bw) * PREVIEW_W)))
            y1 = int(max(0, min(PREVIEW_H - 1, (ymin + bh) * PREVIEW_H)))
            boxes.append((x0, y0, x1, y1))

        with self._lock:
            self._boxes = boxes

        # Optional small debug
        if self._frame_count % 30 == 0:
            print(f"[CAM{self.idx}] frame={self._frame_count} dets={len(boxes)}")

        return Gst.PadProbeReturn.OK

    def _on_cairo_draw(self, overlay, context: cairo.Context, timestamp, duration):
        # Draw most recent boxes
        with self._lock:
            boxes = list(self._boxes)

        # Green boxes, no text
        context.set_source_rgba(0.0, 1.0, 0.0, 1.0)
        context.set_line_width(2.0)

        for (x0, y0, x1, y1) in boxes:
            w = max(1, x1 - x0)
            h = max(1, y1 - y0)
            context.rectangle(x0, y0, w, h)

        context.stroke()

    def start_recording(self):
        if self._recording:
            return

        mkv_path = OUT_DIR / f"cam{self.idx}_{ts()}.mkv"
        self._last_mkv = str(mkv_path)

        rec_bin = Gst.Bin.new(f"record_bin_{self.idx}")

        q = make("queue")
        conv = make("videoconvert")

        caps = make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420"))

        # Software encoder only (avoid /dev/video9)
        enc = Gst.ElementFactory.make("x264enc", None)
        enc_name = None
        if enc is not None:
            enc_name = "x264enc"
            enc.set_property("tune", "zerolatency")
            enc.set_property("speed-preset", "veryfast")
            enc.set_property("bitrate", 8000)  # kbps
            enc.set_property("key-int-max", 30)
        else:
            enc = Gst.ElementFactory.make("avenc_h264", None)
            if enc is not None:
                enc_name = "avenc_h264"

        if enc is None:
            raise RuntimeError(
                "No software H.264 encoder found (x264enc/avenc_h264).\n"
                "Install:\n"
                "  sudo apt install -y gstreamer1.0-plugins-ugly gstreamer1.0-libav"
            )

        parse = make("h264parse")
        parse.set_property("config-interval", 1)

        mux = make("matroskamux")
        sink = make("filesink")
        sink.set_property("location", self._last_mkv)
        sink.set_property("sync", False)

        for e in (q, conv, caps, enc, parse, mux, sink):
            rec_bin.add(e)

        link_chain(q, conv, caps, enc, parse, mux, sink)

        ghost = Gst.GhostPad.new("sink", q.get_static_pad("sink"))
        rec_bin.add_pad(ghost)

        self.pipeline.add(rec_bin)
        rec_bin.sync_state_with_parent()

        tee_pad = self._tee.get_request_pad("src_%u")
        if tee_pad is None:
            self.pipeline.remove(rec_bin)
            raise RuntimeError("Failed to request tee src pad for recording.")

        if tee_pad.link(rec_bin.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            self._tee.release_request_pad(tee_pad)
            self.pipeline.remove(rec_bin)
            raise RuntimeError("Failed to link tee to record bin.")

        self._tee_pad = tee_pad
        self._rec_bin = rec_bin
        self._recording = True

        print(f"[CAM{self.idx}] recording -> {self._last_mkv} (encoder={enc_name})")

    def stop_recording(self):
        if not self._recording:
            return None

        try:
            self._tee_pad.unlink(self._rec_bin.get_static_pad("sink"))
        except Exception:
            pass

        try:
            self._tee.release_request_pad(self._tee_pad)
        except Exception:
            pass

        self._rec_bin.set_state(Gst.State.NULL)
        try:
            self.pipeline.remove(self._rec_bin)
        except Exception:
            pass

        mkv = self._last_mkv
        self._tee_pad = None
        self._rec_bin = None
        self._recording = False
        self._last_mkv = None

        print(f"[CAM{self.idx}] stopped (mkv) -> {mkv}")
        return mkv


def remux_to_mp4_async(mkv_paths):
    def worker():
        for mkv in mkv_paths:
            if mkv is None:
                continue
            mp4 = str(Path(mkv).with_suffix(".mp4"))
            cmd = ["ffmpeg", "-y", "-i", mkv, "-c", "copy", "-movflags", "+faststart", mp4]
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if p.returncode != 0:
                print(f"[REMUX ERROR] ffmpeg failed for {mkv}:\n{p.stderr}")
                continue
            try:
                os.remove(mkv)
            except OSError:
                pass
            print(f"[REMUX] {mp4}")
    threading.Thread(target=worker, daemon=True).start()


class App(Gtk.Window):
    def __init__(self):
        super().__init__(title="Dual Camera (Hailo YOLO) - Boxes Only")

        self.set_default_size(1400, 650)
        self.connect("destroy", Gtk.main_quit)

        self.cam0 = CamRunner(CAM0_NAME, 0)
        self.cam1 = CamRunner(CAM1_NAME, 1)

        self.cam0.build()
        self.cam1.build()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_border_width(8)
        self.add(root)

        videos = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.pack_start(videos, True, True, 0)

        videos.pack_start(self.cam0.widget(), True, True, 0)
        videos.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        videos.pack_start(self.cam1.widget(), True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.pack_start(btn_row, False, False, 0)

        self.record_btn = Gtk.Button(label="Record")
        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.set_sensitive(False)

        self.record_btn.connect("clicked", self.on_record)
        self.stop_btn.connect("clicked", self.on_stop)

        btn_row.pack_start(self.record_btn, False, False, 0)
        btn_row.pack_start(self.stop_btn, False, False, 0)

        self.status = Gtk.Label(label=f"HEF: {Path(HEF_PATH).name}")
        btn_row.pack_start(self.status, True, True, 0)

        self.cam0.start()
        self.cam1.start()

    def on_record(self, _btn):
        self.record_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.status.set_text("Recording...")

        self.cam0.start_recording()
        self.cam1.start_recording()

    def on_stop(self, _btn):
        self.stop_btn.set_sensitive(False)
        self.record_btn.set_sensitive(True)
        self.status.set_text("Stopping... (remuxing to mp4)")

        mkv0 = self.cam0.stop_recording()
        mkv1 = self.cam1.stop_recording()
        remux_to_mp4_async([mkv0, mkv1])

        self.status.set_text("Preview running. Videos saved in ~/Videos (mp4).")


if __name__ == "__main__":
    print("Using:")
    print("  HEF:", HEF_PATH)
    print("  Postprocess SO:", POSTPROCESS_SO)
    print("  Postprocess FN:", POSTPROCESS_FN)
    print("  (Inference runs on Hailo via hailonet + .hef)")

    win = App()
    win.show_all()
    Gtk.main()
