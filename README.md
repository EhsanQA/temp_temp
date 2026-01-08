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

```python
from pathlib import Path
import os

def find_upwards(filename: str, start: Path) -> Path | None:
    for p in [start, *start.parents]:
        cand = p / filename
        if cand.exists():
            return cand
    return None

def parse_env(env_path: Path) -> dict:
    env = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def find_hef_and_postprocess():
    here = Path(__file__).resolve().parent
    env_file = find_upwards(".env", here)

    # 1) Prefer .env (this is how the examples choose models)
    if env_file:
        os.environ["HAILO_ENV_FILE"] = str(env_file)
        env = parse_env(env_file)

        # try common key names (varies across versions)
        hef = (env.get("HEF_PATH") or env.get("NETWORK_HEF") or env.get("HEF") or
               env.get("HAILO_HEF_PATH") or env.get("MODEL_HEF"))
        post = (env.get("POSTPROCESS_SO") or env.get("YOLO_POSTPROCESS_SO") or
                env.get("HAILO_POSTPROCESS_SO"))

        # If .env contains relative paths, resolve them relative to repo root (where .env lives)
        root = env_file.parent
        if hef:
            hef_path = (root / hef).resolve() if not Path(hef).is_absolute() else Path(hef)
        else:
            hef_path = None

        if post:
            post_path = (root / post).resolve() if not Path(post).is_absolute() else Path(post)
        else:
            post_path = None

        if hef_path and hef_path.exists() and post_path and post_path.exists():
            return str(hef_path), str(post_path)

    # 2) Fallback: search common locations
    candidates = [
        Path.home() / "hailo-rpi5-examples" / "resources",
        Path("/usr/local/hailo/resources"),
        Path("/usr/share/hailo/resources"),
        Path("/opt/hailo/resources"),
    ]

    hef_path = None
    post_path = None
    for base in candidates:
        if base.exists():
            if hef_path is None:
                hefs = list(base.rglob("*.hef"))
                if hefs:
                    # prefer yolov* if present
                    yolos = [p for p in hefs if "yolo" in p.name.lower()]
                    hef_path = str((yolos[0] if yolos else hefs[0]).resolve())
            if post_path is None:
                posts = list(base.rglob("libyolo*_postprocess*.so"))
                if posts:
                    post_path = str(posts[0].resolve())
        if hef_path and post_path:
            return hef_path, post_path

    raise RuntimeError("Could not find HEF and YOLO postprocess .so. Check your .env and resources/ folder.")

HEF_PATH, POSTPROCESS_SO = find_hef_and_postprocess()
POSTPROCESS_FUNCTION = "filter_letterbox"   # what the examples typically use
```

```bash
sudo apt install -y gstreamer1.0-tools \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav
```
