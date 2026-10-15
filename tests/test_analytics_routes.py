import unittest
from datetime import date
from unittest.mock import patch

from bson import Binary, Timestamp
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from app import main


def nested_values(value, key):
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                yield current_value
            yield from nested_values(current_value, key)
    elif isinstance(value, list):
        for item in value:
            yield from nested_values(item, key)


class FakeCollection:
    def __init__(self, result):
        self.result = result
        self.pipeline = None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return iter(self.result)


class FakeDatabase:
    def __init__(self, result=None, command_result=None):
        self.employees = FakeCollection(result or [])
        self.attendance_logs = FakeCollection(result or [])
        self.command_result = command_result or {
            "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
            "executionStats": {"nReturned": 1},
        }
        self.command_argument = None

    def command(self, command):
        self.command_argument = command
        return self.command_result


class AnalyticsRouteTests(unittest.TestCase):
    def test_department_summary_counts_employees_before_log_lookup(self):
        result = {
            "month": "2026-02",
            "items": [
                {
                    "department": "Engineering",
                    "headcount": 3,
                    "present_days": 2.5,
                    "avg_work_hours": 8.12,
                    "late_count": 1,
                    "total_late_minutes": 35,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            ],
        }
        database = FakeDatabase([result["items"][0]])
        with patch.object(main, "db", database):
            response = main.department_summary("2026-02", "Engineering")

        self.assertEqual(response, result)
        pipeline = database.employees.pipeline
        self.assertEqual(
            pipeline[0]["$match"],
            {"joined_on": {"$lte": "2026-02-28"}, "department": "Engineering"},
        )
        self.assertEqual(pipeline[1]["$lookup"]["from"], "attendance_logs")
        self.assertEqual(pipeline[-1], {"$sort": {"department": 1}})
        self.assertTrue(list(nested_values(pipeline, "$reduce")))
        self.assertTrue(list(nested_values(pipeline, "$floor")))

    def test_leaderboard_ranks_first_and_preserves_ties_at_limit(self):
        ranked = [
            {
                "rank": 1,
                "emp_code": "EMP0001",
                "name": "Asha",
                "department": "Engineering",
                "total_late_minutes": 60,
                "late_count": 3,
            },
            {
                "rank": 2,
                "emp_code": "EMP0002",
                "name": "Bala",
                "department": "Engineering",
                "total_late_minutes": 30,
                "late_count": 2,
            },
            {
                "rank": 2,
                "emp_code": "EMP0003",
                "name": "Chirag",
                "department": "Engineering",
                "total_late_minutes": 30,
                "late_count": 2,
            },
        ]
        database = FakeDatabase(ranked)
        with patch.object(main, "db", database):
            response = main.late_leaderboard("2026-06", 2, "Engineering")

        self.assertEqual(response, {"month": "2026-06", "items": ranked})
        pipeline = database.attendance_logs.pipeline
        rank_index = next(
            index for index, stage in enumerate(pipeline) if "$setWindowFields" in stage
        )
        limit_index = next(
            index
            for index, stage in enumerate(pipeline)
            if stage.get("$match", {}).get("rank") == {"$lte": 2}
        )
        self.assertLess(rank_index, limit_index)
        self.assertEqual(pipeline[rank_index]["$setWindowFields"]["output"], {"rank": {"$rank": {}}})
        lookup = next(stage["$lookup"] for stage in pipeline if "$lookup" in stage)
        self.assertEqual(lookup["pipeline"][0], {"$match": {"department": "Engineering"}})
        self.assertEqual(pipeline[-2], {"$sort": {"total_late_minutes": -1, "emp_code": 1}})

    def test_department_trend_fills_inclusive_range_and_calculates_in_pipeline(self):
        trend = {
            "department": "Engineering",
            "items": [
                {
                    "date": "2026-06-05",
                    "is_working_day": True,
                    "headcount": 1,
                    "present_count": 0,
                    "late_count": 0,
                    "attendance_rate": 0.0,
                    "moving_avg_7d": 0.0,
                },
                {
                    "date": "2026-06-06",
                    "is_working_day": False,
                    "headcount": 1,
                    "present_count": 0,
                    "late_count": 0,
                    "attendance_rate": None,
                    "moving_avg_7d": None,
                },
                {
                    "date": "2026-06-07",
                    "is_working_day": False,
                    "headcount": 2,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": None,
                    "moving_avg_7d": None,
                },
            ],
        }
        database = FakeDatabase([trend])
        with patch.object(main, "db", database):
            response = main.department_trend(
                "Engineering",
                date(2026, 6, 5),
                date(2026, 6, 7),
            )

        self.assertEqual(response, trend)
        pipeline = database.employees.pipeline
        self.assertEqual(pipeline[0], {"$match": {"department": "Engineering"}})
        self.assertEqual(
            pipeline[1]["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"][0],
            {"$eq": ["$emp_code", "$$employee_code"]},
        )
        self.assertFalse(
            any(
                stage.get("$group", {}).get("employees", {}).get("$push")
                for stage in pipeline
            )
        )
        self.assertEqual(
            next(nested_values(pipeline, "$range")),
            [0, 3],
        )
        self.assertIn(
            [-6, 0],
            list(nested_values(pipeline, "documents")),
        )
        self.assertTrue(list(nested_values(pipeline, "$dateAdd")))
        self.assertIn(["$joined_on", "$$day_date"], list(nested_values(pipeline, "$lte")))
        daily_group = next(
            stage["$group"]
            for stage in pipeline
            if "$group" in stage and stage["$group"].get("_id") == "$date"
        )
        self.assertEqual(daily_group["headcount"], {"$sum": "$headcount"})
        self.assertEqual(
            daily_group["present_half_day_units"],
            {"$sum": "$present_half_day_units"},
        )
        self.assertIn(
            ["$status", ["PRESENT", "WFH", "ON_DUTY"]],
            list(nested_values(pipeline, "$in")),
        )
        self.assertTrue(list(nested_values(pipeline, "$setWindowFields")))
        self.assertTrue(list(nested_values(pipeline, "_rate_sum_units")))

    def test_department_trend_rejects_invalid_range_and_missing_department(self):
        with self.assertRaises(HTTPException) as raised:
            main.department_trend(
                "Engineering",
                date(2026, 6, 2),
                date(2026, 6, 1),
            )
        self.assertEqual(raised.exception.status_code, 422)

        with patch.object(main, "db", FakeDatabase([])):
            with self.assertRaises(HTTPException) as raised:
                main.department_trend(
                    "Unknown",
                    date(2026, 6, 1),
                    date(2026, 6, 1),
                )
        self.assertEqual(raised.exception.status_code, 404)

    def test_explain_executes_statistics_and_returns_json_safe_output(self):
        database = FakeDatabase(
            command_result={
                "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
                "executionStats": {"nReturned": 2},
            }
        )
        with patch.object(main, "db", database):
            response = main.explain_endpoint(
                "attendance_list",
                emp_code="EMP0001",
                date_from=date(2026, 6, 1),
                date_to=date(2026, 6, 30),
                status="PRESENT",
                page=2,
                page_size=5,
            )

        command = database.command_argument
        self.assertEqual(command["verbosity"], "executionStats")
        self.assertEqual(
            command["explain"]["find"],
            "attendance_logs",
        )
        self.assertEqual(
            command["explain"]["filter"],
            {
                "emp_code": "EMP0001",
                "date": {"$gte": "2026-06-01", "$lte": "2026-06-30"},
                "status": "PRESENT",
            },
        )
        self.assertEqual(command["explain"]["sort"], {"date": -1, "emp_code": 1})
        self.assertEqual(command["explain"]["skip"], 5)
        self.assertEqual(command["explain"]["limit"], 5)
        self.assertEqual(response["endpoint"], "attendance_list")
        self.assertEqual(response["collection"], "attendance_logs")
        self.assertIn("executionStats", response["explain"])

    def test_explain_analytics_uses_endpoint_pipeline_builders(self):
        date_from = date(2026, 6, 1)
        date_to = date(2026, 6, 30)
        cases = (
            (
                "employee_monthly",
                {"emp_code": "EMP0001", "month": "2026-06"},
                "employees",
                main._employee_monthly_pipeline("EMP0001", "2026-06"),
            ),
            (
                "department_summary",
                {"month": "2026-06", "department": "Engineering"},
                "employees",
                main._department_summary_pipeline("2026-06", "Engineering"),
            ),
            (
                "late_leaderboard",
                {"month": "2026-06", "limit": 5, "department": "Engineering"},
                "attendance_logs",
                main._late_leaderboard_pipeline(
                    "2026-06",
                    5,
                    "Engineering",
                ),
            ),
            (
                "department_trend",
                {
                    "department": "Engineering",
                    "from_date": date_from,
                    "to_date": date_to,
                    "month": None,
                },
                "employees",
                main._department_trend_pipeline(
                    "Engineering",
                    date_from,
                    date_to,
                ),
            ),
        )

        for endpoint, parameters, collection, pipeline in cases:
            with self.subTest(endpoint=endpoint):
                database = FakeDatabase()
                with patch.object(main, "db", database):
                    response = main.explain_endpoint(endpoint, **parameters)

                explained = database.command_argument["explain"]
                self.assertEqual(database.command_argument["verbosity"], "executionStats")
                self.assertEqual(explained["aggregate"], collection)
                self.assertEqual(explained["pipeline"], pipeline)
                self.assertEqual(response["collection"], collection)

    def test_explain_response_encodes_bson_metadata_as_extended_json(self):
        database = FakeDatabase(
            command_result={
                "operationTime": Timestamp(1, 2),
                "$clusterTime": {
                    "clusterTime": Timestamp(1, 2),
                    "signature": {"hash": Binary(b"abc"), "keyId": 1},
                },
                "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
            }
        )
        with patch.object(main, "db", database):
            response = main.explain_endpoint(
                "attendance_list",
                page=1,
                page_size=20,
            )

        encoded = jsonable_encoder(response)
        self.assertEqual(encoded["explain"]["operationTime"], {"$timestamp": {"t": 1, "i": 2}})
        self.assertEqual(
            encoded["explain"]["$clusterTime"]["signature"]["hash"],
            {"$binary": {"base64": "YWJj", "subType": "00"}},
        )
        self.assertEqual(
            encoded["explain"]["queryPlanner"]["winningPlan"]["stage"],
            "IXSCAN",
        )

    def test_explain_requires_each_analytics_routes_parameters(self):
        cases = (
            ("employee_monthly", {"emp_code": "EMP0001", "month": None}),
            ("department_summary", {"month": None}),
            ("late_leaderboard", {"month": None}),
            (
                "department_trend",
                {
                    "department": "Engineering",
                    "month": None,
                    "from_date": None,
                    "to_date": None,
                },
            ),
        )
        for endpoint, kwargs in cases:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(HTTPException) as raised:
                    main.explain_endpoint(endpoint, **kwargs)
                self.assertEqual(raised.exception.status_code, 422)

    def test_registered_analytics_routes_have_contract_parameters(self):
        paths = main.app.openapi()["paths"]
        expected_operations = {
            "/health": {"get"},
            "/employees": {"get", "post"},
            "/attendance/punch-in": {"post"},
            "/attendance": {"get"},
            "/attendance/punch-out": {"post"},
            "/attendance/{emp_code}/{date}": {"patch"},
            "/analytics/employees/{emp_code}/monthly": {"get"},
            "/analytics/departments/summary": {"get"},
            "/analytics/leaderboard/late": {"get"},
            "/analytics/departments/{department}/trend": {"get"},
            "/admin/explain/{endpoint}": {"get"},
        }
        for path, operations in expected_operations.items():
            with self.subTest(path=path):
                self.assertTrue(operations.issubset(paths[path]))
        summary_params = {
            parameter["name"]
            for parameter in paths["/analytics/departments/summary"]["get"]["parameters"]
        }
        leaderboard_params = {
            parameter["name"]
            for parameter in paths["/analytics/leaderboard/late"]["get"]["parameters"]
        }
        trend_params = {
            parameter["name"]
            for parameter in paths[
                "/analytics/departments/{department}/trend"
            ]["get"]["parameters"]
        }
        self.assertEqual(summary_params, {"month", "department"})
        self.assertEqual(leaderboard_params, {"month", "limit", "department"})
        self.assertEqual(trend_params, {"department", "from", "to"})


if __name__ == "__main__":
    unittest.main()
