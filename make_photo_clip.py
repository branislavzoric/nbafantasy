#!/usr/bin/env python3
"""
make_photo_clip.py

Turns still photos into a smooth zoom/pan ("Ken Burns") clip for the bottom half of
a reel, for when there's no video we're allowed to use. Output is 1080x960, so
build_reel.py uses it as-is without cropping.

The shot list is a JSON file next to the photos:
  {"output": "clip.mp4", "crossfade": 0.6,
   "shots": [{"image": "a.jpg", "seconds": 6,
              "from": {"cx": 1632, "cy": 1224, "w": 2750},   # crop centre + width, in
              "to":   {"cx": 1106, "cy": 1500, "w": 1300},   # source pixels
              "credit": "...", "source": "..."}]}
Each shot moves from "from" to "to" (eased); consecutive shots crossfade.

Usage:
  python make_photo_clip.py "Reel Clips/holland_photos.json"
"""

import json
import os
import subprocess
import sys

from PIL import Image

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(PROJECT_DIR, "tools", "ffmpeg", "bin", "ffmpeg.exe")
OUT_W, OUT_H = 1080, 960
FPS = 30


def ease(x):
    return x * x * (3 - 2 * x)  # smoothstep: gentle start and stop


def crop_box(img, cx, cy, w):
    """Crop box with the output aspect ratio, kept inside the image."""
    w = min(w, img.width, img.height * OUT_W / OUT_H)
    h = w * OUT_H / OUT_W
    x0 = min(max(cx - w / 2, 0), img.width - w)
    y0 = min(max(cy - h / 2, 0), img.height - h)
    return (x0, y0, x0 + w, y0 + h)


def shot_frames(shot, base_dir):
    img = Image.open(os.path.join(base_dir, shot["image"])).convert("RGB")
    n = round(shot["seconds"] * FPS)
    a, b = shot["from"], shot["to"]
    for i in range(n):
        k = ease(i / max(n - 1, 1))
        box = crop_box(img, *(a[key] + (b[key] - a[key]) * k for key in ("cx", "cy", "w")))
        yield img.resize((OUT_W, OUT_H), Image.LANCZOS, box=box)


def all_frames(spec, base_dir):
    """Consecutive shots overlap by `crossfade` seconds: the last frames of one shot
    are held back and blended into the first frames of the next."""
    fade = round(spec.get("crossfade", 0) * FPS)
    held = []
    for s, shot in enumerate(spec["shots"]):
        last_shot = s == len(spec["shots"]) - 1
        frames = list(shot_frames(shot, base_dir))
        for i, frame in enumerate(frames[:len(held)]):
            yield Image.blend(held[i], frame, (i + 1) / (len(held) + 1))
        keep = 0 if last_shot else fade
        yield from frames[len(held):len(frames) - keep]
        held = frames[len(frames) - keep:]


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    spec_path = os.path.abspath(sys.argv[1])
    base_dir = os.path.dirname(spec_path)
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    out_path = os.path.join(base_dir, spec["output"])

    ffmpeg = subprocess.Popen(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{OUT_W}x{OUT_H}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", out_path],
        stdin=subprocess.PIPE)
    count = 0
    for frame in all_frames(spec, base_dir):
        ffmpeg.stdin.write(frame.tobytes())
        count += 1
    ffmpeg.stdin.close()
    if ffmpeg.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(f"Saved {out_path} ({count / FPS:.1f}s)")
    for shot in spec["shots"]:
        print(f"  {shot['image']}: {shot.get('credit', '')}")


if __name__ == "__main__":
    main()
