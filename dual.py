#!/usr/bin/env python3
import os
import time
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk, ImageDraw

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput

# === Your requested settings ===
RECORD_SIZE = (1920, 1080)
FPS = 10

# Preview stream inside the app (keep small for speed)
PREVIEW_SIZE = (640, 360)

# Bitrate per camera (bits/sec). Increase for better quality/bigger files.
BITRATE = 8_000_000  # 8 Mbps per camera (you can change)

LINE_W = 6  # separator line width in preview


def ffmpeg_wrap_h264_to_mp4(h264_path: str, mp4_path: str, fps: int):
    # Fast "wrap" (no re-encode)
    cmd = ["ffmpeg", "-y", "-r", str(fps), "-i", h264_path,
           "-c", "copy", "-movflags", "+faststart", mp4_path]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class CameraWorker(threading.Thread):
    """
    Owns ONE Picamera2 instance (one camera). GUI never calls picam2 directly.
    Commands arrive via cmd_q; events sent via evt_q.
    """
    def __init__(self, cam_id: int, camera_num: int, cmd_q: queue.Queue, evt_q: queue.Queue):
        super().__init__(daemon=True)
        self.cam_id = cam_id
        self.camera_num = camera_num
        self.cmd_q = cmd_q
        self.evt_q = evt_q

        self.frame_lock = threading.Lock()
        self.latest_frame = None

        self.running = True
        self.recording = False

        self.picam2 = None
        self.encoder = None
        self.output = None

        self.cur_h264 = None
        self.cur_mp4 = None

    def get_latest_frame(self):
        with self.frame_lock:
            return self.latest_frame

    def run(self):
        try:
            self.picam2 = Picamera2(camera_num=self.camera_num)
            config = self.picam2.create_video_configuration(
                main={"size": RECORD_SIZE},
                lores={"size": PREVIEW_SIZE, "format": "RGB888"},
                controls={"FrameRate": FPS},
            )
            self.picam2.configure(config)
            self.picam2.start()
            self.evt_q.put(("ready", self.cam_id, None))
            self.evt_q.put(("status", self.cam_id, "Preview running"))
        except Exception as e:
            self.evt_q.put(("fatal", self.cam_id, f"Camera init failed: {e}"))
            return

        target_dt = 1.0 / FPS

        while self.running:
            # handle commands (start/stop/shutdown)
            try:
                while True:
                    cmd, payload = self.cmd_q.get_nowait()
                    if cmd == "shutdown":
                        self._shutdown()
                        return
                    elif cmd == "start":
                        self._start_recording(payload)
                    elif cmd == "stop":
                        self._stop_recording()
            except queue.Empty:
                pass

            # grab preview frame
            try:
                frame = self.picam2.capture_array("lores")
                with self.frame_lock:
                    self.latest_frame = frame
            except Exception:
                time.sleep(0.02)

            time.sleep(target_dt)

    def _start_recording(self, paths):
        if self.recording:
            return
        h264_path, mp4_path = paths
        self.cur_h264 = h264_path
        self.cur_mp4 = mp4_path

        try:
            self.evt_q.put(("status", self.cam_id, "Starting recording"))
            self.encoder = H264Encoder(bitrate=BITRATE)
            self.output = FileOutput(h264_path)
            self.picam2.start_recording(self.encoder, self.output)
            self.recording = True
            self.evt_q.put(("recording", self.cam_id, True))
            self.evt_q.put(("status", self.cam_id, f"Recording -> {os.path.basename(mp4_path)}"))
        except Exception as e:
            self.recording = False
            self.encoder = None
            self.output = None
            self.evt_q.put(("recording", self.cam_id, False))
            self.evt_q.put(("error", self.cam_id, f"Start recording failed: {e}"))
            self.evt_q.put(("ready", self.cam_id, None))

    def _stop_recording(self):
        if not self.recording:
            return

        self.evt_q.put(("status", self.cam_id, "Stopping recording"))
        err = None

        try:
            self.picam2.stop_recording()
        except Exception as e:
            err = f"stop_recording failed: {e}"

        # Some stacks stop the pipeline; ensure preview resumes.
        try:
            self.picam2.start()
        except Exception as e:
            err = (err + " | " if err else "") + f"preview restart failed: {e}"

        self.recording = False
        self.encoder = None
        self.output = None
        self.evt_q.put(("recording", self.cam_id, False))

        if err:
            self.evt_q.put(("error", self.cam_id, err))
            self.evt_q.put(("status", self.cam_id, "Preview running"))
            self.evt_q.put(("ready", self.cam_id, None))
            return

        # Convert h264->mp4 off-thread (do not block preview)
        h264_path = self.cur_h264
        mp4_path = self.cur_mp4

        def convert():
            try:
                self.evt_q.put(("status", self.cam_id, "Saving MP4"))
                ffmpeg_wrap_h264_to_mp4(h264_path, mp4_path, FPS)
                try:
                    os.remove(h264_path)
                except OSError:
                    pass
                self.evt_q.put(("saved", self.cam_id, mp4_path))
            except Exception as e:
                self.evt_q.put(("error", self.cam_id, f"FFmpeg failed: {e}"))
            finally:
                self.evt_q.put(("status", self.cam_id, "Preview running"))
                self.evt_q.put(("ready", self.cam_id, None))

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


class DualCamApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Dual Camera Recorder")

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

        self.evt_q = queue.Queue()

        # One command queue per camera
        self.cmd_q0 = queue.Queue()
        self.cmd_q1 = queue.Queue()

        # Change camera_num if needed (0/1 is typical)
        self.worker0 = CameraWorker(cam_id=0, camera_num=0, cmd_q=self.cmd_q0, evt_q=self.evt_q)
        self.worker1 = CameraWorker(cam_id=1, camera_num=1, cmd_q=self.cmd_q1, evt_q=self.evt_q)
        self.worker0.start()
        self.worker1.start()

        self.ready = {0: False, 1: False}
        self.recording = {0: False, 1: False}
        self.last_status = {0: "Starting", 1: "Starting"}

        self._tk_image = None

        self.poll_events()
        self.update_preview()

    def set_buttons(self):
        all_ready = self.ready[0] and self.ready[1]
        any_recording = self.recording[0] or self.recording[1]
        self.btn_record.config(state=tk.NORMAL if (all_ready and not any_recording) else tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL if any_recording else tk.DISABLED)

        # Combined status line
        self.status.set(f"Cam0: {self.last_status[0]}  |  Cam1: {self.last_status[1]}")

    def poll_events(self):
        try:
            while True:
                typ, cam_id, payload = self.evt_q.get_nowait()

                if typ == "ready":
                    self.ready[cam_id] = True
                elif typ == "recording":
                    self.recording[cam_id] = bool(payload)
                    # when recording starts, consider that cam not "ready" for new record
                    self.ready[cam_id] = not self.recording[cam_id]
                elif typ == "status":
                    self.last_status[cam_id] = payload
                elif typ == "saved":
                    self.last_status[cam_id] = f"Saved {os.path.basename(payload)}"
                elif typ == "error":
                    self.last_status[cam_id] = "Error"
                    messagebox.showerror(f"Camera {cam_id} error", payload)
                    self.ready[cam_id] = True
                    self.recording[cam_id] = False
                elif typ == "fatal":
                    messagebox.showerror(f"Camera {cam_id} fatal", payload)
                    self.root.destroy()
                    return

                self.set_buttons()
        except queue.Empty:
            pass

        self.root.after(100, self.poll_events)

    def update_preview(self):
        f0 = self.worker0.get_latest_frame()
        f1 = self.worker1.get_latest_frame()

        if f0 is not None and f1 is not None:
            try:
                img0 = Image.fromarray(f0)
                img1 = Image.fromarray(f1)

                w, h = img0.size
                out = Image.new("RGB", (w + LINE_W + w, h), (0, 0, 0))
                out.paste(img0, (0, 0))
                out.paste(img1, (w + LINE_W, 0))

                draw = ImageDraw.Draw(out)
                # separator line
                draw.rectangle([w, 0, w + LINE_W - 1, h], fill=(255, 255, 255))

                self._tk_image = ImageTk.PhotoImage(out)
                self.video_label.configure(image=self._tk_image)
            except Exception:
                pass

        self.root.after(int(1000 / FPS), self.update_preview)

    def on_record(self):
        # disable until both workers report recording
        self.ready[0] = False
        self.ready[1] = False
        self.set_buttons()

        out_dir = os.path.expanduser("~/Videos")
        os.makedirs(out_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")

        h264_0 = os.path.join(out_dir, f"cam0_{ts}.h264")
        mp4_0  = os.path.join(out_dir, f"cam0_{ts}.mp4")
        h264_1 = os.path.join(out_dir, f"cam1_{ts}.h264")
        mp4_1  = os.path.join(out_dir, f"cam1_{ts}.mp4")

        self.cmd_q0.put(("start", (h264_0, mp4_0)))
        self.cmd_q1.put(("start", (h264_1, mp4_1)))

    def on_stop(self):
        self.cmd_q0.put(("stop", None))
        self.cmd_q1.put(("stop", None))

    def on_close(self):
        if self.recording[0] or self.recording[1]:
            if not messagebox.askyesno("Quit", "Recording is in progress. Stop and save before quitting?"):
                return
            self.on_stop()
            return

        self.cmd_q0.put(("shutdown", None))
        self.cmd_q1.put(("shutdown", None))
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    DualCamApp(root)
    root.mainloop()
