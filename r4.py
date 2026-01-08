#!/usr/bin/env python3
import os
import time
import threading
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

PREVIEW_SIZE = (640, 360)    # preview in the app (fast)
RECORD_SIZE  = (1920, 1080)  # recorded size
FPS = 30
BITRATE = 10_000_000         # 10 Mbps


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

        # --- State ---
        self.recording = False
        self.busy = False  # true while starting/stopping recording
        self._tk_image = None

        self.encoder = None
        self.output = None
        self.mp4_path = None

        # --- Camera init ---
        try:
            self.picam2 = Picamera2()
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

        self.status.set("Preview running (idle).")
        self.update_preview()

    def set_buttons(self, record_enabled: bool, stop_enabled: bool):
        self.btn_record.config(state=tk.NORMAL if record_enabled else tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL if stop_enabled else tk.DISABLED)

    def update_preview(self):
        # Keep preview going; if camera is busy, just skip frames rather than blocking UI
        if not self.busy:
            try:
                frame = self.picam2.capture_array("lores")
                img = Image.fromarray(frame)
                self._tk_image = ImageTk.PhotoImage(img)
                self.video_label.configure(image=self._tk_image)
            except Exception:
                pass

        self.root.after(int(1000 / FPS), self.update_preview)

    def start_recording(self):
        if self.recording or self.busy:
            return

        out_dir = os.path.expanduser("~/Videos")
        os.makedirs(out_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.mp4_path = os.path.join(out_dir, f"recording_{ts}.mp4")

        try:
            self.busy = True
            self.status.set("Starting recording…")
            self.set_buttons(False, False)

            self.encoder = H264Encoder(bitrate=BITRATE)
            self.output = FfmpegOutput(self.mp4_path)

            # start_recording can be slightly blocking too, but usually quick
            self.picam2.start_recording(self.encoder, self.output)

            self.recording = True
            self.status.set(f"Recording… {os.path.basename(self.mp4_path)}")
            self.set_buttons(False, True)
        except Exception as e:
            self.recording = False
            self.encoder = None
            self.output = None
            self.status.set("Preview running (idle).")
            self.set_buttons(True, False)
            messagebox.showerror("Recording error", f"Failed to start recording:\n{e}")
        finally:
            self.busy = False

    def stop_recording(self):
        if not self.recording or self.busy:
            return

        self.busy = True
        self.status.set("Stopping & saving…")
        self.set_buttons(False, False)

        # Do the potentially-blocking stop in a background thread
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self):
        err = None
        try:
            self.picam2.stop_recording()

            # Ensure ffmpeg output is closed (helps prevent “hang” on some setups)
            try:
                if self.output is not None:
                    self.output.close()
            except Exception:
                pass
        except Exception as e:
            err = e
        finally:
            self.recording = False
            self.encoder = None
            self.output = None

        # UI updates must happen on the Tk main thread
        def done():
            self.busy = False
            if err is not None:
                self.status.set("Stopped (error while saving).")
                self.set_buttons(True, False)
                messagebox.showerror("Recording error", f"Failed to stop/save cleanly:\n{err}")
            else:
                self.status.set(f"Saved: {self.mp4_path}")
                self.set_buttons(True, False)

        self.root.after(0, done)

    def on_close(self):
        if self.recording:
            if messagebox.askyesno("Quit", "Recording is in progress. Stop and save before quitting?"):
                self.stop_recording()
                # Wait a bit for the stop thread; easiest is to prevent closing now
                return
        try:
            if self.recording:
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
