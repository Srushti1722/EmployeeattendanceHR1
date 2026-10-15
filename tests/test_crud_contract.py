import unittest
import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import patch

from bson import ObjectId
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app import main


def epoch_millis(year, month, day, hour, minute):
    return int(
        datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=timezone.utc,
        ).timestamp()
        * 1000
    )


class FakeCursor:
    def __init__(self, documents):
        self.documents = list(documents)

    def sort(self, specification, direction=None):
        if isinstance(specification, str):
            specification = [(specification, direction)]
        for field, order in reversed(specification):
            self.documents.sort(
                key=lambda document: document.get(field),
                reverse=order < 0,
            )
        return self

    def skip(self, count):
        self.documents = self.documents[count:]
        return self

    def limit(self, count):
        self.documents = self.documents[:count]
        return self

    def __iter__(self):
        return iter(self.documents)


class FakeCollection:
    def __init__(self, documents, insert_error=None, assign_object_id=False):
        self.documents = [dict(document) for document in documents]
        self.inserted = None
        self.insert_error = insert_error
        self.assign_object_id = assign_object_id
        self.query = None
        self.sort_specification = None
        self.page_skip = None
        self.page_limit = None

    def find_one(self, query):
        return next(
            (
                dict(document)
                for document in self.documents
                if all(document.get(key) == value for key, value in query.items())
            ),
            None,
        )

    def find(self, query, projection=None):
        self.query = query
        matched = [
            dict(document)
            for document in self.documents
            if all(
                self._matches_value(document.get(key), value)
                for key, value in query.items()
            )
        ]
        if projection and projection.get("_id") == 0:
            for document in matched:
                document.pop("_id", None)
        return FakeCursor(matched)

    @staticmethod
    def _matches_value(actual, expected):
        if isinstance(expected, dict):
            return all(
                (actual >= value if operator == "$gte" else actual <= value)
                for operator, value in expected.items()
            )
        return actual == expected

    def count_documents(self, query):
        return sum(
            1
            for document in self.documents
            if all(
                self._matches_value(document.get(key), value)
                for key, value in query.items()
            )
        )

    def insert_one(self, document):
        if self.insert_error is not None:
            raise self.insert_error
        if self.assign_object_id:
            document["_id"] = ObjectId()
        self.inserted = dict(document)
        self.documents.append(self.inserted)

    def create_index(self, keys, unique=False):
        return (keys, unique)


class FakeDatabase:
    def __init__(
        self,
        employees,
        attendance,
        employee_insert_error=None,
        attendance_insert_error=None,
        assign_employee_object_id=False,
    ):
        self.employees = FakeCollection(
            employees,
            employee_insert_error,
            assign_employee_object_id,
        )
        self.attendance_logs = FakeCollection(attendance, attendance_insert_error)


class CrudContractTests(unittest.TestCase):
    def employee(self, code, department="Engineering"):
        return {
            "emp_code": code,
            "name": "Test Employee",
            "email": f"{code.lower()}@example.com",
            "department": department,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }

    def attendance(self, code, day, status="PRESENT"):
        return {
            "_id": f"{code}-{day}",
            "emp_code": code,
            "date": day,
            "status": status,
            "punch_in": datetime(2026, 6, 1, 3, 40, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 10,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

    def test_employee_validation_matches_contract_constraints(self):
        valid = {
            "emp_code": "EMP0001",
            "name": "Asha",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-02-28",
        }
        self.assertEqual(main.EmployeeIn(**valid).joined_on, "2026-02-28")
        for changes in (
            {"emp_code": "EMP123"},
            {"email": "not-an-email"},
            {"shift_start": "24:00"},
            {"shift_end": "22:00", "shift_start": "22:00"},
            {"joined_on": "2026-02-30"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                main.EmployeeIn(**(valid | changes))

    def test_employee_creation_returns_epoch_milliseconds(self):
        database = FakeDatabase([], [], assign_employee_object_id=True)
        body = main.EmployeeIn(
            emp_code="EMP0001",
            name="Asha",
            email="asha@example.com",
            department="Engineering",
            joined_on="2026-01-01",
        )
        with patch.object(main, "db", database), patch.object(
            main, "datetime"
        ) as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026, 1, 1, tzinfo=timezone.utc
            )
            result = main.create_employee(body)

        self.assertEqual(result["created_at"], epoch_millis(2026, 1, 1, 0, 0))
        self.assertNotIn("_id", result)
        expected_fields = {
            "emp_code",
            "name",
            "email",
            "department",
            "shift_start",
            "shift_end",
            "joined_on",
            "created_at",
        }
        self.assertEqual(set(result), expected_fields)
        json.dumps(jsonable_encoder(result))

    def test_employee_duplicate_precheck_and_insert_race_return_conflict(self):
        body = main.EmployeeIn(
            emp_code="EMP0001",
            name="Asha",
            email="asha@example.com",
            department="Engineering",
            joined_on="2026-01-01",
        )
        existing_database = FakeDatabase([self.employee("EMP0001")], [])
        with patch.object(main, "db", existing_database):
            with self.assertRaises(HTTPException) as raised:
                main.create_employee(body)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "emp_code already exists")

        race_database = FakeDatabase([], [], employee_insert_error=DuplicateKeyError("duplicate"))
        with patch.object(main, "db", race_database), patch.object(
            main, "datetime"
        ) as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026, 1, 1, tzinfo=timezone.utc
            )
            with self.assertRaises(HTTPException) as raised:
                main.create_employee(body)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "emp_code already exists")

    def test_employee_list_filters_counts_sorts_and_pages(self):
        database = FakeDatabase(
            [
                self.employee("EMP0003"),
                self.employee("EMP0001"),
                self.employee("EMP0002", "Sales"),
            ],
            [],
        )
        with patch.object(main, "db", database):
            result = main.list_employees("Engineering", page=2, page_size=1)

        self.assertEqual(result["total"], 2)
        self.assertEqual([item["emp_code"] for item in result["items"]], ["EMP0003"])
        self.assertEqual(result["page"], 2)
        self.assertTrue(all(isinstance(item["created_at"], int) for item in result["items"]))

    def test_punch_in_uses_ist_shift_day_and_returns_serialized_shape(self):
        employee = self.employee("EMP0001")
        database = FakeDatabase([employee], [])
        body = main.PunchInIn(
            emp_code="EMP0001",
            punched_at=epoch_millis(2026, 6, 2, 0, 0),
        )
        with patch.object(main, "db", database):
            result = main.punch_in(body)

        self.assertEqual(result["date"], "2026-06-02")
        self.assertEqual(result["punch_in"], body.punched_at)
        self.assertEqual(result["late_minutes"], 0)
        self.assertNotIn("_id", result)
        self.assertNotIn("id", result)

    def test_punch_in_overnight_post_midnight_belongs_to_previous_shift(self):
        employee = {
            **self.employee("EMP0001"),
            "shift_start": "22:00",
            "shift_end": "06:00",
        }
        database = FakeDatabase([employee], [])
        body = main.PunchInIn(
            emp_code="EMP0001",
            punched_at=epoch_millis(2026, 6, 2, 0, 0),
        )
        with patch.object(main, "db", database):
            result = main.punch_in(body)
        self.assertEqual(result["date"], "2026-06-01")

    def test_punch_in_unknown_employee_is_404_and_invalid_status_is_rejected(self):
        database = FakeDatabase([], [])
        with patch.object(main, "db", database):
            with self.assertRaises(HTTPException) as raised:
                main.punch_in(main.PunchInIn(emp_code="EMP9999"))
        self.assertEqual(raised.exception.status_code, 404)
        with self.assertRaises(ValidationError):
            main.PunchInIn(emp_code="EMP0001", status="ABSENT")
        with self.assertRaises(ValidationError):
            main.PunchInIn(emp_code="EMP0001", punched_at=1_783_310_000)

    def test_duplicate_punch_in_conflict_is_consistent_for_precheck_and_index_race(self):
        body = main.PunchInIn(
            emp_code="EMP0001",
            punched_at=epoch_millis(2026, 6, 2, 0, 0),
        )
        employee = self.employee("EMP0001")
        existing_database = FakeDatabase(
            [employee],
            [self.attendance("EMP0001", "2026-06-02")],
        )
        with patch.object(main, "db", existing_database):
            with self.assertRaises(HTTPException) as raised:
                main.punch_in(body)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "already punched in for this date")

        race_database = FakeDatabase(
            [employee],
            [],
            attendance_insert_error=DuplicateKeyError("duplicate"),
        )
        with patch.object(main, "db", race_database):
            with self.assertRaises(HTTPException) as raised:
                main.punch_in(body)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "already punched in for this date")

    def test_attendance_list_is_filtered_sorted_paged_and_serialized(self):
        database = FakeDatabase(
            [],
            [
                self.attendance("EMP0002", "2026-06-02"),
                self.attendance("EMP0001", "2026-06-02"),
                self.attendance("EMP0001", "2026-06-01"),
                self.attendance("EMP0001", "2026-06-03", "LEAVE"),
            ],
        )
        with patch.object(main, "db", database):
            result = main.list_attendance(
                emp_code="EMP0001",
                date_from=date(2026, 6, 1),
                date_to=date(2026, 6, 2),
                status="PRESENT",
                page=1,
                page_size=1,
            )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["items"][0]["date"], "2026-06-02")
        self.assertEqual(result["items"][0]["punch_in"], epoch_millis(2026, 6, 1, 3, 40))
        self.assertNotIn("_id", result["items"][0])
        self.assertNotIn("id", result["items"][0])

    def test_attendance_list_rejects_reversed_date_range(self):
        with self.assertRaises(HTTPException) as raised:
            main.list_attendance(
                date_from=date(2026, 6, 2),
                date_to=date(2026, 6, 1),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_lifespan_creates_expected_indexes_on_each_startup(self):
        database = FakeDatabase([], [])
        employee_indexes = []
        attendance_indexes = []
        database.employees.create_index = lambda keys, unique=False: employee_indexes.append(
            (keys, unique)
        )
        database.attendance_logs.create_index = lambda keys, unique=False: attendance_indexes.append(
            (keys, unique)
        )

        async def run_lifespans():
            for _ in range(2):
                async with main.lifespan(main.app):
                    pass

        with patch.object(main, "db", database):
            asyncio.run(run_lifespans())

        self.assertEqual(
            employee_indexes,
            [
                ("emp_code", True),
                ([("joined_on", 1)], False),
                ([("department", 1), ("joined_on", 1)], False),
            ]
            * 2,
        )
        self.assertEqual(
            attendance_indexes,
            [
                ([("emp_code", 1), ("date", 1)], True),
                ([("date", -1), ("emp_code", 1)], False),
            ]
            * 2,
        )


if __name__ == "__main__":
    unittest.main()
