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

        self.preview_proc = None
        self.record_proc = None
        self.h264_path = None
        self.mp4_path = None

        self.status = tk.StringVar(value="Starting preview…")

        self.btn_record = tk.Button(root, text="Record", width=20, command=self.start_recording)
        self.btn_stop   = tk.Button(root, text="Stop",   width=20, command=self.stop_recording, state=tk.DISABLED)
        self.lbl_status = tk.Label(root, textvariable=self.status)

        self.btn_record.pack(padx=20, pady=(20, 10))
        self.btn_stop.pack(padx=20, pady=10)
        self.lbl_status.pack(padx=20, pady=(10, 20))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.vid_cmd, self.hello_cmd = self.find_camera_commands()
        if not self.vid_cmd or not self.hello_cmd:
            messagebox.showerror(
                "Error",
                "Missing camera tools.\n"
                "Need rpicam-vid + rpicam-hello (or libcamera-vid + libcamera-hello).\n\n"
                "Try:\n  sudo apt update\n  sudo apt install -y rpicam-apps"
            )
            root.destroy()
            return

        if not shutil.which("ffmpeg"):
            messagebox.showerror("Error", "ffmpeg not found. Install it:\nsudo apt install -y ffmpeg")
            root.destroy()
            return

        # Start preview immediately and keep it running while idle
        self.start_preview()

    def find_camera_commands(self):
        vid = None
        hello = None
        if shutil.which("rpicam-vid"):
            vid = "rpicam-vid"
        elif shutil.which("libcamera-vid"):
            vid = "libcamera-vid"

        if shutil.which("rpicam-hello"):
            hello = "rpicam-hello"
        elif shutil.which("libcamera-hello"):
            hello = "libcamera-hello"

        return vid, hello

    def start_preview(self):
        if self.preview_proc is not None:
            return

        # -t 0 => run forever
        cmd = [self.hello_cmd, "-t", "0"]
        try:
            self.preview_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self.status.set("Preview running (idle).")
        except Exception as e:
            self.preview_proc = None
            self.status.set("Preview failed to start.")
            messagebox.showerror("Error", f"Failed to start preview:\n{e}")

    def stop_preview(self):
        if self.preview_proc is None:
            return
        try:
            self.preview_proc.send_signal(signal.SIGINT)
            self.preview_proc.wait(timeout=5)
        except Exception:
            try:
                self.preview_proc.terminate()
                self.preview_proc.wait(timeout=2)
            except Exception:
                self.preview_proc.kill()
        finally:
            self.preview_proc = None

    def start_recording(self):
        if self.record_proc is not None:
            return

        out_dir = os.path.expanduser("~/Videos")
        os.makedirs(out_dir, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        self.h264_path = os.path.join(out_dir, f"recording_{ts}.h264")
        self.mp4_path  = os.path.join(out_dir, f"recording_{ts}.mp4")

        # Switch from preview -> recording-with-preview
        self.status.set("Switching to recording…")
        self.root.update_idletasks()
        self.stop_preview()

        # No --nopreview here => preview window stays visible while recording
        cmd = [
            self.vid_cmd,
            "-t", "0",                 # record until stopped
            "--inline",                # include SPS/PPS (helps MP4 wrapping)
            "--width", str(WIDTH),
            "--height", str(HEIGHT),
            "--framerate", str(FPS),
            "-o", self.h264_path
        ]

        try:
            self.record_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            self.record_proc = None
            messagebox.showerror("Error", f"Failed to start recording:\n{e}")
            # Go back to preview if recording failed
            self.start_preview()
            self.status.set("Preview running (idle).")
            return

        self.status.set(f"Recording… ({os.path.basename(self.h264_path)})")
        self.btn_record.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

    def stop_recording(self):
        if self.record_proc is None:
            return

        self.status.set("Stopping recording…")
        self.root.update_idletasks()

        # Stop recording (like Ctrl+C)
        try:
            self.record_proc.send_signal(signal.SIGINT)
            self.record_proc.wait(timeout=10)
        except Exception:
            try:
                self.record_proc.terminate()
                self.record_proc.wait(timeout=5)
            except Exception:
                self.record_proc.kill()

        self.record_proc = None

        # Convert to MP4 (fast wrap, no re-encode)
        self.status.set("Converting to MP4…")
        self.root.update_idletasks()

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-r", str(FPS),
            "-i", self.h264_path,
            "-c", "copy",
            "-movflags", "+faststart",
            self.mp4_path
        ]

        try:
            subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            messagebox.showerror("Error", "FFmpeg failed to create MP4.")
            self.status.set("Conversion failed. Restarting preview…")
            self.btn_record.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            self.start_preview()
            return

        # Optional: delete .h264 after successful conversion
        try:
            os.remove(self.h264_path)
        except OSError:
            pass

        self.status.set(f"Saved: {self.mp4_path}")
        self.btn_record.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)

        # Go back to always-on preview
        self.start_preview()

    def on_close(self):
        # Stop everything cleanly
        if self.record_proc is not None:
            if messagebox.askyesno("Quit", "Recording is in progress. Stop and save MP4 before quitting?"):
                self.stop_recording()
            else:
                # user chose to quit without saving nicely
                try:
                    self.record_proc.terminate()
                except Exception:
                    pass

        self.stop_preview()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = RecorderApp(root)
    root.mainloop()
