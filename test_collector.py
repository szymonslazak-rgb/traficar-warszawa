import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from traficar_collector import Detector, Store

MODELS = {15: "Toyota Corolla"}
T0 = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


def car(car_id=1, lat=52.23, lng=21.01, fuel=50.0, rng=400):
    return {"id": car_id, "lat": str(lat), "lng": str(lng), "zoneId": 2, "modelId": 15,
            "regPlate": f"WE{car_id:05d}", "fuel": fuel, "range": rng, "available": True}


def minutes(n):
    return T0 + timedelta(minutes=n)


class DetectorTest(unittest.TestCase):
    def run_reads(self, reads):
        detector = Detector()
        trips = []
        for at, cars in reads:
            trips += detector.step(at, cars, MODELS)
        return detector, trips

    def test_trip_when_car_returns_elsewhere(self):
        reads = [(minutes(0), [car()])] + [(minutes(i), []) for i in range(1, 25)]
        reads.append((minutes(25), [car(lat=52.26, lng=21.05, fuel=45)]))
        _, trips = self.run_reads(reads)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["kind"], "przejazd")
        self.assertEqual(trips[0]["minutes"], 24.0)
        self.assertGreater(trips[0]["distance_km"], 3)

    def test_round_trip_counted_by_fuel(self):
        reads = [(minutes(0), [car()])] + [(minutes(i), []) for i in range(1, 40)]
        reads.append((minutes(40), [car(lat=52.2301, fuel=46)]))
        _, trips = self.run_reads(reads)
        self.assertEqual(trips[0]["kind"], "przejazd")

    def test_range_drop_alone_is_not_a_trip(self):
        reads = [(minutes(0), [car(rng=146)])] + [(minutes(i), []) for i in range(1, 15)]
        reads.append((minutes(15), [car(rng=88)]))
        _, trips = self.run_reads(reads)
        self.assertEqual(trips[0]["kind"], "rezerwacja")

    def test_cancelled_reservation_not_a_trip(self):
        reads = [(minutes(0), [car()])] + [(minutes(i), []) for i in range(1, 10)]
        reads.append((minutes(10), [car()]))
        _, trips = self.run_reads(reads)
        self.assertEqual(trips[0]["kind"], "rezerwacja")

    def test_trip_inside_read_gap(self):
        reads = [(minutes(0), [car()]), (minutes(30), [car(lat=52.30)])]
        _, trips = self.run_reads(reads)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["note"], "w luce odczytu")

    def test_no_trip_for_gps_jitter_without_gap(self):
        reads = [(minutes(0), [car()]), (minutes(1), [car(lat=52.2302)])]
        _, trips = self.run_reads(reads)
        self.assertEqual(trips, [])

    def test_state_round_trip(self):
        detector, _ = self.run_reads([(minutes(0), [car(), car(2)]), (minutes(1), [car()])])
        restored = Detector.from_json(detector.to_json())
        trips = restored.step(minutes(20), [car(), car(2, lat=52.3)], MODELS)
        self.assertEqual([t["kind"] for t in trips], ["przejazd"])


class StoreTest(unittest.TestCase):
    def test_summary_counts_trips_by_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            detector = Detector()
            store.add_read(minutes(0), 1, True)
            detector.step(minutes(0), [car()], MODELS)
            detector.step(minutes(1), [], MODELS)
            store.add_read(minutes(1), 0, True)
            store.add_trips(detector.step(minutes(30), [car(lat=52.3)], MODELS))
            store.add_read(minutes(30), 1, True)
            summary = store.write_summary(minutes(31))
            today = summary["days"][-1]
            self.assertEqual(today["trips"], 1)
            self.assertEqual(today["trips_by_model"], {"Toyota Corolla": 1})
            self.assertEqual(summary["periods"]["doba"]["avg_trips_per_day"], 1.0)


if __name__ == "__main__":
    unittest.main()
