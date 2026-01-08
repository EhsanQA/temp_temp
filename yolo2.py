#!/usr/bin/env python3
import os
import time
import subprocess
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gst, GLib

# -----------------------------
# USER CONFIG (edit these!)
# -----------------------------
CAM0_NAME = 'PUT_CAMERA0_NAME_HERE'
CAM1_NAME = 'PUT_CAMERA1_NAME_HERE'

# Use the SAME HEF + postprocess + draw libs your hailo-rpi5-examples YOLO uses.
HEF_PATH = '/home/pi/hailo-rpi5-examples/resources/PUT_MODEL.hef'
POSTPROCESS_SO = '/home/pi/hailo-rpi5-examples/resources/libPUT_POSTPROCESS.so'
DRAW_SO = '/home/pi/hailo-rpi5-examples/resources/libPUT_DRAW.so'
POSTPROCESS_FUNCTION = 'yolov5'  # must match the postprocess .so expectations

# Recording / preview settings
REC_WIDTH, REC_HEIGHT, REC_FPS = 1920, 1080, 10
PREV_WIDTH, PREV_HEIGHT, PREV_FPS = 1280, 720, 10

OUT_DIR = os.path.expanduser("~/Videos")
DIVIDER_GAP_PX = 6  # blank gap between the two previews

# -----------------------------

Gst.init(None)

def ts():
    return time.strftime("%Y%m%d_%H%M%S")

def remux_to_mp4(mkv_path: str) -> str:
    mp4_path = os.path.splitext(mkv_path)[0] + ".mp4"
    # Stream copy (fast), no re-encode
    subprocess.run(["ffmpeg", "-y", "-i", mkv_path, "-c", "copy", mp4_path],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return mp4_path

class App(Gtk.Window):
    def __init__(self):
        super().__init__(title="Dual Camera (Hailo YOLO)")

        self.set_default_size(1200, 700)
        self.connect("destroy", self.on_destroy)

        self.pipeline = None
        self.sink_widget = None
        self.recording = False
        self.last_mkv0 = None
        self.last_mkv1 = None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_border_width(8)
        self.add(root)

        self.video_box = Gtk.Box()
        root.pack_start(self.video_box, True, True, 0)

        btn_row = Gtk.Box(spacing=8)
        root.pack_start(btn_row, False, False, 0)

        self.btn_record = Gtk.Button(label="Record")
        self.btn_stop = Gtk.Button(label="Stop")
        self.btn_stop.set_sensitive(False)

        self.btn_record.connect("clicked", self.on_record)
        self.btn_stop.connect("clicked", self.on_stop)

        btn_row.pack_start(self.btn_record, False, False, 0)
        btn_row.pack_start(self.btn_stop, False, False, 0)

        # Start preview-only
        self.start_preview_pipeline()

    def build_pipeline(self, do_record: bool):
        # Output file paths (Matroska is robust; then we remux to MP4 on Stop)
        mkv0 = os.path.join(OUT_DIR, f"cam0_{ts()}.mkv") if do_record else None
        mkv1 = os.path.join(OUT_DIR, f"cam1_{ts()}.mkv") if do_record else None

        # Two branches:
        #  - record branch: full res -> HW encoder -> mkv
        #  - preview/detect branch: scaled -> hailonet -> postprocess -> draw -> compositor -> gtksink
        #
        # Hailo detection chain is per Hailo TAPPAS detection pipeline pattern: hailonet + hailofilter(post) + hailofilter(draw). 
        #
        # Camera selection via libcamerasrc camera-name="..." 

        # Note: Using v4l2h264enc is typically the Pi hardware encoder; matroskamux example exists in libcamera+gstreamer docs. 
        rec0 = ""
        rec1 = ""
        if do_record:
            rec0 = f"""
                cam0tee. ! queue !
                v4l2h264enc extra-controls="controls,repeat_sequence_header=1" !
                h264parse ! matroskamux !
                filesink location="{mkv0}" sync=false
            """
            rec1 = f"""
                cam1tee. ! queue !
                v4l2h264enc extra-controls="controls,repeat_sequence_header=1" !
                h264parse ! matroskamux !
                filesink location="{mkv1}" sync=false
            """

        pipeline_str = f"""
            compositor name=comp background=black
                sink_0::xpos=0 sink_0::ypos=0
                sink_1::xpos={PREV_WIDTH + DIVIDER_GAP_PX} sink_1::ypos=0
            ! videoconvert !
              fpsdisplaysink video-sink=gtksink name=display sync=false text-overlay=false

            libcamerasrc camera-name="{CAM0_NAME}" !
              video/x-raw,format=NV12,width={REC_WIDTH},height={REC_HEIGHT},framerate={REC_FPS}/1 !
              tee name=cam0tee
            cam0tee. ! queue ! videoconvert ! videoscale !
              video/x-raw,format=YUY2,width={PREV_WIDTH},height={PREV_HEIGHT},framerate={PREV_FPS}/1 !
              queue leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0 !
              hailonet hef-path="{HEF_PATH}" qos=false batch-size=1 !
              queue !
              hailofilter function-name="{POSTPROCESS_FUNCTION}" so-path="{POSTPROCESS_SO}" qos=false debug=false !
              queue !
              hailofilter so-path="{DRAW_SO}" qos=false debug=false !
              queue !
              videoconvert ! comp.sink_0
            {rec0}

            libcamerasrc camera-name="{CAM1_NAME}" !
              video/x-raw,format=NV12,width={REC_WIDTH},height={REC_HEIGHT},framerate={REC_FPS}/1 !
              tee name=cam1tee
            cam1tee. ! queue ! videoconvert ! videoscale !
              video/x-raw,format=YUY2,width={PREV_WIDTH},height={PREV_HEIGHT},framerate={PREV_FPS}/1 !
              queue leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0 !
              hailonet hef-path="{HEF_PATH}" qos=false batch-size=1 !
              queue !
              hailofilter function-name="{POSTPROCESS_FUNCTION}" so-path="{POSTPROCESS_SO}" qos=false debug=false !
              queue !
              hailofilter so-path="{DRAW_SO}" qos=false debug=false !
              queue !
              videoconvert ! comp.sink_1
            {rec1}
        """

        return pipeline_str, mkv0, mkv1

    def start_pipeline(self, do_record: bool):
        self.stop_pipeline()

        pipeline_str, mkv0, mkv1 = self.build_pipeline(do_record)
        self.pipeline = Gst.parse_launch(pipeline_str)

        # Embed gtksink widget into our single window
        display = self.pipeline.get_by_name("display")
        gtksink = display.get_property("video-sink")
        widget = gtksink.get_property("widget")

        # Swap widget in UI
        for child in self.video_box.get_children():
            self.video_box.remove(child)
        self.video_box.pack_start(widget, True, True, 0)
        self.video_box.show_all()

        # Keep filenames for remux
        self.last_mkv0 = mkv0
        self.last_mkv1 = mkv1

        # Start playing
        self.pipeline.set_state(Gst.State.PLAYING)

    def start_preview_pipeline(self):
        self.recording = False
        self.btn_record.set_sensitive(True)
        self.btn_stop.set_sensitive(False)
        self.start_pipeline(do_record=False)

    def start_record_pipeline(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        self.recording = True
        self.btn_record.set_sensitive(False)
        self.btn_stop.set_sensitive(True)
        self.start_pipeline(do_record=True)

    def stop_pipeline(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def on_record(self, _btn):
        self.start_record_pipeline()

    def on_stop(self, _btn):
        # Stop recording pipeline (finalizes MKV)
        self.stop_pipeline()

        # Remux to MP4 (fast, no re-encode)
        if self.last_mkv0 and os.path.exists(self.last_mkv0):
            mp4 = remux_to_mp4(self.last_mkv0)
            print(f"Saved: {mp4}")
        if self.last_mkv1 and os.path.exists(self.last_mkv1):
            mp4 = remux_to_mp4(self.last_mkv1)
            print(f"Saved: {mp4}")

        # Back to preview-only
        self.start_preview_pipeline()

    def on_destroy(self, *_):
        self.stop_pipeline()
        Gtk.main_quit()

if __name__ == "__main__":
    app = App()
    app.show_all()
    Gtk.main()
