#!/usr/bin/env python3
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

# Prevent the Hailo pipeline from opening its own video window.
# (We will show the frames inside our Tk window instead.)
os.environ.setdefault("HAILO_RPI_SINK", "fakesink")

import cv2
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from PIL import Image, ImageTk
except Exception as e:
    raise SystemExit(
        "Pillow ImageTk is missing. Install with:\n"
        "  sudo apt install -y python3-pil.imagetk\n"
        f"Original error: {e}"
    )

# Hailo apps infra imports (installed by the hailo-rpi5-examples environment)
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from hailo_apps_infra.detection_pipeline import GStreamerDetectionApp
from hailo_apps_infra.hailo_rpi_common import (
    get_caps_from_pad,
    get_numpy_from_buffer,
    app_callback_class,
)
import hailo


class UserData(app_callback_class):
    """Thread-safe storage for the latest annotated frame."""
    def __init__(self, conf_threshold: float = 0.30):
        super().__init__()
        self.use_frame = True
        self.conf_threshold = conf_threshold
        self._lock = threading.Lock()
        self._frame = None
        self._count = -1

    def set_frame(self, frame, count: int):
        with self._lock:
            self._frame = frame
            self._count = count

    def get_frame(self):
        with self._lock:
            return self._frame, self._count


def hailo_callback(pad, info, user_data: UserData):
    buffer = info.get_buffer()
    if buffer is None:
        return Gst.PadProbeReturn.OK

    # Increment internal frame counter (provided by app_callback_class)
    user_data.increment()
    count = user_data.get_count()

    fmt, width, height = get_caps_from_pad(pad)

    frame_bgr = None
    if user_data.use_frame and fmt is not None and width is not None and height is not None:
        frame = get_numpy_from_buffer(buffer, fmt, width, height)  # typically RGB
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    # Get detections from metadata
    roi = hailo.get_roi_from_buffer(buffer)
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)

    # Draw boxes on the frame we will preview/record
    if frame_bgr is not None and width and height:
        for det in detections:
            conf = float(det.get_confidence())
            if conf < user_data.conf_threshold:
                continue

            label = det.get_label()
            bbox = det.get_bbox()  # normalized coords

            x1 = int(max(0.0, bbox.xmin()) * width)
            y1 = int(max(0.0, bbox.ymin()) * height)
            x2 = int(min(1.0, bbox.xmin() + bbox.width()) * width)
            y2 = int(min(1.0, bbox.ymin() + bbox.height()) * height)

            cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), (0, 255, 0), 2)
            text = f"{label} {conf:.2f}"
            y_text = max(0, y1 - 7)
            cv2.putText(frame_bgr, text, (x1, y_text),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)

        user_data.set_frame(frame_bgr, count)

    return Gst.PadProbeReturn.OK


class AppGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("YOLO Preview + Recorder (Hailo NPU)")
        self.root.geometry("900x650")

        self.user_data = UserData(conf_threshold=0.30)

        # Video UI
        self.video_label = ttk.Label(root)
        self.video_label.pack(fill="both", expand=True, padx=8, pady=8)

        # Controls
        controls = ttk.Frame(root)
        controls.pack(fill="x", padx=8, pady=(0, 8))

        self.status_var = tk.StringVar(value="Status: starting…")
        ttk.Label(controls, textvariable=self.status_var).pack(side="left")

        self.record_btn = ttk.Button(controls, text="Record", command=self.start_recording)
        self.record_btn.pack(side="right", padx=(6, 0))

        self.stop_btn = ttk.Button(controls, text="Stop", command=self.stop_recording, state="disabled")
        self.stop_btn.pack(side="right")

        # Recording state
        self.recording = False
        self.writer = None
        self.last_written_count = -1
        self.last_display_count = -1
        self.output_path = None

        # Start Hailo pipeline in background thread
        self._start_hailo_thread()

        # Periodic UI update
        self._update_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _start_hailo_thread(self):
        def run_pipeline():
            try:
                Gst.init(None)

                # Force camera input by default (same idea as running: detection.py --input rpi)
                sys.argv = [sys.argv[0], "--input", "rpi"]

                app = GStreamerDetectionApp(hailo_callback, self.user_data)
                app.run()  # blocking
            except Exception as e:
                # If pipeline fails, surface it in the UI
                self.status_var.set(f"Status: Hailo pipeline error: {e}")

        t = threading.Thread(target=run_pipeline, daemon=True)
        t.start()

    def _update_ui(self):
        frame, count = self.user_data.get_frame()

        if frame is not None and count != self.last_display_count:
            self.last_display_count = count

            # Show frame in Tk
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            imgtk = ImageTk.PhotoImage(image=img)
            self.video_label.imgtk = imgtk  # keep reference
            self.video_label.configure(image=imgtk)

            # Write exactly one frame per pipeline frame while recording
            if self.recording and self.writer is not None and count != self.last_written_count:
                self.last_written_count = count
                self.writer.write(frame)

        # Update status
        if self.recording and self.output_path:
            self.status_var.set(f"Status: RECORDING → {self.output_path.name}")
        else:
            self.status_var.set("Status: preview running")

        self.root.after(20, self._update_ui)

    def start_recording(self):
        if self.recording:
            return

        frame, _ = self.user_data.get_frame()
        if frame is None:
            messagebox.showwarning("Not ready", "No camera frame yet. Wait a second and press Record again.")
            return

        h, w = frame.shape[:2]
        out_dir = Path.home() / "Videos" / "hailo_recordings"
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_path = out_dir / f"record_{ts}.mp4"

        fps = 10  # your requirement
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(str(self.output_path), fourcc, fps, (w, h))
        if not self.writer.isOpened():
            self.writer = None
            messagebox.showerror("VideoWriter error", "Could not open MP4 writer. Try installing codecs or use .avi.")
            return

        self.recording = True
        self.last_written_count = -1
        self.record_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

    def stop_recording(self):
        if not self.recording:
            return

        self.recording = False
        self.record_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

        if self.writer is not None:
            self.writer.release()
            self.writer = None

        if self.output_path:
            messagebox.showinfo("Saved", f"Saved:\n{self.output_path}")

    def on_close(self):
        try:
            if self.writer is not None:
                self.writer.release()
        finally:
            self.root.destroy()


def main():
    root = tk.Tk()
    # Use ttk default theme
    try:
        style = ttk.Style()
        style.theme_use("clam")
    except Exception:
        pass
    AppGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
