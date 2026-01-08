#!/usr/bin/env python3
import os
import time
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput


PREVIEW_SIZE = (640, 360)   # what you see in the app window (fast)
RECORD_SIZE  = (1920, 1080) # what gets recorded to mp4
FPS = 30
BITRATE = 10_000_000        # 10 Mbps (adjust if needed)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Pi Camera Recorder")

        # --- UI ---
        self.video_label = tk.Label(root)
        self.video_label.pack(padx=10, pady=10)

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=(0, 10))

        self.btn_record = tk.Button(btn_frame, text="Record", width=15, command=self.start_recording)
        self.btn_stop   = tk.Button(btn_frame, text="Stop",   width=15, command=self.stop_recording, state=tk.DISABLED)
        self.btn_record.pack(side=tk.LEFT, padx=10)
        self.btn_stop.pack(side=tk.LEFT, padx=10)

        self.status = tk.StringVar(value="Initializing camera…")
        tk.Label(root, textvariable=self.status).pack(pady=(0, 10))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # --- Camera ---
        try:
            self.picam2 = Picamera2()
            # Use a small "lores" stream for preview (fast), and a larger main stream for recording.
            config = self.picam2.create_video_configuration(
                main={"size": RECORD_SIZE},
                lores={"size": PREVIEW_SIZE, "format": "RGB888"},
                controls={"FrameRate": FPS},
            )
            self.picam2.configure(config)
            self.picam2.start()
        except Exception as e:
            messagebox.showerror("Camera error", f"Failed to initialize camera:\n{e}")
            raise

        self.recording = False
        self.encoder = None
        self.output = None
        self.mp4_path = None

        self.status.set("Preview running (idle).")
        self._tk_image = None

        # Start UI update loop
        self.update_preview()

    def update_preview(self):
        """Grab a frame and show it inside the Tk window."""
        try:
            frame = self.picam2.capture_array("lores")  # RGB888 numpy array
            img = Image.fromarray(frame)
            self._tk_image = ImageTk.PhotoImage(img)
            self.video_label.configure(image=self._tk_image)
        except Exception:
            # Don’t crash the whole UI if one frame fails; just try again.
            pass

        # Schedule next frame
        self.root.after(int(1000 / FPS), self.update_preview)

    def start_recording(self):
        if self.recording:
            return

        out_dir = os.path.expanduser("~/Videos")
        os.makedirs(out_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.mp4_path = os.path.join(out_dir, f"recording_{ts}.mp4")

        try:
            self.encoder = H264Encoder(bitrate=BITRATE)
            self.output = FfmpegOutput(self.mp4_path)  # uses ffmpeg to mux into MP4
            self.picam2.start_recording(self.encoder, self.output)
        except Exception as e:
            self.encoder = None
            self.output = None
            messagebox.showerror("Recording error", f"Failed to start recording:\n{e}")
            return

        self.recording = True
        self.status.set(f"Recording… {os.path.basename(self.mp4_path)}")
        self.btn_record.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

    def stop_recording(self):
        if not self.recording:
            return

        try:
            self.picam2.stop_recording()
        except Exception as e:
            messagebox.showerror("Recording error", f"Failed to stop recording cleanly:\n{e}")
            # still try to recover UI state

        self.recording = False
        self.encoder = None
        self.output = None

        self.status.set(f"Saved: {self.mp4_path}")
        self.btn_record.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)

    def on_close(self):
        if self.recording:
            if messagebox.askyesno("Quit", "Recording is in progress. Stop and save before quitting?"):
                self.stop_recording()
            else:
                # If they insist, try to stop anyway to release camera
                try:
                    self.picam2.stop_recording()
                except Exception:
                    pass

        try:
            self.picam2.stop()
        except Exception:
            pass

        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
