"""Search Wikimedia Commons (then Flickr via Openverse) for freely licensed photos and save candidates + a contact sheet.

  venv/Scripts/python.exe commons_photos.py <photos dir> key="search query" [key2="query 2" ...]

For each key it saves up to 4 candidates as <dir>/candidates/<key>_<n>.jpg with their
credit in <dir>/candidates/meta.json, and writes <dir>/candidates/sheet.jpg (key + number
under each thumb). Pick one with:
  venv/Scripts/python.exe commons_photos.py <photos dir> --pick key=n [key2=n ...]
which copies it to <dir>/<key>.jpg and records {file, page, license, author, source} in credits.json.

Search order: Commons files with the full query in the title, then Commons full-text hits whose title
still contains every word of the query, then Openverse (mostly Flickr) with the same title rule.
Only licenses that allow commercial reuse: CC BY, CC BY-SA, CC0, public domain (no NC / ND).
"""
import io
import json
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

import requests
from PIL import Image, ImageDraw

API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE = "https://api.openverse.org/v1/images/"
UA = {"User-Agent": "B3BasketballBot/1.0 (b3basketball.com; news photo credits)"}
OK_LICENSES = ("cc by", "cc-by", "cc0", "public domain", "pd")
OV_LICENSES = {"by": "CC BY", "by-sa": "CC BY-SA", "cc0": "CC0", "pdm": "Public domain"}
PER_KEY = 4


def clean(html):
    return re.sub(r"<[^>]+>", "", html or "").strip()


def fold(s):
    """Lower-case, accents stripped, word list ("Porziņģis" -> "porzingis")."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", s)


def names_match(query, title):
    return set(fold(query)) <= set(fold(title))


def get(url, params, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=60)
        except requests.RequestException:
            time.sleep(10 * (i + 1))
            continue
        if r.status_code == 429:
            time.sleep(15 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{url} kept failing / rate-limiting")


def commons(query, search):
    data = get(API, {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                     "gsrsearch": f"{search} filetype:bitmap", "gsrlimit": 20, "prop": "imageinfo",
                     "iiprop": "url|size|extmetadata", "iiurlwidth": 1280})
    out = []
    pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    for p in pages:
        ii = p["imageinfo"][0]
        meta = ii.get("extmetadata", {})
        lic = clean(meta.get("LicenseShortName", {}).get("value"))
        if not lic.lower().startswith(OK_LICENSES) or ii.get("width", 0) < 600:
            continue
        if not names_match(query, p["title"]):
            continue
        out.append({"file": p["title"], "page": ii["descriptionurl"], "license": lic, "source": "Wikimedia Commons",
                    "author": clean(meta.get("Artist", {}).get("value")) or "unknown", "thumb": ii["thumburl"]})
    return out


def openverse(query):
    """Flickr and other CC sources indexed by Openverse (anonymous API: slow, a few requests a minute)."""
    try:
        data = get(OPENVERSE, {"q": query, "license": ",".join(OV_LICENSES), "page_size": 20}, tries=2)
    except Exception as e:                      # Openverse is the fallback: never fail the whole run on it
        print(f"  openverse: {e}")
        return []
    out = []
    for r in data.get("results", []):
        if r.get("source") == "wikimedia" or (r.get("width") or 0) < 600 or not names_match(query, r.get("title", "")):
            continue
        lic = OV_LICENSES.get(r["license"], r["license"].upper())
        if r["license"] not in ("cc0", "pdm"):
            lic += f' {r.get("license_version", "")}'.rstrip()
        out.append({"file": r["title"], "page": r["foreign_landing_url"], "license": lic,
                    "source": (r.get("source") or "openverse").capitalize(), "author": r.get("creator") or "unknown",
                    "thumb": r["url"]})
    return out


def search(query):
    seen, out = set(), []
    for found in (lambda: commons(query, f'intitle:"{query}"'), lambda: commons(query, query),
                  lambda: openverse(query)):
        for c in found():
            if c["page"] not in seen:
                seen.add(c["page"])
                out.append(c)
        if len(out) >= PER_KEY:
            break
    return out[:PER_KEY]


def download(url):
    headers = UA if "wikimedia.org" in url else {}     # Flickr's CDN answers 403 to bot-style user agents
    for i in range(4):
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code != 429:
            return r
        time.sleep(15 * (i + 1))
    return r


def fetch(dir_, pairs):
    cand = dir_ / "candidates"
    cand.mkdir(parents=True, exist_ok=True)
    meta_file = cand / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    for key, query in pairs:
        found = search(query)
        print(f"{key}: {len(found)} candidates for {query!r} "
              f"({', '.join(sorted({c['source'] for c in found})) or 'none'})")
        meta[key] = []
        for n, c in enumerate(found, 1):
            time.sleep(4)
            r = download(c.pop("thumb"))
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
        credits[key]["source"] = m.get("source", "Wikimedia Commons")
        print(f"{key}.jpg <- {m['file']} ({m['license']}, {credits[key]['source']})")
    cred_file.write_text(json.dumps(credits, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    args = [a.split("=", 1) for a in sys.argv[2:] if a != "--pick"]
    (pick if "--pick" in sys.argv else fetch)(target, args)
