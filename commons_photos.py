"""Search Wikimedia Commons for freely licensed photos and save candidates + a contact sheet.

  venv/Scripts/python.exe commons_photos.py <photos dir> key="search query" [key2="query 2" ...]

For each key it saves up to 4 candidates as <dir>/candidates/<key>_<n>.jpg with their
credit in <dir>/candidates/meta.json, and writes <dir>/candidates/sheet.jpg (key + number
under each thumb). Pick one with:
  venv/Scripts/python.exe commons_photos.py <photos dir> --pick key=n [key2=n ...]
which copies it to <dir>/<key>.jpg and records {file, page, license, author} in credits.json.
"""
import io
import json
import re
import shutil
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw

API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "B3BasketballBot/1.0 (b3basketball.com; news photo credits)"}
OK_LICENSES = ("cc by", "cc-by", "cc0", "public domain", "pd")
PER_KEY = 4


def clean(html):
    return re.sub(r"<[^>]+>", "", html or "").strip()


def get(params, tries=4):
    for i in range(tries):
        r = requests.get(API, params=params, headers=UA, timeout=30)
        if r.status_code == 429:
            time.sleep(15 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Commons kept rate-limiting")


def search(query):
    data = get({"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": 20, "prop": "imageinfo",
                "iiprop": "url|size|extmetadata", "iiurlwidth": 1280})
    out = []
    pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    for p in pages:
        ii = p["imageinfo"][0]
        meta = ii.get("extmetadata", {})
        lic = clean(meta.get("LicenseShortName", {}).get("value"))
        if not lic.lower().startswith(OK_LICENSES) or ii.get("width", 0) < 600:
            continue
        out.append({"file": p["title"], "page": ii["descriptionurl"], "license": lic,
                    "author": clean(meta.get("Artist", {}).get("value")) or "unknown", "thumb": ii["thumburl"]})
        if len(out) == PER_KEY:
            break
    return out


def fetch(dir_, pairs):
    cand = dir_ / "candidates"
    cand.mkdir(parents=True, exist_ok=True)
    meta_file = cand / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    for key, query in pairs:
        found = search(query)
        print(f"{key}: {len(found)} candidates for {query!r}")
        meta[key] = []
        for n, c in enumerate(found, 1):
            time.sleep(4)
            r = requests.get(c.pop("thumb"), headers=UA, timeout=60)
            if r.status_code != 200:
                print(f"  {key}_{n}: HTTP {r.status_code}")
                continue
            Image.open(io.BytesIO(r.content)).convert("RGB").save(cand / f"{key}_{n}.jpg", quality=88)
            meta[key].append(dict(c, n=n))
        time.sleep(6)
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    sheet(cand, meta)


def sheet(cand, meta):
    tw, th = 300, 220
    rows = [(k, m["n"]) for k, items in meta.items() for m in items if (cand / f"{k}_{m['n']}.jpg").exists()]
    keys = list(dict.fromkeys(k for k, _ in rows))
    im = Image.new("RGB", (tw * PER_KEY, (th + 24) * max(len(keys), 1)), "white")
    d = ImageDraw.Draw(im)
    for k, n in rows:
        t = Image.open(cand / f"{k}_{n}.jpg")
        t.thumbnail((tw - 6, th - 6))
        x, y = (n - 1) * tw, keys.index(k) * (th + 24)
        im.paste(t, (x + 3, y + 3))
        d.text((x + 6, y + th + 4), f"{k}={n}", fill="black")
    im.save(cand / "sheet.jpg", quality=85)
    print(f"sheet -> {cand / 'sheet.jpg'}")


def pick(dir_, picks):
    cand = dir_ / "candidates"
    meta = json.loads((cand / "meta.json").read_text(encoding="utf-8"))
    cred_file = dir_ / "credits.json"
    credits = json.loads(cred_file.read_text(encoding="utf-8")) if cred_file.exists() else {}
    for key, n in picks:
        m = next(m for m in meta[key] if m["n"] == int(n))
        shutil.copy(cand / f"{key}_{n}.jpg", dir_ / f"{key}.jpg")
        credits[key] = {k: m[k] for k in ("file", "page", "license", "author")}
        print(f"{key}.jpg <- {m['file']} ({m['license']})")
    cred_file.write_text(json.dumps(credits, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    args = [a.split("=", 1) for a in sys.argv[2:] if a != "--pick"]
    (pick if "--pick" in sys.argv else fetch)(target, args)
