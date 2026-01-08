#!/usr/bin/env python3
import os
import signal
import shutil
import subprocess
import time
import tkinter as tk
from tkinter import messagebox

FPS = 30
WIDTH = 1920
HEIGHT = 1080

class RecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Pi Camera Recorder")

        self.proc = None
        self.h264_path = None
        self.mp4_path = None

        self.status = tk.StringVar(value="Idle")

        self.btn_record = tk.Button(root, text="Record", width=20, command=self.start_recording)
        self.btn_stop   = tk.Button(root, text="Stop",   width=20, command=self.stop_recording, state=tk.DISABLED)
        self.lbl_status = tk.Label(root, textvariable=self.status)

        self.btn_record.pack(padx=20, pady=(20, 10))
        self.btn_stop.pack(padx=20, pady=10)
        self.lbl_status.pack(padx=20, pady=(10, 20))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.vid_cmd = self.find_video_command()
        if not self.vid_cmd:
            messagebox.showerror("Error", "Neither 'rpicam-vid' nor 'libcamera-vid' was found.\n"
                                          "Install rpicam-apps or libcamera apps.")
            root.destroy()
            return

        if not shutil.which("ffmpeg"):
            messagebox.showerror("Error", "ffmpeg not found. Install it:\nsudo apt install -y ffmpeg")
            root.destroy()
            return

    def find_video_command(self):
        for cmd in ("rpicam-vid", "libcamera-vid"):
            if shutil.which(cmd):
                return cmd
        return None

    def start_recording(self):
        if self.proc is not None:
            return

        out_dir = os.path.expanduser("~/Videos")
        os.makedirs(out_dir, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        self.h264_path = os.path.join(out_dir, f"recording_{ts}.h264")
        self.mp4_path  = os.path.join(out_dir, f"recording_{ts}.mp4")

        # -t 0 => record until stopped
        # --nopreview => no camera window
        # --inline => include SPS/PPS (helps containerizing later)
        cmd = [
            self.vid_cmd,
            "-t", "0",
            "--nopreview",
            "--inline",
            "--width", str(WIDTH),
            "--height", str(HEIGHT),
            "--framerate", str(FPS),
            "-o", self.h264_path
        ]

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            self.proc = None
            messagebox.showerror("Error", f"Failed to start recording:\n{e}")
            return

        self.status.set(f"Recording… saving to {os.path.basename(self.h264_path)}")
        self.btn_record.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

    def stop_recording(self):
        if self.proc is None:
            return

        self.status.set("Stopping…")
        self.root.update_idletasks()

        # Graceful stop (like Ctrl+C)
        try:
            self.proc.send_signal(signal.SIGINT)
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

        self.proc = None

        # Convert to MP4 (fast “re-wrap”, no re-encode)
        self.status.set("Converting to MP4…")
        self.root.update_idletasks()

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-r", str(FPS),          # tell ffmpeg the input fps for raw h264
            "-i", self.h264_path,
            "-c", "copy",
            "-movflags", "+faststart",
            self.mp4_path
        ]

        try:
            subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            messagebox.showerror("Error", "FFmpeg failed to create MP4.\n"
                                        "Try lowering resolution/FPS or check camera/ffmpeg installation.")
            self.status.set("Idle (conversion failed)")
            self.btn_record.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            return

        # Optional: delete the .h264 after successful conversion
        try:
            os.remove(self.h264_path)
        except OSError:
            pass

        self.status.set(f"Saved: {self.mp4_path}")
        self.btn_record.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)

    def on_close(self):
        # If recording, stop and convert before exiting
        if self.proc is not None:
            if messagebox.askyesno("Quit", "Recording is in progress. Stop and save MP4 before quitting?"):
                self.stop_recording()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = RecorderApp(root)
    root.mainloop()
