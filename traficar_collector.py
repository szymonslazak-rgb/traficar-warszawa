"""Reads the Traficar map for Warsaw every minute and infers rentals.

The map lists only free cars. A car that drops off the map is booked; it counts as
a trip only when it comes back at least 5 minutes later somewhere else. Everything is
written as CSV and JSON under data/, so a GitHub Actions job can commit it and the next
job can resume. Each save also
rebuilds the offline report (docs/index.html) from those files and the Bolt numbers.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bolt import BoltData
from report import ORDER_MIN_KM, ORDER_MIN_MINUTES, is_order, write_report

API = "https://fioletowe.live/api/v1"
ZONE_ID = 2
WARSAW = ZoneInfo("Europe/Warsaw")
USER_AGENT = "Mozilla/5.0 (compatible; traficar-warszawa/1.0)"

RESERVATION_MAX_MIN = 20
GAP_MIN = 3
FORGET_AFTER = timedelta(days=7)
SUMMARY_DAYS = 35

TRIP_FIELDS = [
    "car_id",
    "reg_plate",
    "model",
    "start_at",
    "end_at",
    "minutes",
    "distance_km",
    "fuel_start",
    "fuel_end",
    "range_start",
    "range_end",
    "kind",
    "note",
]
READ_FIELDS = ["taken_at", "available", "ok"]

CARSHARING_TYPES = {1: "Osobowe", 2: "Dostawcze"}


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def warsaw_day(at: datetime) -> str:
    return at.astimezone(WARSAW).date().isoformat()


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_json(path: str) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        headers={
            "accept": "application/json",
            "referer": "https://fioletowe.live/",
            "user-agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def fetch_cars(carsharing_models: set[int]) -> list[dict]:
    """Free cars in Warsaw. Scooters and anything that is not a carsharing car are dropped."""
    body = get_json(f"/cars?zoneId={ZONE_ID}")
    cars = body.get("cars") or []
    unique: dict[int, dict] = {}
    for car in cars:
        if (
            car.get("zoneId") == ZONE_ID
            and car.get("available", True)
            and int(car["modelId"]) in carsharing_models
        ):
            unique[int(car["id"])] = car
    return list(unique.values())


def fetch_models() -> dict[int, dict]:
    body = get_json("/car-models")
    return {
        int(m["id"]): {
            "name": str(m["name"]),
            "type": int(m.get("type") or 0),
            "electric": bool(m.get("electric")),
        }
        for m in body.get("carModels") or []
    }


def carsharing_ids(models: dict[int, dict]) -> set[int]:
    return {mid for mid, meta in models.items() if meta["type"] in CARSHARING_TYPES}


class Registry:
    """Every car ever seen on the map, with the first and last day it was seen."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.cars: dict[str, dict] = {}
        if path.exists():
            self.cars = json.loads(path.read_text("utf-8"))

    def observe(self, at: datetime, cars: list[dict], models: dict[int, dict]) -> None:
        day = warsaw_day(at)
        for car in cars:
            meta = models.get(int(car["modelId"]))
            if meta is None:
                continue
            entry = self.cars.setdefault(
                str(car["id"]),
                {"model": meta["name"], "kind": CARSHARING_TYPES[meta["type"]], "electric": meta["electric"],
                 "first": day, "last": day},
            )
            entry["last"] = day

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cars, ensure_ascii=False, separators=(",", ":")), "utf-8")
        tmp.replace(self.path)


@dataclass
class Snapshot:
    lat: float
    lng: float
    fuel: float | None
    range_km: int | None
    model_id: int
    plate: str
    seen_at: str

    @classmethod
    def from_car(cls, car: dict, at: datetime) -> "Snapshot":
        return cls(
            lat=float(car["lat"]),
            lng=float(car["lng"]),
            fuel=None if car.get("fuel") is None else float(car["fuel"]),
            range_km=None if car.get("range") is None else int(car["range"]),
            model_id=int(car["modelId"]),
            plate=str(car["regPlate"]),
            seen_at=iso(at),
        )


def classify(minutes: float, distance_km: float) -> str:
    if minutes >= ORDER_MIN_MINUTES and distance_km >= ORDER_MIN_KM:
        return "przejazd"
    if minutes <= RESERVATION_MAX_MIN:
        return "rezerwacja"
    return "postoj"


class Detector:
    """Turns a sequence of map reads into trips. State survives restarts via to_json."""

    def __init__(self) -> None:
        self.available: dict[int, Snapshot] = {}
        self.missing: dict[int, dict] = {}
        self.last_read: str | None = None

    def step(self, at: datetime, cars: list[dict], models: dict[int, str]) -> list[dict]:
        trips: list[dict] = []
        gap = (
            self.last_read is not None
            and (at - parse_iso(self.last_read)) >= timedelta(minutes=GAP_MIN)
        )
        present: set[int] = set()

        for car in cars:
            car_id = int(car["id"])
            present.add(car_id)
            now = Snapshot.from_car(car, at)

            if car_id in self.missing:
                gone = self.missing.pop(car_id)
                before = Snapshot(**gone["snapshot"])
                start = parse_iso(gone["since"])
                minutes = (at - start).total_seconds() / 60
                distance = haversine_km(before.lat, before.lng, now.lat, now.lng)
                note = "start niepewny" if gone.get("uncertain") else ""
                trips.append(
                    self._trip(car_id, before, now, start, at, minutes, distance,
                               classify(minutes, distance), note, models)
                )
            elif car_id in self.available and gap:
                before = self.available[car_id]
                distance = haversine_km(before.lat, before.lng, now.lat, now.lng)
                start = parse_iso(before.seen_at)
                minutes = (at - start).total_seconds() / 60
                if classify(minutes, distance) == "przejazd":
                    trips.append(
                        self._trip(car_id, before, now, start, at, minutes, distance,
                                   "przejazd", "w luce odczytu", models)
                    )

            self.available[car_id] = now

        for car_id in [cid for cid in self.available if cid not in present]:
            snapshot = self.available.pop(car_id)
            self.missing[car_id] = {
                "since": iso(at),
                "uncertain": bool(gap),
                "snapshot": snapshot.__dict__,
            }

        cutoff = at - FORGET_AFTER
        for car_id in [cid for cid, m in self.missing.items() if parse_iso(m["since"]) < cutoff]:
            del self.missing[car_id]

        self.last_read = iso(at)
        return trips

    @staticmethod
    def _trip(car_id, before, after, start, end, minutes, distance, kind, note, models) -> dict:
        return {
            "car_id": car_id,
            "reg_plate": after.plate,
            "model": models.get(after.model_id, f"model {after.model_id}"),
            "start_at": iso(start),
            "end_at": iso(end),
            "minutes": round(minutes, 1),
            "distance_km": round(distance, 2),
            "fuel_start": before.fuel,
            "fuel_end": after.fuel,
            "range_start": before.range_km,
            "range_end": after.range_km,
            "kind": kind,
            "note": note,
        }

    def to_json(self) -> dict:
        return {
            "last_read": self.last_read,
            "available": {str(k): v.__dict__ for k, v in self.available.items()},
            "missing": {str(k): v for k, v in self.missing.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> "Detector":
        detector = cls()
        detector.last_read = data.get("last_read")
        detector.available = {int(k): Snapshot(**v) for k, v in (data.get("available") or {}).items()}
        detector.missing = {int(k): v for k, v in (data.get("missing") or {}).items()}
        return detector


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "state.json"
        (root / "trips").mkdir(parents=True, exist_ok=True)
        (root / "reads").mkdir(parents=True, exist_ok=True)

    def load_detector(self) -> Detector:
        if not self.state_path.exists():
            return Detector()
        return Detector.from_json(json.loads(self.state_path.read_text("utf-8")))

    def save_detector(self, detector: Detector) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(detector.to_json(), separators=(",", ":")), "utf-8")
        tmp.replace(self.state_path)

    def append(self, folder: str, fields: list[str], day: str, rows: list[dict]) -> None:
        if not rows:
            return
        path = self.root / folder / f"{day}.csv"
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if new:
                writer.writeheader()
            writer.writerows(rows)

    def add_trips(self, trips: list[dict]) -> None:
        by_day: dict[str, list[dict]] = {}
        for trip in trips:
            by_day.setdefault(warsaw_day(parse_iso(trip["start_at"])), []).append(trip)
        for day, rows in by_day.items():
            self.append("trips", TRIP_FIELDS, day, rows)

    def add_read(self, at: datetime, available: int, ok: bool) -> None:
        self.append(
            "reads", READ_FIELDS, warsaw_day(at),
            [{"taken_at": iso(at), "available": available, "ok": int(ok)}],
        )

    def write_summary(self, now: datetime) -> dict:
        today = now.astimezone(WARSAW).date()
        days = []
        for offset in range(SUMMARY_DAYS - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            reads = self._rows("reads", day)
            trips = self._rows("trips", day)
            if not reads and not trips:
                continue
            ok_reads = [r for r in reads if r.get("ok") == "1"]
            counted = [t for t in trips if is_order(t)]
            by_model: dict[str, int] = {}
            for trip in counted:
                by_model[trip["model"]] = by_model.get(trip["model"], 0) + 1
            expected = 1440 if day != today.isoformat() else max(
                1, int((now.astimezone(WARSAW) - now.astimezone(WARSAW).replace(
                    hour=0, minute=0, second=0, microsecond=0)).total_seconds() // 60)
            )
            days.append({
                "day": day,
                "trips": len(counted),
                "reservations": sum(1 for t in trips if t.get("kind") == "rezerwacja"),
                "trips_by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
                "avg_available": round(
                    sum(int(r["available"]) for r in ok_reads) / len(ok_reads), 1
                ) if ok_reads else None,
                "reads": len(ok_reads),
                "coverage_pct": round(min(100.0, 100 * len(ok_reads) / expected), 1),
            })

        summary = {"generated_at": iso(now), "zone": "Warszawa", "days": days,
                   "periods": self._periods(days, today.isoformat())}
        (self.root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), "utf-8"
        )
        return summary

    @staticmethod
    def _periods(days: list[dict], today: str) -> dict:
        out = {}
        finished = [d for d in days if d["day"] != today] or days
        for name, span in (("doba", 1), ("tydzien", 7), ("miesiac", 30)):
            window = finished[-span:] if span >
