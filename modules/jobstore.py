"""
jobstore.py
Short-lived server-side storage for a user's in-progress report.

Streamlit kept the fetched DataFrame and generated files in `session_state`.
Flask has no equivalent, and this data is far too large for a cookie, so each
run gets a job directory under the system temp dir, referenced by an opaque id
held in the user's session.

Disk-backed (rather than an in-memory dict) so it survives across gunicorn
workers and process restarts. Jobs older than TTL_HOURS are pruned on access.
"""

from __future__ import annotations

import pickle
import shutil
import time
import uuid
from pathlib import Path
from tempfile import gettempdir

import pandas as pd

ROOT = Path(gettempdir()) / "sprint-report-jobs"
TTL_SECONDS = 6 * 60 * 60  # 6 hours


def _job_dir(job_id: str) -> Path:
    # Guard against path traversal - ids we mint are hex only.
    safe = "".join(ch for ch in str(job_id) if ch.isalnum())
    return ROOT / safe


def new_job() -> str:
    job_id = uuid.uuid4().hex
    _job_dir(job_id).mkdir(parents=True, exist_ok=True)
    return job_id


def exists(job_id: str) -> bool:
    return bool(job_id) and _job_dir(job_id).is_dir()


def save_dataframe(job_id: str, df: pd.DataFrame, source: str) -> None:
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    df.to_pickle(d / "data.pkl")
    (d / "source.txt").write_text(source, encoding="utf-8")


def load_dataframe(job_id: str):
    path = _job_dir(job_id) / "data.pkl"
    if not path.exists():
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def source_label(job_id: str) -> str:
    path = _job_dir(job_id) / "source.txt"
    return path.read_text(encoding="utf-8") if path.exists() else "data"


def save_files(job_id: str, files: dict) -> None:
    """files: {'Excel': (filename, bytes, mime), ...}"""
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    meta = {}
    for fmt, (filename, data, mime) in files.items():
        blob = d / f"{fmt}.bin"
        blob.write_bytes(data)
        meta[fmt] = (filename, mime)
    (d / "files.pkl").write_bytes(pickle.dumps(meta))


def file_meta(job_id: str) -> dict:
    path = _job_dir(job_id) / "files.pkl"
    if not path.exists():
        return {}
    try:
        return pickle.loads(path.read_bytes())
    except Exception:
        return {}


def load_file(job_id: str, fmt: str):
    """-> (filename, bytes, mime) or None."""
    meta = file_meta(job_id).get(fmt)
    blob = _job_dir(job_id) / f"{fmt}.bin"
    if not meta or not blob.exists():
        return None
    filename, mime = meta
    return filename, blob.read_bytes(), mime


def save_object(job_id: str, name: str, obj) -> None:
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.pkl").write_bytes(pickle.dumps(obj))


def load_object(job_id: str, name: str):
    path = _job_dir(job_id) / f"{name}.pkl"
    if not path.exists():
        return None
    try:
        return pickle.loads(path.read_bytes())
    except Exception:
        return None


def clear_files(job_id: str) -> None:
    """Drop generated artifacts (kept data) so the report is rebuilt."""
    d = _job_dir(job_id)
    for path in list(d.glob("*.bin")) + [d / "files.pkl", d / "parsed.pkl"]:
        if path.exists():
            path.unlink()


def prune(ttl_seconds: int = TTL_SECONDS) -> None:
    if not ROOT.is_dir():
        return
    cutoff = time.time() - ttl_seconds
    for d in ROOT.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
