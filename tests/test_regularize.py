import threading
import unittest
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app import main


def epoch_millis(year, month, day, hour, minute, second=0, millisecond=0):
    instant = datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        millisecond * 1000,
        tzinfo=timezone.utc,
    )
    return int(instant.timestamp() * 1000)


class FakeCollection:
    def __init__(self, documents, selection_barrier=None):
        self.documents = [deepcopy(document) for document in documents]
        self.selection_barrier = selection_barrier
        self.lock = threading.Lock()

    def find_one(self, query):
        with self.lock:
            for document in self.documents:
                if all(document.get(field) == value for field, value in query.items()):
                    selected = deepcopy(document)
                    break
            else:
                selected = None
        if "date" in query and self.selection_barrier:
            self.selection_barrier.wait(timeout=5)
        return selected

    def find_one_and_update(self, query, update, return_document=None):
        with self.lock:
            for document in self.documents:
                if not self._matches(document, query):
                    continue
                document.update(update["$set"])
                document.setdefault("history", []).append(update["$push"]["history"])
                return deepcopy(document)
        return None

    @staticmethod
    def _matches(document, query):
        for field, expected in query.items():
            if field == "$and":
                if not all(FakeCollection._matches(document, item) for item in expected):
                    return False
                continue
            if isinstance(expected, dict) and "$exists" in expected:
                exists = field in document
                if exists != expected["$exists"]:
                    return False
                if exists and "$eq" in expected and document[field] != expected["$eq"]:
                    return False
                continue
            if document.get(field) != expected:
                return False
        return True


class FakeDatabase:
    def __init__(self, employee, attendance):
        self.employees = FakeCollection([employee] if employee else [])
        self.attendance_logs = attendance


class RegularizationTests(unittest.TestCase):
    employee = {
        "emp_code": "EMP0001",
        "shift_start": "09:30",
        "shift_end": "18:30",
    }

    def record(self, **overrides):
        record = {
            "_id": "record-1",
            "emp_code": "EMP0001",
            "date": "2026-06-01",
            "status": "PRESENT",
            "punch_in": datetime(2026, 6, 1, 3, 40, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 10,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
            "unrelated": "preserve me",
        }
        record.update(overrides)
        return record

    def body(self, **overrides):
        fields = {
            "reason": "corrected badge time",
            "regularized_by": "hr.admin",
        }
        fields.update(overrides)
        return main.RegularizeIn(**fields)

    def invoke(self, employee, record, body):
        database = FakeDatabase(
            employee,
            FakeCollection([record] if record else []),
        )
        with patch.object(main, "db", database):
            result = main.regularize_attendance(
                "EMP0001",
                date(2026, 6, 1),
                body,
            )
        return result, database

    def test_updates_punch_and_recomputes_derived_values_and_audit(self):
        original = self.record()
        new_punch_in = epoch_millis(2026, 6, 1, 4, 45)
        new_punch_out = epoch_millis(2026, 6, 1, 14, 0)
        result, database = self.invoke(
            self.employee,
            original,
            self.body(punch_in=new_punch_in, punch_out=new_punch_out),
        )

        self.assertEqual(result["punch_in"], new_punch_in)
        self.assertEqual(result["punch_out"], new_punch_out)
        self.assertEqual(result["late_minutes"], 45)
        self.assertEqual(result["work_hours"], 9.25)
        self.assertEqual(result["overtime_minutes"], 60)
        self.assertFalse(result["half_day"])
        self.assertNotIn("_id", result)
        self.assertEqual(len(result["history"]), 1)
        changes = result["history"][0]["changes"]
        self.assertEqual(
            set(changes),
            {"punch_in", "punch_out", "late_minutes", "work_hours", "overtime_minutes"},
        )
        self.assertEqual(result["history"][0]["by"], "hr.admin")
        self.assertEqual(result["history"][0]["reason"], "corrected badge time")
        self.assertEqual(
            database.attendance_logs.documents[0]["unrelated"],
            "preserve me",
        )

    def test_status_absent_clears_punches_and_derived_values(self):
        closed = self.record(
            punch_out=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            work_hours=8.33,
            late_minutes=10,
        )
        result, _ = self.invoke(
            self.employee,
            closed,
            self.body(status="ABSENT"),
        )
        self.assertIsNone(result["punch_in"])
        self.assertIsNone(result["punch_out"])
        self.assertIsNone(result["work_hours"])
        self.assertEqual(result["late_minutes"], 0)
        self.assertEqual(result["overtime_minutes"], 0)
        self.assertFalse(result["half_day"])
        self.assertEqual(
            set(result["history"][0]["changes"]),
            {"status", "punch_in", "punch_out", "work_hours", "late_minutes"},
        )

    def test_preserves_previous_history_and_appends_one_entry(self):
        previous_history = [
            {
                "at": datetime(2026, 6, 2, tzinfo=timezone.utc),
                "by": "earlier.admin",
                "reason": "first correction",
                "changes": {"late_minutes": {"from": 20, "to": 10}},
            }
        ]
        record = self.record(history=previous_history)
        result, _ = self.invoke(
            self.employee,
            record,
            self.body(punch_in=epoch_millis(2026, 6, 1, 3, 45)),
        )

        self.assertEqual(len(result["history"]), 2)
        self.assertEqual(result["history"][0]["by"], "earlier.admin")
        self.assertEqual(result["history"][1]["by"], "hr.admin")

    def test_legacy_record_without_history_gets_one_new_entry(self):
        legacy = self.record()
        legacy.pop("history")
        legacy.pop("half_day")
        result, _ = self.invoke(
            self.employee,
            legacy,
            self.body(punch_in=epoch_millis(2026, 6, 1, 3, 45)),
        )
        self.assertEqual(len(result["history"]), 1)
        self.assertFalse(result["half_day"])

    def test_overnight_punch_in_must_map_to_shift_start_date(self):
        employee = {**self.employee, "shift_start": "22:00", "shift_end": "06:00"}
        record = self.record(
            punch_in=datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc),
            date="2026-06-01",
        )
        result, _ = self.invoke(
            employee,
            record,
            self.body(punch_in=epoch_millis(2026, 6, 2, 0, 29, 59)),
        )
        self.assertEqual(result["date"], "2026-06-01")
        self.assertEqual(result["punch_in"], epoch_millis(2026, 6, 2, 0, 29, 59))

    def test_overnight_punch_in_at_shift_end_belongs_to_current_date(self):
        employee = {**self.employee, "shift_start": "22:00", "shift_end": "06:00"}
        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                employee,
                self.record(),
                self.body(punch_in=epoch_millis(2026, 6, 2, 0, 30)),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_recomputed_values_follow_grace_overtime_and_half_up_boundaries(self):
        result, _ = self.invoke(
            self.employee,
            self.record(),
            self.body(
                punch_in=epoch_millis(2026, 6, 1, 4, 10, 1),
                punch_out=epoch_millis(2026, 6, 1, 13, 30),
            ),
        )
        self.assertEqual(result["late_minutes"], 10)
        self.assertEqual(result["overtime_minutes"], 30)

        result, _ = self.invoke(
            self.employee,
            self.record(),
            self.body(
                punch_in=epoch_millis(2026, 6, 1, 4, 0),
                punch_out=epoch_millis(2026, 6, 1, 8, 29, 59),
            ),
        )
        self.assertEqual(result["work_hours"], 4.5)
        self.assertFalse(result["half_day"])

    def test_rejects_punch_in_on_wrong_attendance_day(self):
        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                self.employee,
                self.record(),
                self.body(punch_in=epoch_millis(2026, 6, 2, 3, 45)),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_invalid_status_fields_duration_and_noop_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.body(status="HOLIDAY")
        with self.assertRaises(ValidationError):
            self.body(status=None)
        with self.assertRaises(ValidationError):
            self.body(punch_in=None)
        with self.assertRaises(ValidationError):
            self.body(punch_out=None)
        with self.assertRaises(ValidationError):
            self.body(punch_in=1_783_310_000)
        with self.assertRaises(ValidationError):
            self.body(reason="no")
        with self.assertRaises(ValidationError):
            self.body(regularized_by="")

        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                self.employee,
                self.record(),
                self.body(
                    status="LEAVE",
                    punch_in=epoch_millis(2026, 6, 1, 3, 45),
                ),
            )
        self.assertEqual(raised.exception.status_code, 422)

        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                self.employee,
                self.record(),
                self.body(
                    punch_in=epoch_millis(2026, 6, 1, 4, 0),
                    punch_out=epoch_millis(2026, 6, 1, 3, 59),
                ),
            )
        self.assertEqual(raised.exception.status_code, 422)

        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                self.employee,
                self.record(),
                self.body(),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_missing_employee_or_record_returns_404(self):
        with self.assertRaises(HTTPException) as raised:
            self.invoke(None, self.record(), self.body(status="WFH"))
        self.assertEqual(raised.exception.status_code, 404)

        with self.assertRaises(HTTPException) as raised:
            self.invoke(self.employee, None, self.body(status="WFH"))
        self.assertEqual(raised.exception.status_code, 404)

    def test_concurrent_regularizations_do_not_overwrite_history(self):
        barrier = threading.Barrier(2)
        database = FakeDatabase(
            self.employee,
            FakeCollection([self.record()], selection_barrier=barrier),
        )

        def attempt(punch_in):
            request = self.body(punch_in=punch_in)
            try:
                return main.regularize_attendance(
                    "EMP0001",
                    date(2026, 6, 1),
                    request,
                )
            except HTTPException as exc:
                return exc.status_code

        with patch.object(main, "db", database):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        attempt,
                        [
                            epoch_millis(2026, 6, 1, 3, 45),
                            epoch_millis(2026, 6, 1, 3, 50),
                        ],
                    )
                )

        self.assertEqual(results.count(409), 1, repr(results))
        self.assertEqual(len(database.attendance_logs.documents[0]["history"]), 1)


if __name__ == "__main__":
    unittest.main()
