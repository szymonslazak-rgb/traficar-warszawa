import csv
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import bolt
import report
from report import build_data, is_order, render
from traficar_collector import Detector, Registry, classify

WARSAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)

SHEET = (
    "Created Time,Registration Number,Order State\n"
    '11/22/2026 10:00:00,WE12345,finished\n'
    '11/22/2026 11:00:00,we99999,finished\n'
    '11/22/2026 11:30:00,WE33333,started\n'
    '11/22/2026 12:00:00,WE11111,cancelled\n'
    '11/22/2026 13:00:00,WE22222,commerce_failed\n'
    '11/22/2026 14:00:00,GA12345,finished\n'
    '11/23/2026 1:05:09,WA00001,finished\n'
    '11/23/2026 2:00:00,WA00002,booked\n'
)


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_data(root: Path) -> None:
    for offset in range(1, 7):
        day = (NOW.astimezone(WARSAW).date() - timedelta(days=offset)).isoformat()
        start = datetime.fromisoformat(day).replace(tzinfo=WARSAW)
        reads = [
            {"taken_at": (start + timedelta(minutes=m)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
             "available": 100 + (m % 2) * 20, "ok": 1}
            for m in range(1440)
        ]
        write_csv(root / "reads" / f"{day}.csv", ["taken_at", "available", "ok"], reads)

        def trip(model, minutes, km, kind):
            return {"model": model, "start_at": reads[0]["taken_at"], "minutes": minutes, "distance_km": km, "kind": kind}

        trips = (
            [trip("RENAULT Clio V", 25, 3.2, "przejazd") for _ in range(30)]
            + [trip("DACIA Dokker", 5, 0.3, "przejazd") for _ in range(10)]
            # Not orders: cancelled reservation, back in place with less fuel, moved in under 5 minutes.
            + [trip("DACIA Dokker", 8, 0.0, "rezerwacja") for _ in range(7)]
            + [trip("RENAULT Clio V", 40, 0.1, "przejazd") for _ in range(3)]
            + [trip("RENAULT Clio V", 4.9, 2.0, "przejazd") for _ in range(2)]
        )
        write_csv(root / "trips" / f"{day}.csv", ["model", "start_at", "minutes", "distance_km", "kind"], trips)
    first = (NOW.astimezone(WARSAW).date() - timedelta(days=10)).isoformat()
    last = NOW.astimezone(WARSAW).date().isoformat()
    cars = {str(i): {"model": "RENAULT Clio V", "kind": "Osobowe", "electric": False, "first": first, "last": last} for i in range(8)}
    cars.update({str(100 + i): {"model": "DACIA Dokker", "kind": "Dostawcze", "electric": False, "first": first, "last": last} for i in range(2)})
    (root / "cars.json").write_text(json.dumps(cars), "utf-8")


class BoltSheetTest(unittest.TestCase):
    def test_counts_finished_orders_per_day(self):
        per_day, latest = bolt.aggregate_sheet(SHEET)
        self.assertEqual(per_day, {"2026-11-22": 3, "2026-11-23": 1})
        self.assertEqual(latest, "11/23/2026 1:05:09")

    def test_missing_columns_is_an_error(self):
        with self.assertRaises(ValueError):
            bolt.aggregate_sheet("a,b\n1,2\n")


class BoltFleetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_fleet_is_the_fixed_number_for_each_day_with_orders(self):
        data = bolt.BoltData(self.tmp / "bolt.json")
        with mock.patch.object(bolt, "download_sheet", return_value=SHEET):
            data.refresh()
        snapshot = data.snapshot()
        self.assertEqual(snapshot["orders"], {"2026-11-22": 3, "2026-11-23": 1})
        self.assertEqual(snapshot["fleet"], {"2026-11-22": 490, "2026-11-23": 490})
        self.assertEqual(bolt.BOLT_FLEET, 490)

    def test_failed_download_is_recorded_and_retried_later(self):
        data = bolt.BoltData(self.tmp / "bolt.json")
        with mock.patch.object(bolt, "download_sheet", side_effect=urllib.error.URLError("offline")):
            data.refresh()
        snapshot = data.snapshot()
        self.assertEqual(snapshot["fleet"], {})
        self.assertIn("offline", snapshot["errors"]["orders"])
        self.assertFalse(data._due(NOW))


class RegistryTest(unittest.TestCase):
    def test_scooter_models_never_get_a_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(Path(tmp) / "cars.json")
            models = {15: {"name": "Toyota", "type": 1, "electric": False}, 99: {"name": "Scooter", "type": 6, "electric": True}}
            from traficar_collector import carsharing_ids

            self.assertEqual(carsharing_ids(models), {15})
            registry.observe(NOW, [{"id": 1, "modelId": 15}], models)
            self.assertEqual(registry.cars["1"]["kind"], "Osobowe")
            registry.observe(NOW + timedelta(days=1), [{"id": 1, "modelId": 15}], models)
            self.assertEqual(registry.cars["1"]["first"], "2026-11-23")
            self.assertEqual(registry.cars["1"]["last"], "2026-11-24")


class OrderRuleTest(unittest.TestCase):
    def test_order_needs_five_minutes_off_the_map_and_another_place(self):
        self.assertEqual(classify(5, 0.3), "przejazd")
        self.assertEqual(classify(4.9, 5.0), "rezerwacja")
        self.assertEqual(classify(60, 0.29), "postoj")
        self.assertTrue(is_order({"minutes": "5.0", "distance_km": "0.3"}))
        self.assertFalse(is_order({"minutes": "45", "distance_km": "0.1", "kind": "przejazd"}))
        self.assertFalse(is_order({"kind": "przejazd"}))

    def test_car_moved_during_a_read_gap_counts_only_after_five_minutes(self):
        detector = Detector()
        car = {"id": 1, "modelId": 15, "lat": 52.2297, "lng": 21.0122, "fuel": 50, "range": 400, "regPlate": "WE1"}
        moved = dict(car, lat=52.2397)
        detector.step(NOW, [car], {15: "Toyota"})
        self.assertEqual(detector.step(NOW + timedelta(minutes=4), [moved], {15: "Toyota"}), [])
        trips = detector.step(NOW + timedelta(minutes=10), [car], {15: "Toyota"})
        self.assertEqual([t["kind"] for t in trips], ["przejazd"])


class ReportTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(report, "COMPARE_FROM", "2000-01-01")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        make_data(self.tmp)
        self.data = build_data(
            self.tmp,
            {"orders": {"2026-11-22": 700, "2026-11-21": 600}, "fleet": {"2026-11-22": 1000}, "errors": {}},
            NOW,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_daily_numbers_count_only_trips(self):
        day = self.data["days"][-1]
        self.assertEqual(day["orders"], 40)
        self.assertEqual(day["by_model"], {"RENAULT Clio V": 30, "DACIA Dokker": 10})
        self.assertEqual(day["free"], 110.0)
        self.assertEqual(day["coverage"], 100.0)

    def test_start_is_the_first_full_day_of_readings(self):
        self.assertEqual(self.data["start"], "2026-11-17")
        partial = Path(tempfile.mkdtemp())
        try:
            make_data(partial)
            first = partial / "reads" / "2026-11-17.csv"
            with first.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))[758:]
            write_csv(first, ["taken_at", "available", "ok"], rows)
            self.assertEqual(build_data(partial, {}, NOW)["start"], "2026-11-18")
        finally:
            shutil.rmtree(partial)

    def test_comparison_never_starts_before_the_fixed_start(self):
        with mock.patch.object(report, "COMPARE_FROM", "2026-11-20"):
            self.assertEqual(build_data(self.tmp, {}, NOW)["start"], "2026-11-20")
        with mock.patch.object(report, "COMPARE_FROM", "2026-11-10"):
            self.assertEqual(build_data(self.tmp, {}, NOW)["start"], "2026-11-17")

    def test_tables_ignore_days_before_the_common_start(self):
        html = render(self.data)
        start = html.index('<script id="calc">') + len('<script id="calc">')
        calc_js = html[start : html.index("</script>", start)]
        data = dict(self.data, start="2026-11-22", bolt_orders={"2026-11-20": 500, "2026-11-22": 700})
        script = (
            calc_js
            + f"\nconst D = {json.dumps(data)};\nconst c = createCalc(D);"
            + """
            const r = c.groupRows('2026-11-17', '2026-11-23', 'day');
            const before = c.groupRows('2026-11-17', '2026-11-20', 'day');
            console.log(JSON.stringify({ from: r.from, days: r.rows.length, total: r.total,
              empty: before.rows.length, presets: c.presets().map(p => p.from) }));
            """
        )
        try:
            result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            self.skipTest("node is not installed")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["from"], "2026-11-22")
        self.assertEqual(out["days"], 2)
        self.assertEqual(out["total"]["boltOrders"], 700)
        self.assertEqual(out["total"]["trOrders"], 40)
        self.assertEqual(out["empty"], 0)
        self.assertTrue(all(f >= "2026-11-22" for f in out["presets"]))

    def test_html_embeds_the_data_and_is_self_contained(self):
        html = render(self.data)
        self.assertIn('"bolt_fleet":{"2026-11-22":1000}', html)
        self.assertNotIn("__DATA__", html)
        self.assertNotIn("https://", html.split('id="calc"')[0].split("<style>")[1])

    def test_tables_calculate_in_the_browser_code(self):
        html = render(self.data)
        start = html.index('<script id="calc">') + len('<script id="calc">')
        calc_js = html[start : html.index("</script>", start)]
        script = (
            calc_js
            + f"\nconst D = {json.dumps(self.data)};\nconst c = createCalc(D);"
            + """
            const orders = c.groupRows('2026-11-17', '2026-11-23', 'day');
            const week = c.groupRows('2026-11-16', '2026-11-22', 'week');
            const models = c.models('2026-11-17', '2026-11-22');
            console.log(JSON.stringify({
              today: orders.rows[orders.rows.length - 1].partial,
              total: orders.total, week: week.rows.map(r => [r.label, r.trOrders]),
              models: models.rows.map(m => [m.model, m.cars, m.orders, m.perCarDay]), mtotal: models.total
            }));
            """
        )
        try:
            result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            self.skipTest("node is not installed")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertTrue(out["today"])
        total = out["total"]
        self.assertEqual(total["trOrders"], 240)  # 6 closed days x 40
        self.assertEqual(total["trFleet"], 10)
        self.assertEqual(total["trPerCarDay"], 4)
        self.assertEqual(total["boltOrders"], 1300)
        self.assertEqual(total["boltFleet"], 1000)
        self.assertAlmostEqual(total["boltPerCarDay"], 0.65)
        self.assertEqual(out["week"][0][1], 240)
        self.assertEqual(out["models"][0], ["RENAULT Clio V", 8, 180, 180 / (6 * 8)])
        self.assertEqual(out["models"][1], ["DACIA Dokker", 2, 60, 60 / (6 * 2)])
        self.assertEqual(out["mtotal"]["orders"], 240)


if __name__ == "__main__":
    unittest.main()
