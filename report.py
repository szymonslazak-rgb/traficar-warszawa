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
# Cars that leave the map for washing, charging or moving by staff. Taken off every day's count
# in proportion to how much of that day the data covers.
SERVICE_PER_DAY = 30
# Collection has run without gaps under the current counting rule since this moment. Orders of
# both companies before it stay in data/ but are left out of the comparison.
COMPARE_FROM = datetime(2026, 11, 26, 10, 15, tzinfo=WARSAW)
TEMPLATE = Path(__file__).with_name("report_template.html")


def is_order(trip: dict) -> bool:
    """A car that stayed off the map for at least 5 minutes, wherever it came back."""
    try:
        return float(trip["minutes"]) >= ORDER_MIN_MINUTES
    except (KeyError, TypeError, ValueError):
        return False


def pending_orders(root: Path, now: datetime, count_from: datetime) -> list[dict]:
    """Cars still off the map for at least 5 minutes. They count on the day they left."""
    path = root / "state.json"
    if not path.exists():
        return []
    missing = json.loads(path.read_text("utf-8")).get("missing") or {}
    models: dict[str, str] = {}
    cars_path = root / "cars.json"
    if cars_path.exists():
        models = {cid: c.get("model") for cid, c in json.loads(cars_path.read_text("utf-8")).items()}
    out = []
    for car_id, entry in missing.items():
        since = _parse(entry["since"])
        if since < count_from or now - since < timedelta(minutes=ORDER_MIN_MINUTES):
            continue
        out.append({
            "since": since,
            "day": since.astimezone(WARSAW).date().isoformat(),
            "model": models.get(car_id) or "Nieznany",
        })
    return out


def take_off_service(by_model: dict[str, int], service: int) -> dict[str, int]:
    """Removes service disappearances from the models in proportion to their share."""
    total = sum(by_model.values())
    service = min(service, total)
    if service <= 0 or total <= 0:
        return dict(by_model)
    shares = {m: n * service / total for m, n in by_model.items()}
    cut = {m: int(v) for m, v in shares.items()}
    for m in sorted(shares, key=lambda k: shares[k] - cut[k], reverse=True)[: service - sum(cut.values())]:
        cut[m] += 1
    left = {m: n - cut[m] for m, n in by_model.items()}
    return {m: n for m, n in sorted(left.items(), key=lambda kv: -kv[1]) if n > 0}


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
    pending = pending_orders(root, now, COMPARE_FROM)
    days = sorted(_day_files(root, "reads") | _day_files(root, "trips") | {p["day"] for p in pending})

    first_read: datetime | None = None
    for day in days:
        reads = _rows(root / "reads" / f"{day}.csv")
        if reads:
            first_read = min(_parse(r["taken_at"]) for r in reads)
            break

    start_day = None
    count_from = COMPARE_FROM
    if first_read is not None:
        local = first_read.astimezone(WARSAW)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if local - midnight > timedelta(minutes=10):
            midnight += timedelta(days=1)
        count_from = max(midnight, COMPARE_FROM)
        start_day = count_from.astimezone(WARSAW).date().isoformat()

    out_days = []
    last_read: datetime | None = None
    for day in days:
        reads = [
            r for r in _rows(root / "reads" / f"{day}.csv")
            if r.get("ok") == "1" and _parse(r["taken_at"]) >= count_from
        ]
        if reads:
            last_read = _parse(reads[-1]["taken_at"])
        trips = [
            t for t in _rows(root / "trips" / f"{day}.csv")
            if is_order(t) and _parse(t["start_at"]) >= count_from
        ]
        by_model: dict[str, int] = {}
        for trip in trips:
            by_model[trip["model"]] = by_model.get(trip["model"], 0) + 1
        for order in (p for p in pending if p["day"] == day and p["since"] >= count_from):
            by_model[order["model"]] = by_model.get(order["model"], 0) + 1

        start = datetime.fromisoformat(day).replace(tzinfo=WARSAW)
        end = (datetime.fromisoformat(day) + timedelta(days=1)).replace(tzinfo=WARSAW)
        window_start = max(start.astimezone(timezone.utc), first_read or start, count_from)
        window_end = min(end.astimezone(timezone.utc), now)
        expected = max(1.0, (window_end - window_start).total_seconds() / 60)
        by_model = take_off_service(by_model, round(SERVICE_PER_DAY * min(1.0, expected / 1440)))
        out_days.append(
            {
                "day": day,
                "reads": len(reads),
                "coverage": round(min(100.0, 100 * len(reads) / expected), 1),
                "free": round(sum(int(r["available"]) for r in reads) / len(reads), 2) if reads else None,
                "orders": sum(by_model.values()),
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
        "count_from": count_from.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "traficar_until": last_read.astimezone(timezone.utc).isoformat(timespec="seconds") if last_read else None,
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
