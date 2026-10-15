import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import main


class FakeEmployeeCollection:
    def __init__(self, result):
        self.result = result
        self.pipeline = None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return iter(self.result)


class FakeDatabase:
    def __init__(self, aggregation_result):
        self.employees = FakeEmployeeCollection(aggregation_result)


def nested_values(value, key):
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                yield current_value
            yield from nested_values(current_value, key)
    elif isinstance(value, list):
        for item in value:
            yield from nested_values(item, key)


class MonthlyAnalyticsTests(unittest.TestCase):
    def result(self, **overrides):
        summary = {
            "emp_code": "EMP0001",
            "month": "2026-06",
            "working_days": 22,
            "present_days": 10.5,
            "leave_days": 1,
            "late_count": 2,
            "total_late_minutes": 35,
            "total_overtime_minutes": 60,
            "attendance_pct": 47.7273,
        }
        summary.update(overrides)
        return summary

    def invoke(self, aggregation_result, month="2026-06"):
        database = FakeDatabase(aggregation_result)
        with patch.object(main, "db", database):
            result = main.employee_monthly("EMP0001", month)
        return result, database

    def test_returns_monthly_metrics_from_mongodb_aggregation(self):
        summary = self.result()
        result, database = self.invoke([summary])

        self.assertEqual(result, summary)
        pipeline = database.employees.pipeline
        self.assertEqual(pipeline[0], {"$match": {"emp_code": "EMP0001"}})
        lookup = pipeline[1]["$lookup"]
        self.assertEqual(lookup["from"], "attendance_logs")
        match = lookup["pipeline"][0]["$match"]["$expr"]["$and"]
        self.assertIn({"$gte": ["$date", "2026-06-01"]}, match)
        self.assertIn({"$lte": ["$date", "2026-06-30"]}, match)
        projected_log_fields = lookup["pipeline"][1]["$project"]
        self.assertEqual(
            set(projected_log_fields) - {"_id"},
            {"date", "status", "half_day", "late_minutes", "overtime_minutes"},
        )
        self.assertNotIn("punch_in", projected_log_fields)
        self.assertNotIn("punch_out", projected_log_fields)

    def test_empty_month_returns_zero_metrics_and_null_rate_for_no_workdays(self):
        summary = self.result(
            working_days=0,
            present_days=0,
            leave_days=0,
            late_count=0,
            total_late_minutes=0,
            total_overtime_minutes=0,
            attendance_pct=None,
        )
        result, database = self.invoke([summary], month="2026-02")

        self.assertEqual(result, summary)
        lookup = database.employees.pipeline[1]["$lookup"]
        match = lookup["pipeline"][0]["$match"]["$expr"]["$and"]
        self.assertIn({"$gte": ["$date", "2026-02-01"]}, match)
        self.assertIn({"$lte": ["$date", "2026-02-28"]}, match)
        percentage_conditions = list(
            nested_values(database.employees.pipeline, "$eq")
        )
        self.assertIn(["$working_days", 0], percentage_conditions)

    def test_month_with_workdays_but_no_attendance_has_zero_rate(self):
        summary = self.result(
            working_days=22,
            present_days=0,
            leave_days=0,
            late_count=0,
            total_late_minutes=0,
            total_overtime_minutes=0,
            attendance_pct=0.0,
        )
        result, _ = self.invoke([summary])
        self.assertEqual(result, summary)

    def test_join_date_weekdays_presence_and_rounding_are_in_pipeline(self):
        _, database = self.invoke([self.result()], month="2024-02")
        pipeline = database.employees.pipeline

        self.assertIn(
            {"dateString": "$joined_on"},
            list(nested_values(pipeline, "$dateFromString")),
        )
        self.assertTrue(list(nested_values(pipeline, "$dateAdd")))
        self.assertTrue(list(nested_values(pipeline, "$isoDayOfWeek")))
        self.assertTrue(list(nested_values(pipeline, "$range")))
        self.assertTrue(list(nested_values(pipeline, "$dateDiff")))
        self.assertIn(
            ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]],
            list(nested_values(pipeline, "$in")),
        )
        self.assertIn(
            ["$$this.half_day", False],
            list(nested_values(pipeline, "$ifNull")),
        )
        self.assertIn(
            ["$$log.status", "LEAVE"],
            list(nested_values(pipeline, "$eq")),
        )
        self.assertIn(
            ["$$log.late_minutes", 0],
            list(nested_values(pipeline, "$ifNull")),
        )
        self.assertIn(
            ["$$log.overtime_minutes", 0],
            list(nested_values(pipeline, "$ifNull")),
        )
        self.assertIn(
            ["$present_half_day_units", 1_000_000],
            list(nested_values(pipeline, "$multiply")),
        )
        self.assertIn(
            ["$working_days", 2],
            list(nested_values(pipeline, "$multiply")),
        )

        lookup = pipeline[1]["$lookup"]
        match = lookup["pipeline"][0]["$match"]["$expr"]["$and"]
        self.assertIn({"$gte": ["$date", "2024-02-01"]}, match)
        self.assertIn({"$lte": ["$date", "2024-02-29"]}, match)

    def test_unknown_employee_returns_not_found(self):
        with self.assertRaises(HTTPException) as raised:
            self.invoke([])
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "employee not found")

    def test_invalid_calendar_month_returns_validation_error(self):
        for month in ("2026-13", "2026-00", "0000-01", "2026-1"):
            with self.subTest(month=month):
                with self.assertRaises(HTTPException) as raised:
                    self.invoke([], month=month)
                self.assertEqual(raised.exception.status_code, 422)

    def test_registered_route_matches_monthly_contract(self):
        operation = main.app.openapi()["paths"][
            "/analytics/employees/{emp_code}/monthly"
        ]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

        self.assertTrue(parameters["month"]["required"])
        self.assertEqual(
            parameters["month"]["schema"]["pattern"],
            r"^\d{4}-(0[1-9]|1[0-2])$",
        )
        self.assertEqual(set(operation["responses"]), {"200", "404", "422"})
        response_schema = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        response_schema_name = response_schema["$ref"].rsplit("/", 1)[1]
        response_properties = main.app.openapi()["components"]["schemas"][
            response_schema_name
        ]["properties"]
        self.assertEqual(
            set(response_properties),
            {
                "emp_code",
                "month",
                "working_days",
                "present_days",
                "leave_days",
                "late_count",
                "total_late_minutes",
                "total_overtime_minutes",
                "attendance_pct",
            },
        )


if __name__ == "__main__":
    unittest.main()
