#!/usr/bin/env python3
import threading
import queue
import time
from datetime import datetime

import cv2
import numpy as np
import tkinter as tk
from PIL import Image, ImageTk

from picamera2 import Picamera2, MappedArray
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

from ultralytics import YOLO


# -------------------- TUNABLE SETTINGS --------------------
FPS = 10

# Recording resolution (what gets saved)
RECORD_SIZE = (1280, 720)     # Try (1920, 1080) only if your Pi keeps up

# Inference resolution (smaller = faster)
INFER_SIZE = (640, 360)

# YOLO model (smallest models are best on CPU)
MODEL_PATH = "yolo11n.pt"     # or "yolov8n.pt"

CONF_THRES = 0.25
IOU_THRES = 0.45

H264_BITRATE = 8_000_000      # 8 Mbps for 720p@10fps; adjust as desired
# ----------------------------------------------------------


class CameraWorker(threading.Thread):
    def __init__(self, frame_queue: queue.Queue, status_queue: queue.Queue):
        super().__init__(daemon=True)
        self.frame_queue = frame_queue
        self.status_queue = status_queue

        self.cmd_queue = queue.Queue()
        self.stop_flag = threading.Event()

        self.picam2 = None
        self.model = None

        self.recording = False
        self.encoder = None
        self.output = None

        # Latest detections in INFER_SIZE coordinates:
        # list of (x1, y1, x2, y2, conf, cls_id)
        self.det_lock = threading.Lock()
        self.dets = []

        self.main_w = None
        self.main_h = None
        self.lores_w = None
        self.lores_h = None

    def send_cmd(self, cmd: str):
        """cmd in {'record','stop','quit'}"""
        self.cmd_queue.put(cmd)

    def _set_status(self, msg: str):
        try:
            self.status_queue.put_nowait(msg)
        except queue.Full:
            pass

    def _put_latest_frame(self, rgb_frame: np.ndarray):
        """Keep only the latest frame (drop older)."""
        try:
            while True:
                self.frame_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.frame_queue.put_nowait(rgb_frame)
        except queue.Full:
            pass

    def _draw_boxes_on_recording(self, request):
        """
        Picamera2 callback: draw on the 'main' stream buffer so the *recorded video* contains the boxes.
        Keep this extremely light (drawing only).  (Picamera2 warns against heavy work in callbacks.)
        """
        with self.det_lock:
            dets = list(self.dets)

        if not dets:
            return

        xscale = self.main_w / self.lores_w
        yscale = self.main_h / self.lores_h

        with MappedArray(request, "main") as m:
            # m.array is typically 4-channel (XRGB8888-like). OpenCV is happy drawing on it.
            for (x1, y1, x2, y2, conf, cls_id) in dets:
                X1 = int(x1 * xscale)
                Y1 = int(y1 * yscale)
                X2 = int(x2 * xscale)
                Y2 = int(y2 * yscale)

                name = self.model.names[int(cls_id)] if self.model and hasattr(self.model, "names") else str(int(cls_id))
                label = f"{name} {conf:.2f}"

                cv2.rectangle(m.array, (X1, Y1), (X2, Y2), (0, 255, 0, 0), 2)
                cv2.putText(
                    m.array,
                    label,
                    (X1, max(25, Y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 0, 0),
                    2,
                    cv2.LINE_AA,
                )

    def _start_recording(self):
        if self.recording:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"record_{ts}.mp4"

        self.encoder = H264Encoder(bitrate=H264_BITRATE)
        self.output = FfmpegOutput(filename)

        # Record the 'main' stream (which we annotate in the callback)
        self.picam2.start_recording(self.encoder, self.output)
        self.recording = True
        self._set_status(f"Recording → {filename}")

    def _stop_recording(self):
        if not self.recording:
            return
        self.picam2.stop_recording()
        self.recording = False
        self.encoder = None
        self.output = None
        self._set_status("Stopped (saved MP4).")

    def _shutdown(self):
        try:
            if self.recording:
                self._stop_recording()
        except Exception:
            pass
        try:
            if self.picam2:
                self.picam2.stop()
        except Exception:
            pass

    def run(self):
        # Load YOLO
        self._set_status("Loading YOLO model...")
        self.model = YOLO(MODEL_PATH)
        self._set_status(f"Loaded model: {MODEL_PATH}")

        # Camera init
        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": RECORD_SIZE, "format": "XRGB8888"},
            lores={"size": INFER_SIZE, "format": "XRGB8888"},
            controls={"FrameRate": FPS},
        )
        self.picam2.configure(config)

        (self.main_w, self.main_h) = self.picam2.stream_configuration("main")["size"]
        (self.lores_w, self.lores_h) = self.picam2.stream_configuration("lores")["size"]

        # Draw onto main stream so recording includes overlays
        self.picam2.post_callback = self._draw_boxes_on_recording
        self.picam2.start()
        self._set_status("Camera started.")

        try:
            while not self.stop_flag.is_set():
                # Handle any pending commands
                try:
                    while True:
                        cmd = self.cmd_queue.get_nowait()
                        if cmd == "record":
                            self._start_recording()
                        elif cmd == "stop":
                            self._stop_recording()
                        elif cmd == "quit":
                            self.stop_flag.set()
                            break
                except queue.Empty:
                    pass

                if self.stop_flag.is_set():
                    break

                # Grab lores frame for inference + preview
                lores = self.picam2.capture_array("lores")
                if lores.ndim == 3 and lores.shape[2] == 4:
                    frame_bgr = lores[:, :, :3]
                else:
                    frame_bgr = lores

                # YOLO inference
                results = self.model(
                    frame_bgr,
                    imgsz=640,
                    conf=CONF_THRES,
                    iou=IOU_THRES,
                    verbose=False,
                )
                r = results[0]

                # Store detections for recording overlay (in lores coords)
                dets = []
                if r.boxes is not None and len(r.boxes) > 0:
                    xyxy = r.boxes.xyxy.cpu().numpy()
                    confs = r.boxes.conf.cpu().numpy()
                    clss = r.boxes.cls.cpu().numpy().astype(int)
                    for (x1, y1, x2, y2), c, cl in zip(xyxy, confs, clss):
                        dets.append((float(x1), float(y1), float(x2), float(y2), float(c), int(cl)))

                with self.det_lock:
                    self.dets = dets

                # Preview frame with boxes (Ultralytics helper)
                annotated_bgr = r.plot()
                annotated_rgb = cv2.cvtColor(annotated_bgr, cv2.COLOR_BGR2RGB)
                self._put_latest_frame(annotated_rgb)

                # If you want to reduce CPU load, uncomment:
                # time.sleep(0.001)

        finally:
            self._shutdown()
            self._set_status("Camera stopped.")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Pi Camera + YOLO Recorder")

        self.frame_queue = queue.Queue(maxsize=1)
        self.status_queue = queue.Queue(maxsize=10)

        self.video_label = tk.Label(root)
        self.video_label.pack(padx=8, pady=8)

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=6)

        self.record_btn = tk.Button(btn_frame, text="Record", width=12, command=self.on_record)
        self.record_btn.grid(row=0, column=0, padx=6)

        self.stop_btn = tk.Button(btn_frame, text="Stop", width=12, command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=1, padx=6)

        self.status_var = tk.StringVar(value="Starting...")
        self.status_label = tk.Label(root, textvariable=self.status_var)
        self.status_label.pack(pady=(0, 8))

        self.worker = CameraWorker(self.frame_queue, self.status_queue)
        self.worker.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._last_photo = None
        self.update_ui()

    def on_record(self):
        self.worker.send_cmd("record")
        self.record_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

    def on_stop(self):
        self.worker.send_cmd("stop")
        self.record_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    def on_close(self):
        self.worker.send_cmd("quit")
        self.root.after(200, self.root.destroy)

    def update_ui(self):
        # Update preview
        try:
            frame = self.frame_queue.get_nowait()
            img = Image.fromarray(frame)
            photo = ImageTk.PhotoImage(img)
            self.video_label.configure(image=photo)
            self._last_photo = photo  # keep reference
        except queue.Empty:
            pass

        # Update status
        try:
            while True:
                msg = self.status_queue.get_nowait()
                self.status_var.set(msg)
        except queue.Empty:
            pass

        self.root.after(15, self.update_ui)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
