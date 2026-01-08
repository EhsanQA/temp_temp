#!/usr/bin/env python3
import os
import time
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput

PREVIEW_SIZE = (640, 360)
RECORD_SIZE  = (1920, 1080)
FPS = 30
BITRATE = 10_000_000  # 10 Mbps


def ffmpeg_wrap_h264_to_mp4(h264_path: str, mp4_path: str, fps: int):
    # Fast wrap (no re-encode)
    cmd = ["ffmpeg", "-y", "-r", str(fps), "-i", h264_path, "-c", "copy", "-movflags", "+faststart", mp4_path]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class CameraWorker(threading.Thread):
    """
    Owns Picamera2. GUI never calls picam2 directly.
    Communicates using:
      - cmd_q: commands from GUI
      - evt_q: events back to GUI
      - latest_frame: last preview frame (lores)
    """
    def __init__(self, cmd_q: queue.Queue, evt_q: queue.Queue):
        super().__init__(daemon=True)
        self.cmd_q = cmd_q
        self.evt_q = evt_q

        self.frame_lock = threading.Lock()
        self.latest_frame = None

        self.running = True
        self.recording = False

        self.picam2 = None
        self.encoder = None
        self.output = None

        self.current_h264 = None
        self.current_mp4 = None

    def run(self):
        try:
            self.picam2 = Picamera2()
            config = self.picam2.create_video_configuration(
                main={"size": RECORD_SIZE},
                lores={"size": PREVIEW_SIZE, "format": "RGB888"},
                controls={"FrameRate": FPS},
            )
            self.picam2.configure(config)
            self.picam2.start()  # start preview pipeline
            self.evt_q.put(("status", "Preview running (idle)."))
            self.evt_q.put(("ready", None))
        except Exception as e:
            self.evt_q.put(("fatal", f"Failed to initialize camera: {e}"))
            return

        target_dt = 1.0 / FPS

        while self.running:
            # 1) Handle all pending commands first (start/stop/shutdown)
            try:
                while True:
                    cmd, payload = self.cmd_q.get_nowait()
                    if cmd == "shutdown":
                        self._shutdown()
                        return
                    elif cmd == "start":
                        self._start_recording(payload)
                    elif cmd == "stop":
                        self._stop_recording_and_resume_preview()
            except queue.Empty:
                pass

            # 2) Grab a preview frame (lores)
            try:
                frame = self.picam2.capture_array("lores")
                with self.frame_lock:
                    self.latest_frame = frame
            except Exception:
                # If pipeline is restarting, brief failures can happen; just keep looping.
                time.sleep(0.02)

            time.sleep(target_dt)

    def get_latest_frame(self):
        with self.frame_lock:
            return self.latest_frame

    def _start_recording(self, paths):
        if self.recording:
            return
        h264_path, mp4_path = paths
        self.current_h264 = h264_path
        self.current_mp4 = mp4_path

        try:
            self.evt_q.put(("status", "Starting recording…"))

            self.encoder = H264Encoder(bitrate=BITRATE)
            self.output = FileOutput(h264_path)

            # start_recording may (re)start camera/encoder internally depending on version
            self.picam2.start_recording(self.encoder, self.output)
            self.recording = True

            self.evt_q.put(("recording", True))
            self.evt_q.put(("status", f"Recording… {os.path.basename(mp4_path)}"))
        except Exception as e:
            self.recording = False
            self.encoder = None
            self.output = None
            self.evt_q.put(("recording", False))
            self.evt_q.put(("error", f"Failed to start recording: {e}"))

    def _stop_recording_and_resume_preview(self):
        if not self.recording:
            return

        self.evt_q.put(("status", "Stopping recording…"))
        err = None

        try:
            self.picam2.stop_recording()
        except Exception as e:
            err = f"stop_recording failed: {e}"

        # IMPORTANT: stop_recording can stop the camera pipeline;
        # restart it so preview continues.  [oai_citation:1‡grobotronics.com](https://grobotronics.com/images/companies/1/content_processor/PDF/picamera2-manual.pdf?srsltid=AfmBOooTHgnNhiH6rc4kVFrtq-_RB-Fa2Vpvm1sxJtgV_pZqKD9ggRPj&utm_source=chatgpt.com)
        try:
            self.picam2.start()
        except Exception as e:
            if err is None:
                err = f"Failed to restart preview after stop: {e}"
            else:
                err += f" | restart preview failed: {e}"

        self.recording = False
        self.encoder = None
        self.output = None
        self.evt_q.put(("recording", False))

        if err:
            self.evt_q.put(("error", err))
            self.evt_q.put(("status", "Preview running (idle)."))
            self.evt_q.put(("ready", None))
            return

        # Convert h264 -> mp4 in a separate thread (don’t block camera/preview)
        h264_path = self.current_h264
        mp4_path = self.current_mp4

        def convert():
            try:
                self.evt_q.put(("status", "Saving MP4…"))
                ffmpeg_wrap_h264_to_mp4(h264_path, mp4_path, FPS)
                try:
                    os.remove(h264_path)
                except OSError:
                    pass
                self.evt_q.put(("saved", mp4_path))
                self.evt_q.put(("status", "Preview running (idle)."))
                self.evt_q.put(("ready", None))
            except Exception as e:
                self.evt_q.put(("error", f"FFmpeg conversion failed: {e}"))
                self.evt_q.put(("status", "Preview running (idle)."))
                self.evt_q.put(("ready", None))

        threading.Thread(target=convert, daemon=True).start()

    def _shutdown(self):
        self.running = False
        try:
            if self.recording:
                try:
                    self.picam2.stop_recording()
                except Exception:
                    pass
            if self.picam2:
                self.picam2.stop()
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Pi Camera Recorder")

        self.video_label = tk.Label(root)
        self.video_label.pack(padx=10, pady=10)

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=(0, 10))

        self.btn_record = tk.Button(btn_frame, text="Record", width=15, command=self.on_record)
        self.btn_stop   = tk.Button(btn_frame, text="Stop",   width=15, command=self.on_stop, state=tk.DISABLED)
        self.btn_record.pack(side=tk.LEFT, padx=10)
        self.btn_stop.pack(side=tk.LEFT, padx=10)

        self.status = tk.StringVar(value="Starting…")
        tk.Label(root, textvariable=self.status).pack(pady=(0, 10))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.cmd_q = queue.Queue()
        self.evt_q = queue.Queue()

        self.worker = CameraWorker(self.cmd_q, self.evt_q)
        self.worker.start()

        self._tk_image = None
        self.recording = False
        self.ready = False

        self.poll_events()
        self.update_preview()

    def set_buttons(self):
        # Record allowed only when ready and not recording
        self.btn_record.config(state=tk.NORMAL if (self.ready and not self.recording) else tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL if self.recording else tk.DISABLED)

    def poll_events(self):
        try:
            while True:
                typ, payload = self.evt_q.get_nowait()
                if typ == "status":
                    self.status.set(payload)
                elif typ == "ready":
                    self.ready = True
                    self.set_buttons()
                elif typ == "recording":
                    self.recording = bool(payload)
                    self.ready = not self.recording  # simplistic: ready when idle
                    self.set_buttons()
                elif typ == "saved":
                    self.status.set(f"Saved: {payload}")
                elif typ == "error":
                    messagebox.showerror("Error", payload)
                    self.ready = True
                    self.recording = False
                    self.set_buttons()
                elif typ == "fatal":
                    messagebox.showerror("Fatal", payload)
                    self.root.destroy()
                    return
        except queue.Empty:
            pass

        self.root.after(100, self.poll_events)

    def update_preview(self):
        frame = self.worker.get_latest_frame()
        if frame is not None:
            try:
                img = Image.fromarray(frame)
                self._tk_image = ImageTk.PhotoImage(img)
                self.video_label.configure(image=self._tk_image)
            except Exception:
                pass
        self.root.after(int(1000 / FPS), self.update_preview)

    def on_record(self):
        self.ready = False
        self.set_buttons()

        out_dir = os.path.expanduser("~/Videos")
        os.makedirs(out_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        h264_path = os.path.join(out_dir, f"recording_{ts}.h264")
        mp4_path  = os.path.join(out_dir, f"recording_{ts}.mp4")

        self.cmd_q.put(("start", (h264_path, mp4_path)))

    def on_stop(self):
        self.cmd_q.put(("stop", None))

    def on_close(self):
        # If recording, ask first
        if self.recording:
            if not messagebox.askyesno("Quit", "Recording is in progress. Stop and save before quitting?"):
                return
            self.cmd_q.put(("stop", None))
            # Let it finish; user can close after
            return

        self.cmd_q.put(("shutdown", None))
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
