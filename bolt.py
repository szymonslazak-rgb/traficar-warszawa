"""Bolt side of the report: orders from the public Google Sheet and fleet size from Databricks.

Orders are read from the sheet at most once an hour (it refreshes hourly anyway). The fleet
size is read from Databricks once per Warsaw day, from 09:00 on. Both run in a background
thread so a slow download never delays a map read. Results live in data/bolt.json.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")
SHEET_ID = "1V_xgPiWUly6tz_qTjfubQWW0Y5sejYrg9v37hhD11w4"
ORDERS_GID = "1458960516"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={ORDERS_GID}"

WARSAW_PREFIXES = {"WA", "WB", "WD", "WE", "WF", "WG", "WH", "WI", "WJ", "WK", "WN", "WT", "WU", "WW", "WX", "WY", "WZ"}
COUNTED_STATES = {"finished", "started", "booked", "paused"}

ORDERS_TTL_S = 3600
RETRY_S = 600
FLEET_HOUR = 9
STATEMENT_TIMEOUT_S = 90

_TIME = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d{2}):(\d{2})$")


def aggregate_sheet(text: str) -> tuple[dict[str, int], str | None]:
    """Counts Warsaw orders per day and returns the newest order time as it appears in the sheet."""
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
        if row[plate_i].strip().upper()[:2] not in WARSAW_PREFIXES:
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


def databricks_settings(env: dict[str, str] | None = None) -> dict[str, str] | None:
    env = os.environ if env is None else env
    values = {
        "host": env.get("DATABRICKS_HOST", "").strip().rstrip("/"),
        "token": env.get("DATABRICKS_TOKEN", "").strip(),
        "warehouse": env.get("DATABRICKS_WAREHOUSE_ID", "").strip(),
        "sql": env.get("DATABRICKS_FLEET_SQL", "").strip(),
    }
    if not all(values.values()):
        return None
    if not (values["host"].startswith("https://") or values["host"].startswith("http://127.0.0.1")):
        return None
    return values


def _databricks_call(settings: dict[str, str], method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{settings['host']}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "authorization": f"Bearer {settings['token']}",
            "accept": "application/json",
            **({"content-type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Databricks odpowiedzial kodem {error.code}") from None


def databricks_fleet_size(settings: dict[str, str], poll_s: float = 3.0) -> int:
    if not re.match(r"^\s*(select|with)\b", settings["sql"], re.IGNORECASE):
        raise RuntimeError("DATABRICKS_FLEET_SQL musi zaczynac sie od SELECT lub WITH")
    reply = _databricks_call(
        settings,
        "POST",
        "/api/2.0/sql/statements/",
        {
            "warehouse_id": settings["warehouse"],
            "statement": settings["sql"],
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        },
    )
    deadline = time.monotonic() + STATEMENT_TIMEOUT_S
    while (reply.get("status") or {}).get("state") in ("PENDING", "RUNNING"):
        if not reply.get("statement_id") or time.monotonic() > deadline:
            raise RuntimeError("zapytanie do Databricks trwa za dlugo")
        time.sleep(poll_s)
        reply = _databricks_call(settings, "GET", f"/api/2.0/sql/statements/{reply['statement_id']}")
    state = (reply.get("status") or {}).get("state")
    if state != "SUCCEEDED":
        raise RuntimeError(f"zapytanie do Databricks nie powiodlo sie ({state or 'brak statusu'})")
    try:
        cell = reply["result"]["data_array"][0][0]
        cars = int(float(cell))
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("pierwsza kolumna pierwszego wiersza z Databricks nie jest liczba aut") from None
    if cars < 0 or cars > 100_000:
        raise RuntimeError("liczba aut z Databricks jest poza zakresem")
    return cars


def warsaw_day(at: datetime) -> str:
    return at.astimezone(WARSAW).date().isoformat()


def warsaw_minutes(at: datetime) -> int:
    local = at.astimezone(WARSAW)
    return local.hour * 60 + local.minute


class BoltData:
    """Bolt numbers on disk, refreshed from a background thread."""

    def __init__(self, path: Path, env: dict[str, str] | None = None) -> None:
        self.path = path
        self.env = env
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.last_try = {"orders": 0.0, "fleet": 0.0}
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

    def _due_orders(self, now: datetime) -> bool:
        fetched = self.data.get("orders_fetched_at")
        age = (now - datetime.fromisoformat(fetched)).total_seconds() if fetched else float("inf")
        return age >= ORDERS_TTL_S and time.monotonic() - self.last_try["orders"] >= RETRY_S

    def _due_fleet(self, now: datetime) -> bool:
        return (
            databricks_settings(self.env) is not None
            and warsaw_minutes(now) >= FLEET_HOUR * 60
            and warsaw_day(now) not in self.data["fleet"]
            and time.monotonic() - self.last_try["fleet"] >= RETRY_S
        )

    def poke(self, now: datetime) -> None:
        """Starts a refresh in the background when something is due. Never blocks."""
        if self.thread is not None and self.thread.is_alive():
            return
        with self.lock:
            orders_due, fleet_due = self._due_orders(now), self._due_fleet(now)
        if not (orders_due or fleet_due):
            return
        self.thread = threading.Thread(target=self.refresh, args=(now, orders_due, fleet_due), daemon=True)
        self.thread.start()

    def refresh(self, now: datetime, orders_due: bool = True, fleet_due: bool = True) -> None:
        if orders_due:
            self.last_try["orders"] = time.monotonic()
            try:
                per_day, latest = aggregate_sheet(download_sheet())
                with self.lock:
                    self.data.update(
                        orders=per_day,
                        latest_order=latest,
                        orders_fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )
                    self.data["errors"].pop("orders", None)
            except (OSError, ValueError, urllib.error.URLError) as error:
                with self.lock:
                    self.data["errors"]["orders"] = str(error)[:200]
        if fleet_due:
            self.last_try["fleet"] = time.monotonic()
            settings = databricks_settings(self.env)
            try:
                if settings is None:
                    raise RuntimeError("brak ustawien Databricks")
                cars = databricks_fleet_size(settings)
                with self.lock:
                    self.data["fleet"][warsaw_day(now)] = cars
                    self.data["errors"].pop("fleet", None)
            except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
                with self.lock:
                    self.data["errors"]["fleet"] = str(error)[:200]
        self.save()

    def join(self) -> None:
        if self.thread is not None:
            self.thread.join(timeout=STATEMENT_TIMEOUT_S + 60)
