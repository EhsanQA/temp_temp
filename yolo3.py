#!/usr/bin/env python3
import os
import time
import threading
import subprocess
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gst, GLib

import hailo

Gst.init(None)

# -----------------------
# USER SETTINGS
# -----------------------
# IMPORTANT: set these to what `libcamera-hello --list-cameras` shows.
# libcamerasrc uses the *camera-name* string (often a long path).
CAM0_NAME = "/base/axi/pcie@1000120000/rp1/i2c@88000/imx708@1a"
CAM1_NAME = "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a"

PREVIEW_W, PREVIEW_H = 1280, 720
PREVIEW_FPS = 10

OUT_DIR = Path.home() / "Videos"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Cropper library used by Hailo examples
CROP_SO = "/usr/lib/aarch64-linux-gnu/hailo/tappas/post_processes/cropping_algorithms/libwhole_buffer.so"


def _find_hailo_resources():
    """
    Find HEF + postprocess .so from hailo_apps_infra installed resources,
    similar to what hailo-rpi5-examples uses internally.
    """
    try:
        import hailo_apps_infra  # installed in the venv by hailo-rpi5-examples
    except ImportError as e:
        raise RuntimeError(
            "Cannot import hailo_apps_infra. Make sure you activated the "
            "hailo-rpi5-examples venv and ran source setup-env.sh"
        ) from e

    res_dir = (Path(hailo_apps_infra.__file__).resolve().parent / ".." / "resources").resolve()

    # Prefer H8L HEFs if present; fallback to anything YOLO-ish.
    hef_candidates = [
        res_dir / "yolov8s_h8l.hef",
        res_dir / "yolov8m_h8l.hef",
        res_dir / "yolov8n_h8l.hef",
    ]
    hef_path = next((p for p in hef_candidates if p.exists()), None)
    if hef_path is None:
        # fallback: any .hef in resources
        hefs = sorted(res_dir.glob("*.hef"))
        if not hefs:
            raise RuntimeError(f"No .hef files found in: {res_dir}")
        hef_path = hefs[0]

    post_so = res_dir / "libyolo_hailortpp_postprocess.so"
    if not post_so.exists():
        raise RuntimeError(f"Postprocess .so not found: {post_so}")

    return str(hef_path), str(post_so)


HEF_PATH, POSTPROCESS_SO = _find_hailo_resources()

# Postprocess function name used by the examples pipeline
POSTPROCESS_FN = "filter_letterbox"


def ts():
    return time.strftime("%Y%m%d_%H%M%S")


class CamRunner:
    """
    One camera pipeline:
      libcamerasrc -> (Hailo detection pipeline like examples) -> hailooverlay -> tee
         tee -> gtksink (preview)
         tee -> (optional) record bin (mkv)
    """
    def __init__(self, cam_name: str, idx: int):
        self.cam_name = cam_name
        self.idx = idx

        self.pipeline = None
        self.gtksink = None
        self.identity = None

        self._tee = None
        self._tee_pad = None
        self._rec_bin = None

        self._recording = False
        self._last_mkv = None

        self._frame_count = 0

    def build(self):
        # This mirrors the structure printed by hailo-rpi5-examples (cropper+aggregator+hailonet+hailofilter+overlay+identity)
        # See the example-generated pipeline string for these exact elements/params. 
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
                    nms-score-threshold=0.3
                    nms-iou-threshold=0.45
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
                queue name=overlay_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                hailooverlay name=hailo_overlay_{self.idx} !
                queue name=display_convert_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                videoconvert n-threads=2 qos=false !
                tee name=tee_{self.idx}

            tee_{self.idx}. !
                queue name=preview_q_{self.idx} leaky=no max-size-buffers=3 max-size-bytes=0 max-size-time=0 !
                gtksink name=gtksink_{self.idx} sync=true
        """

        self.pipeline = Gst.parse_launch(pipe)
        self.gtksink = self.pipeline.get_by_name(f"gtksink_{self.idx}")
        self.identity = self.pipeline.get_by_name(f"identity_callback_{self.idx}")
        self._tee = self.pipeline.get_by_name(f"tee_{self.idx}")

        if not self.gtksink or not self.identity or not self._tee:
            raise RuntimeError("Failed to build pipeline elements (gtksink/identity/tee missing).")

        # Attach probe like the examples: read detections from buffer ROI
        srcpad = self.identity.get_static_pad("src")
        srcpad.add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)

    def widget(self) -> Gtk.Widget:
        return self.gtksink.get_property("widget")

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    def _on_buffer(self, pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK

        self._frame_count += 1

        # Read detections exactly like detection.py / detection_simple.py
        roi = hailo.get_roi_from_buffer(buf)
        detections = roi.get_objects_typed(hailo.HAILO_DETECTION)

        # Keep printing short (you can expand)
        lines = []
        for det in detections:
            lines.append(f"{det.get_label()} {det.get_confidence():.2f}")
        if lines:
            print(f"[CAM{self.idx}] frame={self._frame_count} :: " + ", ".join(lines[:8]))

        return Gst.PadProbeReturn.OK

    def start_recording(self):
        if self._recording:
            return

        mkv_path = OUT_DIR / f"cam{self.idx}_{ts()}.mkv"
        self._last_mkv = str(mkv_path)

        rec_bin = Gst.Bin.new(f"record_bin_{self.idx}")

        q = Gst.ElementFactory.make("queue", None)
        conv = Gst.ElementFactory.make("videoconvert", None)

        enc = Gst.ElementFactory.make("v4l2h264enc", None)
        if enc is None:
            # fallback (CPU)
            enc = Gst.ElementFactory.make("x264enc", None)
            enc.set_property("tune", "zerolatency")

        parse = Gst.ElementFactory.make("h264parse", None)
        if parse:
            parse.set_property("config-interval", 1)

        mux = Gst.ElementFactory.make("matroskamux", None)
        sink = Gst.ElementFactory.make("filesink", None)
        sink.set_property("location", self._last_mkv)

        for e in (q, conv, enc, parse, mux, sink):
            if e is None:
                raise RuntimeError("Failed to create a recording element (encoder/mux/sink missing).")
            rec_bin.add(e)

        Gst.Element.link_many(q, conv, enc, parse, mux, sink)

        # Ghost sink pad so we can link tee -> record_bin
        sinkpad = q.get_static_pad("sink")
        ghost = Gst.GhostPad.new("sink", sinkpad)
        rec_bin.add_pad(ghost)

        self.pipeline.add(rec_bin)
        rec_bin.sync_state_with_parent()

        tee_pad = self._tee.get_request_pad("src_%u")
        if tee_pad is None:
            raise RuntimeError("Failed to request tee src pad for recording.")

        if tee_pad.link(rec_bin.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            self._tee.release_request_pad(tee_pad)
            self.pipeline.remove(rec_bin)
            raise RuntimeError("Failed to link tee to record bin.")

        self._tee_pad = tee_pad
        self._rec_bin = rec_bin
        self._recording = True
        print(f"[CAM{self.idx}] recording -> {self._last_mkv}")

    def stop_recording(self):
        if not self._recording:
            return None

        # Unlink and remove record bin (keeps preview running)
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
            # stream-copy (fast), no re-encode
            cmd = ["ffmpeg", "-y", "-i", mkv, "-c", "copy", "-movflags", "+faststart", mp4]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                os.remove(mkv)
            except OSError:
                pass
            print(f"[REMUX] {mp4}")
    threading.Thread(target=worker, daemon=True).start()


class App(Gtk.Window):
    def __init__(self):
        super().__init__(title="Dual Camera (Hailo YOLO)")

        self.set_default_size(1400, 650)
        self.connect("destroy", Gtk.main_quit)

        self.cam0 = CamRunner(CAM0_NAME, 0)
        self.cam1 = CamRunner(CAM1_NAME, 1)

        self.cam0.build()
        self.cam1.build()

        # UI
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

        # Start pipelines
        self.cam0.start()
        self.cam1.start()

    def on_record(self, _btn):
        self.record_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.status.set_text("Recording...")

        # Start both together
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
    win = App()
    win.show_all()
    Gtk.main()
