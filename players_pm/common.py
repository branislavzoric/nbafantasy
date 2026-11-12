"""Shared helpers for Players +/-: DB engine, schema, job log, polite HTTP with retries, name folding."""

import contextlib
import json
import os
import re
import time
import unicodedata

import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PM_DIR = os.path.join(PROJECT_DIR, "Players PM")
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_session = requests.Session()
_session.headers["User-Agent"] = UA
_last_call = {}


def engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set in .env")
    return create_engine(url)


def ensure_schema(eng):
    with open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8") as f:
        sql = f.read()
    with eng.begin() as conn:
        conn.exec_driver_sql(sql)


@contextlib.contextmanager
def job(eng, name, league=None):
    """Logs a run in props.job_runs. The body sets j['rows'] / j['message'] / j['status']."""
    with eng.begin() as conn:
        run_id = conn.execute(text("INSERT INTO props.job_runs (job, league) VALUES (:j, :l) RETURNING run_id"),
                              {"j": name, "l": league}).scalar()
    j = {"rows": None, "message": None, "status": "ok"}
    try:
        yield j
    except BaseException as e:
        j["status"], j["message"] = "failed", f"{type(e).__name__}: {e}"[:2000]
        raise
    finally:
        with eng.begin() as conn:
            conn.execute(text("""UPDATE props.job_runs SET finished_at = now(), status = :s, rows = :r,
                                 message = :m WHERE run_id = :id"""),
                         {"s": j["status"], "r": j["rows"], "m": j["message"], "id": run_id})


def get_json(url, params=None, min_gap=0.5, retries=4, host_key=None):
    """GET with a minimum gap per host (rate limit) and exponential backoff on errors/429/5xx."""
    key = host_key or re.sub(r"^https?://([^/]+).*", r"\1", url)
    for attempt in range(retries):
        wait = _last_call.get(key, 0) + min_gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[key] = time.time()
        try:
            r = _session.get(url, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt * 2)


def fold(s):
    """'Jokić, Nikola Jr.' -> ['jokic', 'nikola', 'jr']"""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).split()


SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def name_key(s):
    return " ".join(t for t in fold(s) if t not in SUFFIXES)
