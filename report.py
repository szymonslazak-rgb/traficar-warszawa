"""Builds the offline report: one self-contained HTML page with three tables.

Everything the page shows is computed from the files under data/ plus the Bolt numbers in
data/bolt.json. The page embeds the per-day numbers as JSON and recalculates the tables in the
browser when the viewer picks another period, so it needs no server.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")
ORDER_MIN_MINUTES = 5
ORDER_MIN_KM = 0.3
# First full day collected without gaps under the current counting rule; earlier days are kept
# in data/ but left out of the comparison.
COMPARE_FROM = "2026-11-27"
TEMPLATE = Path(__file__).with_name("report_template.html")


def is_order(trip: dict) -> bool:
    """A car left the map and came back at least 5 minutes later somewhere else.

    Judged from the stored minutes and distance rather than the stored kind, so trips
    recorded under an older rule are counted the same way as new ones.
    """
    try:
        return float(trip["minutes"]) >= ORDER_MIN_MINUTES and float(trip["distance_km"]) >= ORDER_MIN_KM
    except (KeyError, TypeError, ValueError):
        return False


_cache: dict[tuple[str, int, int], list[dict]] = {}


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _cache:
        with path.open(newline="", encoding="utf-8") as handle:
            _cache[key] = list(csv.DictReader(handle))
    return _cache[key]


def _day_files(root: Path, folder: str) -> set[str]:
    return {p.stem for p in (root / folder).glob("*.csv")}


def build_data(root: Path, bolt: dict, now: datetime) -> dict:
    today = now.astimezone(WARSAW).date().isoformat()
    days = sorted(_day_files(root, "reads") | _day_files(root, "trips"))

    first_read: datetime | None = None
    for day in days:
        reads = _rows(root / "reads" / f"{day}.csv")
        if reads:
            first_read = min(_parse(r["taken_at"]) for r in reads)
            break

    start_day = None
    if first_read is not None:
        local = first_read.astimezone(WARSAW)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = local.date() if local - midnight <= timedelta(minutes=10) else local.date() + timedelta(days=1)
        start_day = max(start_date.isoformat(), COMPARE_FROM)

    out_days = []
    for day in days:
        reads = [r for r in _rows(root / "reads" / f"{day}.csv") if r.get("ok") == "1"]
        trips = [t for t in _rows(root / "trips" / f"{day}.csv") if is_order(t)]
        by_model: dict[str, int] = {}
        for trip in trips:
            by_model[trip["model"]] = by_model.get(trip["model"], 0) + 1

        start = datetime.fromisoformat(day).replace(tzinfo=WARSAW)
        end = (datetime.fromisoformat(day) + timedelta(days=1)).replace(tzinfo=WARSAW)
        window_start = max(start.astimezone(timezone.utc), first_read or start)
        window_end = min(end.astimezone(timezone.utc), now)
        expected = max(1.0, (window_end - window_start).total_seconds() / 60)
        out_days.append(
            {
                "day": day,
                "reads": len(reads),
                "coverage": round(min(100.0, 100 * len(reads) / expected), 1),
                "free": round(sum(int(r["available"]) for r in reads) / len(reads), 2) if reads else None,
                "orders": len(trips),
                "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
            }
        )

    cars_path = root / "cars.json"
    cars = []
    if cars_path.exists():
        registry = json.loads(cars_path.read_text("utf-8"))
        cars = [
            {"model": c["model"], "kind": c["kind"], "electric": bool(c["electric"]), "first": c["first"], "last": c["last"]}
            for c in registry.values()
        ]

    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "today": today,
        "start": start_day,
        "days": out_days,
        "cars": cars,
        "bolt_orders": bolt.get("orders") or {},
        "bolt_orders_fetched_at": bolt.get("orders_fetched_at"),
        "bolt_latest_order": bolt.get("latest_order"),
        "bolt_fleet": bolt.get("fleet") or {},
        "errors": bolt.get("errors") or {},
    }


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.read_text("utf-8").replace("__DATA__", payload)


def write_report(root: Path, out: Path, bolt: dict, now: datetime) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(render(build_data(root, bolt, now)), "utf-8")
    tmp.replace(out)
