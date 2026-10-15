"""Read-only verification of the application's MongoDB indexes and analytics."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import urlopen


EXPECTED_INDEXES = {
    "employees": [
        ([("emp_code", 1)], True),
        ([("joined_on", 1)], False),
        ([("department", 1), ("joined_on", 1)], False),
    ],
    "attendance_logs": [
        ([("emp_code", 1), ("date", 1)], True),
        ([("date", -1), ("emp_code", 1)], False),
    ],
}


def count_duplicate_groups(collection, identity: dict[str, str]) -> int:
    pipeline = [
        {"$group": {"_id": identity, "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$count": "duplicate_groups"},
    ]
    results = list(collection.aggregate(pipeline))
    return results[0]["duplicate_groups"] if results else 0


def index_matches(collection, keys: list[tuple[str, int]], unique: bool) -> bool:
    for index in collection.list_indexes():
        if list(index["key"].items()) == keys and index.get("unique", False) is unique:
            return True
    return False


def collect_plan_stages(value, stages: set[str], indexes: set[str]) -> None:
    if isinstance(value, dict):
        stage = value.get("stage")
        if isinstance(stage, str):
            stages.add(stage)
        index_name = value.get("indexName")
        if isinstance(index_name, str):
            indexes.add(index_name)
        for key, child in value.items():
            if key in {"rejectedPlans", "allPlansExecution"}:
                continue
            collect_plan_stages(child, stages, indexes)
    elif isinstance(value, list):
        for child in value:
            collect_plan_stages(child, stages, indexes)


def report_explain(base_url: str, endpoint: str, **parameters) -> bool:
    query = urlencode(parameters)
    url = f"{base_url}/admin/explain/{quote(endpoint, safe='')}?{query}"
    try:
        with urlopen(url, timeout=30) as response:
            result = json.load(response)
        stages: set[str] = set()
        indexes: set[str] = set()
        collect_plan_stages(result["explain"], stages, indexes)
        print(
            f"PASS GET /admin/explain/{endpoint}: "
            f"stages={sorted(stages)}, indexes={sorted(indexes)}, "
            f"index_used={bool(indexes)}"
        )
        return True
    except HTTPError as exc:
        print(f"FAIL GET /admin/explain/{endpoint}: HTTP {exc.code}")
        return False
    except URLError as exc:
        print(
            f"FAIL GET /admin/explain/{endpoint}: "
            f"connection error ({type(exc.reason).__name__})"
        )
        return False
    except Exception as exc:
        print(f"FAIL GET /admin/explain/{endpoint}: {type(exc).__name__}")
        return False


def start_local_api() -> tuple[subprocess.Popen, str] | None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print("FAIL local API startup before /health became ready.")
            process.wait()
            return None
        try:
            with urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    print("Local API startup and /health: OK")
                    return process, base_url
        except (HTTPError, URLError, TimeoutError):
            time.sleep(0.25)
    print("FAIL local API did not become healthy within 20 seconds.")
    process.terminate()
    process.wait(timeout=5)
    return None


def run_read_only_checks() -> int:
    if not os.getenv("MONGO_URI") or not os.getenv("MONGO_DB"):
        print("BLOCKED: MONGO_URI or MONGO_DB is absent in this process.")
        print(
            "SKIPPED: MongoDB ping, duplicate inspection, index verification, "
            "aggregations, and all five HTTP explain checks."
        )
        return 2

    from app.main import app, client, db, lifespan

    try:
        try:
            client.admin.command("ping")
        except Exception as exc:
            print(f"MongoDB ping failed ({type(exc).__name__}).")
            return 3
        print("PASS MongoDB ping")

        try:
            employee_conflicts = count_duplicate_groups(
                db.employees,
                {"emp_code": "$emp_code"},
            )
            attendance_conflicts = count_duplicate_groups(
                db.attendance_logs,
                {"emp_code": "$emp_code", "date": "$date"},
            )
        except Exception as exc:
            print(f"Duplicate-key inspection failed ({type(exc).__name__}).")
            return 4
        if employee_conflicts or attendance_conflicts:
            print(
                "FAIL duplicate-key precheck: "
                f"employees={employee_conflicts}, attendance={attendance_conflicts}; "
                "SKIPPED index initialization "
                "to avoid changing index metadata over conflicting records."
            )
            return 5
        print(
            "PASS duplicate-key precheck: "
            f"employees={employee_conflicts}, attendance={attendance_conflicts}"
        )

        try:
            async def initialize_indexes() -> None:
                async with lifespan(app):
                    pass

            asyncio.run(initialize_indexes())
            indexes_ok = True
            for collection_name, definitions in EXPECTED_INDEXES.items():
                collection = db[collection_name]
                for keys, unique in definitions:
                    matched = index_matches(collection, keys, unique)
                    print(
                        f"{'PASS' if matched else 'FAIL'} index "
                        f"{collection_name}{keys} unique={unique}"
                    )
                    indexes_ok = indexes_ok and matched
        except Exception as exc:
            print(f"Startup/index verification failed ({type(exc).__name__}).")
            indexes_ok = False

        from app import main as application

        employee = db.employees.find_one(
            {},
            {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1},
            sort=[("emp_code", 1)],
        )
        attendance = db.attendance_logs.find_one(
            {},
            {"_id": 0, "emp_code": 1, "date": 1, "status": 1},
            sort=[("date", -1), ("emp_code", 1)],
        )
        if employee is None:
            print(
                "INFO: employees is empty; using a non-matching employee code "
                "and department for read-only checks."
            )
        anchor_text = (
            attendance["date"]
            if attendance
            else employee["joined_on"]
            if employee
            else date.today().isoformat()
        )
        emp_code = employee["emp_code"] if employee else "VERIFY_NO_SUCH_EMPLOYEE"
        department = (
            employee["department"]
            if employee
            else "VERIFY_NO_SUCH_DEPARTMENT"
        )
        anchor = date.fromisoformat(anchor_text)
        month = anchor.strftime("%Y-%m")
        trend_end = anchor + timedelta(days=6)
        checks_ok = indexes_ok

        query = {"emp_code": emp_code}
        if attendance:
            query["date"] = {
                "$gte": attendance["date"],
                "$lte": attendance["date"],
            }
        if attendance and attendance.get("status"):
            query["status"] = attendance["status"]

        def run_read_check(label: str, operation) -> bool:
            try:
                operation()
                print(f"PASS {label}")
                return True
            except Exception as exc:
                print(f"FAIL {label}: {type(exc).__name__}")
                return False

        checks_ok = run_read_check(
            "attendance_list query executed",
            lambda: list(
                db.attendance_logs.find(query)
                .sort([("date", -1), ("emp_code", 1)])
                .skip(0)
                .limit(20),
            ),
        ) and checks_ok
        checks_ok = run_read_check(
            "employee_monthly aggregation executed",
            lambda: list(
                db.employees.aggregate(
                    application._employee_monthly_pipeline(
                        emp_code,
                        month,
                    )
                )
            ),
        ) and checks_ok
        checks_ok = run_read_check(
            "department_summary aggregation executed",
            lambda: list(
                db.employees.aggregate(
                    application._department_summary_pipeline(month, None)
                )
            ),
        ) and checks_ok
        checks_ok = run_read_check(
            "late_leaderboard aggregation executed",
            lambda: list(
                db.attendance_logs.aggregate(
                    application._late_leaderboard_pipeline(month, 10, None)
                )
            ),
        ) and checks_ok
        checks_ok = run_read_check(
            "department_trend aggregation executed",
            lambda: list(
                db.employees.aggregate(
                    application._department_trend_pipeline(
                        department,
                        anchor,
                        trend_end,
                    )
                )
            ),
        ) and checks_ok

        local_api = start_local_api()
        if local_api is None:
            print("SKIPPED: five HTTP explain checks; local API did not start.")
            return 9
        process, base_url = local_api
        try:
            attendance_parameters = {
                "emp_code": emp_code,
                "date_from": anchor.isoformat(),
                "date_to": anchor.isoformat(),
                "page": 1,
                "page_size": 20,
            }
            if attendance and attendance.get("status"):
                attendance_parameters["status"] = attendance["status"]
            checks_ok = report_explain(
                base_url,
                "attendance_list",
                **attendance_parameters,
            ) and checks_ok
            checks_ok = report_explain(
                base_url,
                "employee_monthly",
                emp_code=emp_code,
                month=month,
            ) and checks_ok
            checks_ok = report_explain(
                base_url,
                "department_summary",
                month=month,
            ) and checks_ok
            checks_ok = report_explain(
                base_url,
                "late_leaderboard",
                month=month,
                limit=10,
            ) and checks_ok
            checks_ok = report_explain(
                base_url,
                "department_trend",
                department=department,
                **{"from": anchor.isoformat(), "to": trend_end.isoformat()},
            ) and checks_ok
        finally:
            process.terminate()
            process.wait(timeout=10)
        return 0 if checks_ok else 9
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(run_read_only_checks())
