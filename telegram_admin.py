"""B3 admin bot (@b3basketball_bot): approve, skip and post content from Telegram.

Runs as a long-polling loop (no webhook, so it works behind the home router):
  venv/Scripts/pythonw.exe telegram_admin.py

It serves one person only (TELEGRAM_ADMIN_CHAT_ID in .env) and works on the existing queues:
  content/                 news posts and cards       -> n8n 8DEfqsR5VIAwy56c (single post)
  content/carousels/       box scores, bets, rounds   -> n8n nbaCarouselWf001
  content/nightly/         nightly top-10 cards       -> n8n nightlyTop10Wf01
  content/reels/           reels                      -> n8n nbaReelPostWf001
each with pending/ -> approved/ -> posted/ (skipped items go to skipped/).

Commands: /review [queue] · /status · /post <queue> · /postall <queue> [minutes] · /approveall <queue> · /help
Alerts it sends by itself: new items waiting in pending, IG token expiry (IG_TOKEN_EXPIRES=YYYY-MM-DD in .env,
warned 7 days ahead), and a 23:00 summary of what was posted today.
"""

import datetime as dt
import glob
import html
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import requests
from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_DIR, ".env"))
API = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/"
ADMIN = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
STATE_FILE = os.path.join(PROJECT_DIR, "telegram_state.json")
LOG_FILE = os.path.join(PROJECT_DIR, "logs", "telegram_admin.log")
CONTAINER = "n8ndocker-n8n-1"
DOCKER_DESKTOP = os.path.expandvars(r"%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe")

QUEUES = {  # name -> (folder, n8n workflow id, label)
    "news": ("content", "8DEfqsR5VIAwy56c", "📰 News post"),
    "carousels": ("content/carousels", "nbaCarouselWf001", "🗂 Carousel"),
    "nightly": ("content/nightly", "nightlyTop10Wf01", "🌙 Nightly card"),
    "reels": ("content/reels", "nbaReelPostWf001", "🎬 Reel"),
}

lock = threading.Lock()  # state + file moves are shared with the /postall thread


# ---------------------------------------------------------------- helpers

def log(msg):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"offset": 0, "announced": [], "ids": {}, "next_id": 1, "alerts": {}}


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


state = load_state()


def tg(method, files=None, **body):
    try:
        if files:
            r = requests.post(API + method, data={k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                                  for k, v in body.items()}, files=files, timeout=120)
        else:
            r = requests.post(API + method, json=body, timeout=70)
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"{method} failed: {e}")
        return None
    if not d.get("ok"):
        log(f"{method} error: {d}")
        if d.get("error_code") == 409:
            log("another bot instance is polling; exiting")
            sys.exit(1)
    return d.get("result")


def send(text, buttons=None, chat=None):
    body = {"chat_id": chat or ADMIN, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        body["reply_markup"] = keyboard(buttons)
    return tg("sendMessage", **body)


def keyboard(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def qdir(queue, stage):
    return os.path.join(PROJECT_DIR, QUEUES[queue][0], stage)


def items(queue, stage):
    """Slugs in a queue stage, sorted (the order n8n posts them in)."""
    return sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(qdir(queue, stage), "*.json")))


def meta(queue, stage, slug):
    with open(os.path.join(qdir(queue, stage), slug + ".json"), encoding="utf-8") as f:
        return json.load(f)


def item_files(queue, stage, slug):
    m = meta(queue, stage, slug)
    names = {slug + ".json"} | set(m.get("images", []))
    names |= {m[k] for k in ("image", "video") if m.get(k)}
    names |= {os.path.basename(p) for p in glob.glob(os.path.join(qdir(queue, stage), glob.escape(slug) + ".*"))}
    return sorted(names)


def move(queue, slug, src, dst):
    with lock:
        if not os.path.exists(os.path.join(qdir(queue, src), slug + ".json")):
            return False
        os.makedirs(qdir(queue, dst), exist_ok=True)
        for name in item_files(queue, src, slug):
            p = os.path.join(qdir(queue, src), name)
            if os.path.exists(p):
                shutil.move(p, os.path.join(qdir(queue, dst), name))
        return True


def short_id(queue, slug):
    """callback_data is limited to 64 bytes, so buttons carry a short number instead of the slug."""
    with lock:
        for k, v in state["ids"].items():
            if v == [queue, slug]:
                return k
        k = str(state["next_id"])
        state["next_id"] += 1
        state["ids"][k] = [queue, slug]
        if len(state["ids"]) > 2000:  # keep the file small
            for old in sorted(state["ids"], key=int)[:500]:
                del state["ids"][old]
        save_state()
        return k


# ---------------------------------------------------------------- previews

def preview(queue, slug, pos=None):
    m = meta(queue, "pending", slug)
    d = qdir(queue, "pending")
    try:
        if m.get("images"):
            imgs = m["images"][:10]
            media = [{"type": "photo", "media": f"attach://f{i}"} for i in range(len(imgs))]
            files = {f"f{i}": open(os.path.join(d, n), "rb") for i, n in enumerate(imgs)}
            tg("sendMediaGroup", files=files, chat_id=ADMIN, media=media)
        elif m.get("video"):
            p = os.path.join(d, m["video"])
            if os.path.getsize(p) < 49_000_000:
                tg("sendVideo", files={"video": open(p, "rb")}, chat_id=ADMIN)
        elif m.get("image"):
            tg("sendPhoto", files={"photo": open(os.path.join(d, m["image"]), "rb")}, chat_id=ADMIN)
    except OSError as e:
        log(f"preview {slug}: {e}")
    k = short_id(queue, slug)
    head = f"{QUEUES[queue][2]}{f' · {pos}' if pos else ''}\n<b>{html.escape(slug)}</b>\n\n"
    send(head + html.escape(m.get("caption", "")[:3200]),
         [[("✅ Approve", f"a|{k}"), ("❌ Skip", f"s|{k}")], [("📤 Approve & post now", f"p|{k}")]])


def review(queue=None):
    qs = [queue] if queue else list(QUEUES)
    total = sum(len(items(q, "pending")) for q in qs)
    if not total:
        return send("Nothing waiting in pending. ✅")
    send(f"👀 {total} item(s) to review")
    for q in qs:
        slugs = items(q, "pending")
        for i, s in enumerate(slugs, 1):
            preview(q, s, f"{i}/{len(slugs)}")
    send("That's all. Approve everything that's left: " +
         " · ".join(f"/approveall_{q}" for q in qs if items(q, "pending")))


# ---------------------------------------------------------------- posting

def docker_ok():
    try:
        r = subprocess.run(["docker", "ps", "--filter", f"name={CONTAINER}", "--format", "{{.Status}}"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and r.stdout.strip().startswith("Up")
    except (OSError, subprocess.TimeoutExpired):
        return False


def post_next(queue):
    """Run the queue's n8n workflow once (it posts the first approved item). Returns a status line."""
    before = items(queue, "approved")
    if not before:
        return f"Nothing approved in <b>{queue}</b>."
    if not docker_ok():
        if os.path.exists(DOCKER_DESKTOP):
            subprocess.Popen([DOCKER_DESKTOP])
        return "⚠️ Docker / n8n isn't running. I'm starting Docker Desktop, so try again in 1–2 minutes."
    slug = before[0]
    try:
        r = subprocess.run(["docker", "exec", "-e", "N8N_RUNNERS_BROKER_PORT=5690", CONTAINER, "n8n", "execute",
                            f"--id={QUEUES[queue][1]}"], capture_output=True, text=True, timeout=900,
                           encoding="utf-8", errors="replace")
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        out = "timeout"
    done = slug not in items(queue, "approved") and os.path.exists(os.path.join(qdir(queue, "posted"), slug + ".json"))
    log(f"post {queue} {slug}: {'ok' if done else 'FAILED'}")
    if done:
        return f"📤 Posted: <b>{html.escape(slug)}</b>"
    return f"❌ Posting failed: <b>{html.escape(slug)}</b>\n<pre>{html.escape(out[-1200:])}</pre>"


def post_all(queue, minutes):
    n = len(items(queue, "approved"))
    send(f"📤 Posting {n} item(s) from <b>{queue}</b>, one every {minutes} min.")
    while items(queue, "approved"):
        msg = post_next(queue)
        send(msg)
        if msg.startswith(("❌", "⚠️")):
            send("Stopped. Fix the problem, then run /postall again.")
            return
        if items(queue, "approved"):
            time.sleep(minutes * 60)
    send(f"✅ <b>{queue}</b>: all approved items posted.")


# ---------------------------------------------------------------- status & alerts

def status():
    lines = ["<b>B3 status</b>", ""]
    today = dt.date.today()
    for q in QUEUES:
        posted_today = sum(1 for p in glob.glob(os.path.join(qdir(q, "posted"), "*.json"))
                           if dt.date.fromtimestamp(os.path.getmtime(p)) == today)
        lines.append(f"{QUEUES[q][2]}: {len(items(q, 'pending'))} pending · {len(items(q, 'approved'))} approved · "
                     f"{posted_today} posted today")
    lines += ["", f"n8n / Docker: {'🟢 running' if docker_ok() else '🔴 not running'}"]
    logs = sorted(glob.glob(r"C:\Users\Zoric\Desktop\b3basketball-site\daily\logs\*.log"), key=os.path.getmtime)
    if logs:
        with open(logs[-1], encoding="utf-8", errors="replace") as f:
            ends = [l for l in f if l.startswith("=== ") and " end " in l]
        lines.append(f"Last daily run: {os.path.basename(logs[-1])} {ends[-1].split(' end ')[-1].strip() if ends else '(running or no end line)'}")
    exp = os.getenv("IG_TOKEN_EXPIRES")
    if exp:
        lines.append(f"Instagram token expires: {exp}")
    return "\n".join(lines)


def periodic():
    """New-item notices, IG token warning, nightly summary. Called after every poll (≤ ~50 s apart)."""
    new = []
    for q in QUEUES:
        for s in items(q, "pending"):
            key = f"{q}/{s}"
            if key not in state["announced"]:
                new.append(q)
                state["announced"].append(key)
    if new:
        state["announced"] = state["announced"][-3000:]
        save_state()
        counts = {q: new.count(q) for q in dict.fromkeys(new)}
        send("🆕 New content waiting: " + ", ".join(f"{n} {q}" for q, n in counts.items()),
             [[("👀 Review now", "r|all")]])
    now = dt.datetime.now()
    today = now.date().isoformat()
    exp = os.getenv("IG_TOKEN_EXPIRES")
    if exp and state["alerts"].get("ig") != today:
        try:
            left = (dt.date.fromisoformat(exp) - now.date()).days
        except ValueError:
            left = None
        if left is not None and left <= 7:
            send(f"⚠️ The Instagram token expires in <b>{left} day(s)</b> ({exp}). Refresh it in n8n, "
                 "then update IG_TOKEN_EXPIRES in .env.")
            state["alerts"]["ig"] = today
            save_state()
    if now.hour == 23 and state["alerts"].get("summary") != today:
        state["alerts"]["summary"] = today
        save_state()
        send("🌙 " + status())


# ---------------------------------------------------------------- updates

HELP = """<b>B3 admin bot</b>
/review – show everything waiting for approval
/review_news · /review_carousels · /review_nightly · /review_reels
/status – queues, n8n, last daily run
/post_news (or _carousels, _nightly, _reels) – post the next approved item now
/postall_news [minutes] – post all approved, spaced (default 5 min)
/approveall_news (etc.) – approve everything pending in a queue
/help"""


def handle_command(text):
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lstrip("/"), parts[1:]
    base, _, q = cmd.partition("_")
    q = q or (args[0] if args and args[0] in QUEUES else "")
    if q and q not in QUEUES:
        return send(f"Unknown queue <b>{html.escape(q)}</b>. Use: {', '.join(QUEUES)}")
    if base in ("start", "help"):
        send(HELP)
    elif base == "review":
        review(q or None)
    elif base == "status":
        send(status())
    elif base == "post":
        if not q:
            return send("Which queue? /post_news · /post_carousels · /post_nightly · /post_reels")
        send("⏳ Posting…")
        send(post_next(q))
    elif base == "postall":
        if not q:
            return send("Which queue? /postall_news · /postall_carousels · /postall_nightly · /postall_reels")
        mins = next((int(a) for a in args if a.isdigit()), 5)
        threading.Thread(target=post_all, args=(q, mins), daemon=True).start()
    elif base == "approveall":
        if not q:
            return send("Which queue? /approveall_news · /approveall_carousels · /approveall_nightly · /approveall_reels")
        moved = [s for s in items(q, "pending") if move(q, s, "pending", "approved")]
        send(f"✅ Approved {len(moved)} item(s) in <b>{q}</b>. Post them with /postall_{q}")
    else:
        send("Unknown command. /help")


def handle_callback(cb):
    action, _, k = cb["data"].partition("|")
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    msg = cb.get("message", {})
    if action == "r":
        return review()
    if k not in state["ids"]:
        return send("That button is too old. Use /review again.")
    queue, slug = state["ids"][k]
    if action in ("a", "p"):
        ok = move(queue, slug, "pending", "approved")
        result = "✅ Approved" if ok else "Already handled"
    else:
        ok = move(queue, slug, "pending", "skipped")
        result = "❌ Skipped" if ok else "Already handled"
    if msg:
        tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           reply_markup={"inline_keyboard": [[{"text": result, "callback_data": "x|0"}]]})
    log(f"{result} {queue}/{slug}")
    if action == "p" and ok:
        if items(queue, "approved")[:1] != [slug]:
            return send(f"Approved. Other items are ahead of it in <b>{queue}</b>, so use /post_{queue} "
                        f"to post them in order.")
        send("⏳ Posting…")
        send(post_next(queue))


def main():
    log("bot started")
    if not ADMIN:
        print("TELEGRAM_ADMIN_CHAT_ID is not set: send /start to the bot and check the log for your id.")
    while True:
        ups = tg("getUpdates", offset=state["offset"], timeout=50, allowed_updates=["message", "callback_query"])
        if ups is None:
            time.sleep(5)
            continue
        for u in ups:
            state["offset"] = u["update_id"] + 1
            save_state()
            chat = (u.get("message") or u.get("callback_query", {}).get("message") or {}).get("chat", {})
            sender = (u.get("message") or u.get("callback_query") or {}).get("from", {})
            if str(sender.get("id")) != ADMIN or chat.get("type") != "private":
                log(f"ignored update from {sender.get('id')} {sender.get('username')} ({chat.get('type')})")
                if chat.get("type") == "private" and "message" in u:
                    send("This is a private bot.", chat=chat["id"])
                continue
            try:
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                elif u["message"].get("text", "").startswith("/"):
                    handle_command(u["message"]["text"])
            except Exception as e:  # keep the bot alive; report the problem
                log(f"error: {e!r}")
                send(f"⚠️ Error: <code>{html.escape(repr(e))[:500]}</code>")
        if ADMIN:
            periodic()


if __name__ == "__main__":
    main()
