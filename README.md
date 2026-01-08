# temp_temp
https://docs.google.com/presentation/d/1zYxR4G0wJFLDkvmqwWbI_PStGJ1yX2hjykahkLfjbNA/edit?usp=sharing
https://docs.google.com/presentation/d/1DXHP5Zfz3B-aKvx5-f9oXRL1_gLt8k7jetGwQ1B3Jok/edit?usp=sharing


```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-opencv python3-tk python3-pil python3-pil.imagetk ffmpeg
python3 -m pip install -U pip
python3 -m pip install ultralytics[export]
```
```bash
sudo apt install -y python3-gi gir1.2-gtk-3.0 gstreamer1.0-gtk3 ffmpeg
```


```bash
cd ~/hailo-rpi5-examples
grep -ni "hef\|post\|yolo\|function" .env
```

```python
from pathlib import Path

project_root = Path(__file__).resolve().parent  # adjust if your script is in a subfolder
env_file = project_root / ".env"

# quick-and-dirty parse
env = {}
for line in env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip('"').strip("'")

HEF_PATH = env.get("HEF_PATH") or env.get("NETWORK_HEF") or env.get("HEF")
POSTPROCESS_SO = env.get("POSTPROCESS_SO") or env.get("YOLO_POSTPROCESS_SO")
POSTPROCESS_FN = env.get("POSTPROCESS_FUNCTION") or env.get("YOLO_POSTPROCESS_FUNCTION") or "filter_letterbox"
```
