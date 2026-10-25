"""
store.py
Persistent storage for saved sprint details and per-project report configs
(buckets, KPIs, status mapping).

Data persists to a **Zoho Sheet** when Zoho credentials are configured (see
modules/config.py), and falls back to local JSON files under data/ otherwise, so
local development works with no credentials. The Zoho Sheet is the source of
truth in a deployment - a container's filesystem does not survive a redeploy.

Design note - the Zoho backend is intentionally **append-only** and uses only
the two Zoho Sheet operations that are unambiguously documented,
`worksheet.records.fetch` and `worksheet.records.add`. It never relies on the
fragile update / delete-by-criteria API:

  * every save appends a row  (store, project, data, updated_at)
  * on load, the newest row per (store, project) wins
  * a delete appends a tombstone row (data = "__deleted__")
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"

_TOMBSTONE = "__deleted__"
_FETCH_PAGE = 1000
# How long a fetched copy of the worksheet may be reused. Long enough to
# collapse the many reads a single page load makes, short enough that a second
# replica sees another's write quickly. Writes invalidate it immediately.
_CACHE_SECONDS = float(os.environ.get("STORE_CACHE_SECONDS", "30"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Zoho Sheet treats the updated_at cell as a date and hands it back reformatted
# in the sheet's locale (e.g. "13/8/2026 3:56:46.958 PM") rather than the ISO
# string we wrote. Comparing those as text is meaningless - "9:00 AM" sorts
# after "10:00 AM" - so every timestamp is parsed into a real datetime before
# deciding which row is newest.
_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %I:%M:%S.%f %p", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p",
    "%d/%m/%Y %H:%M:%S.%f", "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S.%f %p", "%m/%d/%Y %I:%M:%S %p",
)
_TS_MIN = datetime.min


def _parse_ts(value) -> datetime:
    """Best-effort parse of an updated_at cell; _TS_MIN when unrecognised so the
    row loses to any row with a readable timestamp (row order breaks the tie)."""
    text = str(value or "").strip()
    if not text:
        return _TS_MIN
    normalized = " ".join(text.replace(" ", " ").split())
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return _TS_MIN


class LocalFileBackend:
    """Stores each logical store as a dict[project] -> obj in a JSON file."""

    def _path(self, store: str) -> Path:
        return DATA_DIR / f"{store}.local.json"

    def load_all(self, store: str) -> dict:
        path = self._path(store)
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, store: str, data: dict) -> None:
        path = self._path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(path)

    def save_one(self, store: str, project: str, obj) -> None:
        data = self.load_all(store)
        data[project] = obj
        self._write(store, data)

    def delete_one(self, store: str, project: str) -> None:
        data = self.load_all(store)
        data.pop(project, None)
        self._write(store, data)


class ZohoSheetBackend:
    """Append-only persistence backed by a single Zoho Sheet worksheet."""

    def __init__(self, cfg: dict):
        self.client_id = cfg["client_id"]
        self.client_secret = cfg["client_secret"]
        self.refresh_token = cfg["refresh_token"]
        self.resource_id = cfg["resource_id"]
        self.worksheet = cfg.get("worksheet_name", "app_data")
        dc = str(cfg.get("location", "com")).strip().lstrip(".") or "com"
        self.accounts_host = cfg.get("accounts_domain") or f"https://accounts.zoho.{dc}"
        self.sheet_host = cfg.get("sheet_domain") or f"https://sheet.zoho.{dc}"
        self._token = None
        self._token_exp = 0.0
        self._records = None
        self._records_exp = 0.0

    def _access_token(self) -> str:
        import requests

        if self._token and time.time() < self._token_exp - 60:
            return self._token
        resp = requests.post(
            f"{self.accounts_host}/oauth/v2/token",
            data={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        # raise_for_status() would throw away Zoho's response body, which is the
        # only part that says *why* (invalid_client vs invalid_code vs a
        # mismatched client) - so surface it instead of a bare "400".
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Zoho token request failed (HTTP {resp.status_code}): {(resp.text or '')[:300]}")
        body = resp.json()
        if "access_token" not in body:
            raise RuntimeError(f"Zoho token error: {body}")
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _api(self, method: str, extra: dict) -> dict:
        import requests

        headers = {"Authorization": f"Zoho-oauthtoken {self._access_token()}"}
        data = {"method": method, "worksheet_name": self.worksheet}
        data.update(extra)
        resp = requests.post(
            f"{self.sheet_host}/api/v2/{self.resource_id}",
            headers=headers,
            data=data,
            timeout=30,
        )
        try:
            body = resp.json()
        except ValueError:
            body = None
        failed = resp.status_code >= 400 or (
            isinstance(body, dict) and body.get("status") == "failure"
        )
        if failed:
            detail = json.dumps(body) if body is not None else (resp.text or "")[:400]
            raise RuntimeError(f"Zoho Sheet {method} failed (HTTP {resp.status_code}): {detail}")
        return body or {}

    def _fetch_records(self) -> list:
        """Every row in the worksheet, briefly cached.

        A fetch returns the whole sheet and `load_all` filters it by store name
        in Python, so one fetch answers *every* store. Without a cache each of
        the ~20 read sites spent its own API call on identical data: a single
        settings page cost half a dozen, and the scheduler's tick several a
        minute, which is what exhausted the daily quota (error_code 2950).

        The TTL is deliberately short, and any write drops the cache, so the
        claim-then-verify the scheduler relies on still reads fresh rows.
        """
        if self._records is not None and time.time() < self._records_exp:
            return self._records
        records = []
        start = 1
        while True:
            body = self._api(
                "worksheet.records.fetch",
                {"records_start_index": str(start), "count": str(_FETCH_PAGE)},
            )
            page = body.get("records", []) or []
            records.extend(page)
            if len(page) < _FETCH_PAGE:
                break
            start += len(page)
        self._records = records
        self._records_exp = time.time() + _CACHE_SECONDS
        return records

    def _add_record(self, row: dict) -> None:
        self._api("worksheet.records.add", {"json_data": json.dumps([row])})
        # The sheet has changed; serving the pre-write rows would hide the save
        # from the next read - including the scheduler's own claim check.
        self._records = None
        self._records_exp = 0.0

    def load_all(self, store: str) -> dict:
        """Newest row per (store, project) wins - decided by POSITION in the
        sheet, not by updated_at.

        The store only ever appends, so the last row for a key is the newest.
        updated_at cannot be trusted for ordering: Zoho rewrites the cell in the
        sheet's own locale, and observed values come back out of sequence
        relative to the rows they belong to (a later save can carry an earlier
        stamp), which silently resurrected older settings. The timestamp is kept
        for humans reading the sheet, not for sorting.
        """
        latest: dict[str, tuple] = {}
        for idx, rec in enumerate(self._fetch_records()):
            if rec.get("store") != store:
                continue
            project = rec.get("project")
            if not project:
                continue
            if project not in latest or idx >= latest[project][0]:
                latest[project] = (idx, rec.get("data", ""))
        out = {}
        for project, (_, raw) in latest.items():
            if raw in (_TOMBSTONE, "", None):
                continue
            try:
                out[project] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def save_one(self, store: str, project: str, obj) -> None:
        self._add_record(
            {"store": store, "project": project,
             "data": json.dumps(obj), "updated_at": _now_iso()}
        )

    def delete_one(self, store: str, project: str) -> None:
        self._add_record(
            {"store": store, "project": project,
             "data": _TOMBSTONE, "updated_at": _now_iso()}
        )


_BACKEND = None


def get_backend(reload: bool = False):
    """Zoho Sheet when configured, else local JSON files. Cached per process."""
    global _BACKEND
    if _BACKEND is None or reload:
        cfg = config.section("zoho")
        _BACKEND = ZohoSheetBackend(cfg) if cfg else LocalFileBackend()
    return _BACKEND


def backend_kind() -> str:
    """'ZohoSheetBackend' or 'LocalFileBackend' - for a status indicator."""
    return type(get_backend()).__name__
