import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException

from app import main


def epoch_millis(year, month, day, hour, minute, second=0):
    instant = datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        tzinfo=timezone.utc,
    )
    return int(instant.timestamp() * 1000)


class FakeCollection:
    def __init__(self, documents, selection_barrier=None):
        self.documents = [dict(document) for document in documents]
        self.selection_barrier = selection_barrier
        self.lock = threading.Lock()

    def find_one(self, query, sort=None):
        if "punch_in" in query and self.selection_barrier:
            self.selection_barrier.wait(timeout=5)
        with self.lock:
            matches = []
            for document in self.documents:
                if "_id" in query and document.get("_id") != query["_id"]:
                    continue
                if "emp_code" in query and document.get("emp_code") != query["emp_code"]:
                    continue
                punch_in_filter = query.get("punch_in")
                if punch_in_filter:
                    punch_in = document.get("punch_in")
                    if not isinstance(punch_in, datetime):
                        continue
                    if punch_in >= punch_in_filter["$lt"]:
                        continue
                matches.append(document)
            if sort:
                for field, direction in reversed(sort):
                    matches.sort(
                        key=lambda document: document[field],
                        reverse=direction < 0,
                    )
            return dict(matches[0]) if matches else None

    def find_one_and_update(self, query, update, return_document=None):
        with self.lock:
            for document in self.documents:
                if document.get("_id") != query["_id"]:
                    continue
                if document.get("emp_code") != query["emp_code"]:
                    continue
                if document.get("punch_out") is not None:
                    return None
                document.update(update["$set"])
                return dict(document)
        return None


class FakeDatabase:
    def __init__(self, employee, attendance):
        self.employees = FakeCollection([employee] if employee else [])
        self.attendance_logs = attendance


class PunchOutTests(unittest.TestCase):
    employee = {
        "emp_code": "EMP0001",
        "shift_start": "09:00",
        "shift_end": "17:00",
    }

    def record(self, punch_in, attendance_date="2026-06-01", punch_out=None):
        return {
            "_id": "record-1",
            "emp_code": "EMP0001",
            "date": attendance_date,
            "status": "PRESENT",
            "punch_in": punch_in,
            "punch_out": punch_out,
            "work_hours": None,
            "late_minutes": 12,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

    def invoke(self, employee, attendance, punched_at):
        database = FakeDatabase(employee, attendance)
        with patch.object(main, "db", database):
            return main.punch_out(
                main.PunchOutIn(
                    emp_code="EMP0001",
                    punched_at=punched_at,
                )
            )

    def test_normal_punch_out_computes_fields_and_returns_contract_shape(self):
        punch_in = datetime(2026, 6, 1, 3, 30)
        result = self.invoke(
            self.employee,
            FakeCollection([self.record(punch_in)]),
            epoch_millis(2026, 6, 1, 12, 30),
        )

        self.assertEqual(result["work_hours"], 9.0)
        self.assertEqual(result["overtime_minutes"], 60)
        self.assertFalse(result["half_day"])
        self.assertEqual(result["late_minutes"], 12)
        self.assertEqual(result["punch_out"], epoch_millis(2026, 6, 1, 12, 30))
        self.assertEqual(
            set(result),
            {
                "emp_code",
                "date",
                "status",
                "punch_in",
                "punch_out",
                "work_hours",
                "late_minutes",
                "overtime_minutes",
                "half_day",
                "history",
            },
        )

    def test_selects_most_recent_eligible_punch_in(self):
        older = self.record(datetime(2026, 6, 1, 2, 30))
        newer = self.record(datetime(2026, 6, 1, 3, 30))
        result = self.invoke(
            self.employee,
            FakeCollection([older, newer]),
            epoch_millis(2026, 6, 1, 12, 30),
        )
        self.assertEqual(result["punch_in"], epoch_millis(2026, 6, 1, 3, 30))

    def test_overnight_shift_uses_next_calendar_day_for_shift_end(self):
        employee = {**self.employee, "shift_start": "22:00", "shift_end": "06:00"}
        record = self.record(datetime(2026, 6, 1, 17, 0))
        result = self.invoke(
            employee,
            FakeCollection([record]),
            epoch_millis(2026, 6, 2, 1, 0),
        )

        self.assertEqual(result["work_hours"], 8.0)
        self.assertEqual(result["overtime_minutes"], 30)
        self.assertEqual(result["date"], "2026-06-01")

    def test_overtime_threshold_and_work_hour_half_up_boundary(self):
        record = self.record(datetime(2026, 6, 1, 3, 30))
        result = self.invoke(
            self.employee,
            FakeCollection([record]),
            epoch_millis(2026, 6, 1, 7, 48),
        )
        self.assertEqual(result["work_hours"], 4.3)
        self.assertEqual(result["overtime_minutes"], 0)
        self.assertTrue(result["half_day"])

        record = self.record(datetime(2026, 6, 1, 3, 30))
        result = self.invoke(
            self.employee,
            FakeCollection([record]),
            epoch_millis(2026, 6, 1, 12, 0),
        )
        self.assertEqual(result["overtime_minutes"], 30)

    def test_half_up_rounding_and_exact_half_day_cutoff(self):
        record = self.record(datetime(2026, 6, 1, 3, 30))
        result = self.invoke(
            self.employee,
            FakeCollection([record]),
            epoch_millis(2026, 6, 1, 4, 30, 18),
        )
        self.assertEqual(result["work_hours"], 1.01)
        self.assertTrue(result["half_day"])

        record = self.record(datetime(2026, 6, 1, 3, 30))
        result = self.invoke(
            self.employee,
            FakeCollection([record]),
            epoch_millis(2026, 6, 1, 8, 0),
        )
        self.assertEqual(result["work_hours"], 4.5)
        self.assertFalse(result["half_day"])

    def test_punch_out_truncates_milliseconds_and_overtime_threshold(self):
        record = self.record(datetime(2026, 6, 1, 3, 30, 0, 900000))
        result = self.invoke(
            self.employee,
            FakeCollection([record]),
            epoch_millis(2026, 6, 1, 11, 59, 59) + 999,
        )
        self.assertEqual(result["punch_out"], epoch_millis(2026, 6, 1, 11, 59, 59))
        self.assertEqual(result["punch_in"], epoch_millis(2026, 6, 1, 3, 30))
        self.assertEqual(result["overtime_minutes"], 0)

    def test_missing_punch_out_field_is_treated_as_open_record(self):
        record = self.record(datetime(2026, 6, 1, 3, 30))
        record.pop("punch_out")
        result = self.invoke(
            self.employee,
            FakeCollection([record]),
            epoch_millis(2026, 6, 1, 12, 30),
        )
        self.assertEqual(result["punch_out"], epoch_millis(2026, 6, 1, 12, 30))

    def test_exact_truncated_punch_in_boundary_is_selected_then_rejected_as_zero_duration(self):
        record = self.record(datetime(2026, 6, 1, 3, 30, 0, 900000))
        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                self.employee,
                FakeCollection([record]),
                epoch_millis(2026, 6, 1, 3, 30),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_repeated_punch_out_returns_conflict(self):
        record = self.record(
            datetime(2026, 6, 1, 3, 30),
            punch_out=datetime(2026, 6, 1, 12, 0),
        )
        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                self.employee,
                FakeCollection([record]),
                epoch_millis(2026, 6, 1, 12, 30),
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_invalid_duration_and_missing_record_errors(self):
        record = self.record(datetime(2026, 6, 1, 3, 30))
        for punch_time in (
            epoch_millis(2026, 6, 1, 3, 30),
            epoch_millis(2026, 6, 2, 3, 31),
        ):
            with self.subTest(punch_time=punch_time):
                with self.assertRaises(HTTPException) as raised:
                    self.invoke(
                        self.employee,
                        FakeCollection([record]),
                        punch_time,
                    )
                self.assertEqual(raised.exception.status_code, 422)

        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                self.employee,
                FakeCollection([]),
                epoch_millis(2026, 6, 1, 12, 30),
            )
        self.assertEqual(raised.exception.status_code, 404)

    def test_exact_24_hour_duration_is_allowed(self):
        record = self.record(datetime(2026, 6, 1, 3, 30))
        result = self.invoke(
            self.employee,
            FakeCollection([record]),
            epoch_millis(2026, 6, 2, 3, 30),
        )
        self.assertEqual(result["work_hours"], 24.0)

    def test_unknown_employee_returns_not_found(self):
        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                None,
                FakeCollection([]),
                epoch_millis(2026, 6, 1, 12, 30),
            )
        self.assertEqual(raised.exception.status_code, 404)

    def test_concurrent_punch_outs_have_one_success_and_one_conflict(self):
        barrier = threading.Barrier(2)
        record = self.record(datetime(2026, 6, 1, 3, 30))
        database = FakeDatabase(
            self.employee,
            FakeCollection([record], selection_barrier=barrier),
        )
        request = main.PunchOutIn(
            emp_code="EMP0001",
            punched_at=epoch_millis(2026, 6, 1, 12, 30),
        )

        def attempt_punch_out():
            try:
                with patch.object(main, "db", database):
                    return main.punch_out(request).get("punch_out")
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt_punch_out(), range(2)))

        self.assertEqual(results.count(409), 1)
        self.assertEqual(
            results.count(epoch_millis(2026, 6, 1, 12, 30)),
            1,
        )


if __name__ == "__main__":
    unittest.main()
