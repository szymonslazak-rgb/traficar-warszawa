"""Bolt side of the report: orders from the public Google Sheet, fleet as a fixed number.

Orders are read from the sheet every 15 minutes, in a background thread, so a slow
download never delays a map read. Results live in data/bolt.json.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from report import COMPARE_FROM

SHEET_ID = "1JBw3vbYue_LEH_Boc4Gna6IvzAD4kyUHNHGwyr5lXE0"
ORDERS_GID = "1458960516"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={ORDERS_GID}"

COUNTED_STATES = {"finished"}

BOLT_FLEET = 490
ORDERS_TTL_S = 900
RETRY_S = 300

_TIME = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d{2}):(\d{2})$")


def aggregate_sheet(text: str, since: datetime | None = None) -> tuple[dict[str, int], str | None]:
    """Counts finished orders per day and returns the newest order time as it appears in the sheet.

    Created Time is Warsaw local time; orders created before `since` are skipped.
    """
    cutoff = since.replace(tzinfo=None) if since is not None else None
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0] if rows else []
    try:
        time_i = header.index("Created Time")
        plate_i = header.index("Registration Number")
        state_i = header.index("Order State")
    except ValueError as error:
        raise ValueError("w arkuszu Bolta brakuje kolumn czasu, rejestracji albo statusu") from error

    per_day: dict[str, int] = {}
    latest: tuple[datetime, str] | None = None
    for row in rows[1:]:
        if len(row) <= max(time_i, plate_i, state_i):
            continue
        if row[state_i].strip() not in COUNTED_STATES:
            continue
        created = row[time_i].strip()
        match = _TIME.match(created)
        if not match:
            continue
        month, day, year, hour, minute, second = (int(part) for part in match.groups())
        try:
            stamp = datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue
        if cutoff is not None and stamp < cutoff:
            continue
        key = stamp.date().isoformat()
        per_day[key] = per_day.get(key, 0) + 1
        if latest is None or stamp >= latest[0]:
            latest = (stamp, created)
    return dict(sorted(per_day.items())), latest[1] if latest else None


def download_sheet() -> str:
    request = urllib.request.Request(
        SHEET_URL, headers={"accept": "text/csv", "user-agent": "Mozilla/5.0 (compatible; traficar-warszawa/1.0)"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    if not text.startswith("Created Time,"):
        raise ValueError("arkusz Bolta nie zwrocil tabeli zamowien")
    return text


class BoltData:
    """Bolt numbers on disk, refreshed from a background thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.last_try = 0.0
        self.data: dict = {"orders": {}, "orders_fetched_at": None, "latest_order": None, "fleet": {}, "errors": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text("utf-8")))
            except (OSError, ValueError):
                pass

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def save(self) -> None:
        with self.lock:
            payload = json.dumps(self.data, ensure_ascii=False, indent=1)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(self.path)

    def _due(self, now: datetime) -> bool:
        fetched = self.data.get("orders_fetched_at")
        age = (now - datetime.fromisoformat(fetched)).total_seconds() if fetched else float("inf")
        return age >= ORDERS_TTL_S and time.monotonic() - self.last_try >= RETRY_S

    def poke(self, now: datetime) -> None:
        """Starts a refresh in the background when one is due. Never blocks."""
        if self.thread is not None and self.thread.is_alive():
            return
        with self.lock:
            due = self._due(now)
        if not due:
            return
        self.thread = threading.Thread(target=self.refresh, daemon=True)
        self.thread.start()

    def refresh(self) -> None:
        self.last_try = time.monotonic()
        try:
            per_day, latest = aggregate_sheet(download_sheet(), COMPARE_FROM)
            with self.lock:
                self.data.update(
                    orders=per_day,
                    latest_order=latest,
                    orders_fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    fleet={day: BOLT_FLEET for day in per_day},
                )
                self.data["errors"].pop("orders", None)
        except (OSError, ValueError, urllib.error.URLError) as error:
            with self.lock:
                self.data["errors"]["orders"] = str(error)[:200]
        self.save()

    def join(self) -> None:
        if self.thread is not None:
            self.thread.join(timeout=120)
