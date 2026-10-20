"""Verify X / Instagram posts before embedding them on b3basketball.com.

    venv/Scripts/python.exe check_social.py <post url> [<post url> ...]

For each URL prints a ready-to-paste `social` entry (JSON) with the posting account, or ERROR if the post
is missing/private. X uses the free publish.x.com oEmbed endpoint; Instagram uses the public embed page
(no Meta token needed). Only embed posts from official accounts (league, clubs, the player's own account).
"""
import html
import json
import re
import sys
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124"  # a full browser UA gets the JS-only embed page


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def check_x(url):
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)", url)
    if not m:
        raise ValueError("not an X post URL")
    canon = f"https://x.com/{m[1]}/status/{m[2]}"
    data = json.loads(get("https://publish.x.com/oembed?omit_script=1&url=" + urllib.parse.quote(canon, safe="")))
    handle = data["author_url"].rstrip("/").rsplit("/", 1)[-1]
    body = re.search(r"<p[^>]*>(.*?)</p>", data["html"], re.S)
    date = re.findall(r"<a [^>]*>([^<]+)</a>\s*</blockquote>", data["html"])
    return {"platform": "x", "url": f"https://x.com/{handle}/status/{m[2]}", "account": "@" + handle,
            "name": data["author_name"], "text": re.sub(r"\s*pic\.twitter\.com/\S+", "", strip(body[1])) if body else "", "posted": date[0] if date else ""}


def check_instagram(url):
    m = re.search(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(p|reel|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError("not an Instagram post URL")
    kind = "reel" if m[1] in ("reel", "tv") else "p"
    canon = f"https://www.instagram.com/{kind}/{m[2]}/"
    page = get(canon + "embed/captioned/")
    user = re.search(r'class="UsernameText">([^<]+)', page)
    if not user:
        raise ValueError("post not available (deleted, private or embedding disabled)")
    cap = re.search(r'class="Caption">(.*?)</div>', page, re.S)
    text = strip(cap[1]) if cap else ""
    if text.startswith(user[1]):
        text = text[len(user[1]):].strip()
    text = re.sub(r"\s*View all [\d,]+ comments$", "", text)
    return {"platform": "instagram", "url": canon, "account": "@" + user[1], "name": user[1],
            "verified": "VerifiedSprite" in page, "text": text[:280], "posted": ""}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    ok = True
    for url in sys.argv[1:]:
        try:
            info = check_instagram(url) if "instagram.com" in url else check_x(url)
            print(json.dumps(info, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 — report and continue with the next URL
            ok = False
            print(f"ERROR {url}: {e}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
