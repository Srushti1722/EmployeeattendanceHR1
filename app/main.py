"""Employee Attendance & Analytics API.

Run with ``uvicorn app.main:app --port 8000``. Configure MongoDB using
``MONGO_URI`` and ``MONGO_DB``; a local ``.env`` is loaded for development.
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from bson import json_util
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
    )
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )


class RegularizeIn(BaseModel):
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] = Field(
        default=None,
    )
    punch_in: StrictInt = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    punch_out: StrictInt = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = _to_epoch_millis(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        item["created_at"] = _to_epoch_millis(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = _epoch_millis_to_utc(body.punched_at)
    d = _regularization_attendance_day(ts, emp).isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts.replace(tzinfo=None),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": _late_minutes_for_attendance_day(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return _attendance_response(doc)


def _epoch_millis_to_utc(punched_at: Optional[int]) -> datetime:
    if punched_at is None:
        instant = datetime.now(timezone.utc)
    else:
        instant = datetime.fromtimestamp(punched_at // 1000, timezone.utc)
    return instant.replace(microsecond=0)


def _as_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def _to_epoch_millis(instant: Optional[datetime]) -> Optional[int]:
    if instant is None:
        return None
    return int(_as_utc(instant).replace(microsecond=0).timestamp() * 1000)


def _attendance_response(record: dict) -> dict:
    history = []
    for entry in record.get("history", []):
        converted_entry = dict(entry)
        converted_entry["at"] = _to_epoch_millis(entry.get("at"))
        changes = {}
        for field, change in entry.get("changes", {}).items():
            converted_change = dict(change)
            if field in {"punch_in", "punch_out"}:
                converted_change["from"] = _to_epoch_millis(change.get("from"))
                converted_change["to"] = _to_epoch_millis(change.get("to"))
            changes[field] = converted_change
        converted_entry["changes"] = changes
        history.append(converted_entry)

    return {
        "emp_code": record["emp_code"],
        "date": record["date"],
        "status": record["status"],
        "punch_in": _to_epoch_millis(record.get("punch_in")),
        "punch_out": _to_epoch_millis(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
        "history": history,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punched_out_at = _epoch_millis_to_utc(body.punched_at)
    punched_out_at_db = punched_out_at.replace(tzinfo=None)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {
                "$type": "date",
                "$lt": punched_out_at_db + timedelta(seconds=1),
            },
        },
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")

    punched_in_at = _as_utc(record["punch_in"]).replace(microsecond=0)
    elapsed = punched_out_at - punched_in_at
    if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    total_seconds = int(elapsed.total_seconds())
    work_hours = float(
        (Decimal(total_seconds) / Decimal(3600)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )

    shift_start_hour, shift_start_minute = map(
        int,
        employee["shift_start"].split(":"),
    )
    shift_end_hour, shift_end_minute = map(
        int,
        employee["shift_end"].split(":"),
    )
    attendance_day = date.fromisoformat(record["date"])
    shift_end_day = attendance_day
    if employee["shift_end"] <= employee["shift_start"]:
        shift_end_day += timedelta(days=1)
    shift_end_at = datetime(
        shift_end_day.year,
        shift_end_day.month,
        shift_end_day.day,
        shift_end_hour,
        shift_end_minute,
        tzinfo=IST,
    ).astimezone(timezone.utc)
    overtime_elapsed = punched_out_at - shift_end_at
    overtime_minutes = 0
    if overtime_elapsed.total_seconds() >= 30 * 60:
        overtime_minutes = int(overtime_elapsed.total_seconds() // 60)

    update = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "emp_code": body.emp_code,
            "$or": [
                {"punch_out": None},
                {"punch_out": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "punch_in": punched_in_at.replace(tzinfo=None),
                "punch_out": punched_out_at_db,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
                "late_minutes": record.get("late_minutes", 0),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if update is None:
        current_record = db.attendance_logs.find_one({"_id": record["_id"]})
        if current_record is None:
            raise HTTPException(404, "no punch-in found")
        raise HTTPException(409, "already punched out")

    return _attendance_response(update)


def _regularization_attendance_day(punch_in: datetime, employee: dict) -> date:
    local_punch_in = _as_utc(punch_in).astimezone(IST)
    if employee["shift_end"] <= employee["shift_start"]:
        if local_punch_in.strftime("%H:%M") < employee["shift_end"]:
            return local_punch_in.date() - timedelta(days=1)
    return local_punch_in.date()


def _late_minutes_for_attendance_day(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    start_hour, start_minute = map(int, shift_start.split(":"))
    day = date.fromisoformat(attendance_date)
    shift_start_at = datetime(
        day.year,
        day.month,
        day.day,
        start_hour,
        start_minute,
        tzinfo=IST,
    ).astimezone(timezone.utc)
    delay_seconds = int((_as_utc(punch_in) - shift_start_at).total_seconds())
    return delay_seconds // 60 if delay_seconds > 10 * 60 else 0


def _regularization_values(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    employee: dict,
    attendance_day: date,
) -> dict:
    if status in {"ABSENT", "LEAVE"}:
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    if punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    start_hour, start_minute = map(int, employee["shift_start"].split(":"))
    shift_start = datetime(
        attendance_day.year,
        attendance_day.month,
        attendance_day.day,
        start_hour,
        start_minute,
        tzinfo=IST,
    )
    late_seconds = int(
        (
            punch_in.astimezone(IST)
            - shift_start
        ).total_seconds()
    )
    late_minutes = late_seconds // 60 if late_seconds > 10 * 60 else 0

    if punch_out is None:
        return {
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }

    elapsed = punch_out - punch_in
    if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    total_seconds = int(elapsed.total_seconds())
    work_hours = float(
        (Decimal(total_seconds) / Decimal(3600)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )
    end_hour, end_minute = map(int, employee["shift_end"].split(":"))
    shift_end_day = attendance_day
    if employee["shift_end"] <= employee["shift_start"]:
        shift_end_day += timedelta(days=1)
    shift_end = datetime(
        shift_end_day.year,
        shift_end_day.month,
        shift_end_day.day,
        end_hour,
        end_minute,
        tzinfo=IST,
    ).astimezone(timezone.utc)
    overtime_seconds = int((punch_out - shift_end).total_seconds())
    overtime_minutes = (
        overtime_seconds // 60
        if overtime_seconds >= 30 * 60
        else 0
    )

    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": work_hours < 4.50,
    }


def _regularization_state_value(record: dict, field: str):
    if field in {"punch_in", "punch_out"}:
        value = record.get(field)
        if value is None:
            return None
        return _as_utc(value).replace(microsecond=0)
    defaults = {
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    return record.get(field, defaults.get(field))


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: date,
    body: RegularizeIn,
):
    attendance_date = date
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": attendance_date.isoformat()}
    )
    if record is None:
        raise HTTPException(404, "attendance record not found")

    old_status = record["status"]
    old_punch_in = _regularization_state_value(record, "punch_in")
    old_punch_out = _regularization_state_value(record, "punch_out")
    status = body.status if body.status is not None else old_status
    request_fields = body.model_fields_set

    if status in {"ABSENT", "LEAVE"}:
        if {"punch_in", "punch_out"} & request_fields:
            raise HTTPException(
                422,
                "punch_in and punch_out cannot be supplied with ABSENT or LEAVE",
            )
        punch_in = None
        punch_out = None
    else:
        punch_in = (
            _epoch_millis_to_utc(body.punch_in)
            if "punch_in" in request_fields
            else old_punch_in
        )
        punch_out = (
            _epoch_millis_to_utc(body.punch_out)
            if "punch_out" in request_fields
            else old_punch_out
        )
        if punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if _regularization_attendance_day(punch_in, employee) != attendance_date:
            raise HTTPException(422, "punch_in must be on the attendance date")

    before = {
        "status": old_status,
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": _regularization_state_value(record, "work_hours"),
        "late_minutes": _regularization_state_value(record, "late_minutes"),
        "overtime_minutes": _regularization_state_value(record, "overtime_minutes"),
        "half_day": _regularization_state_value(record, "half_day"),
    }
    after = {
        "status": status,
        **_regularization_values(
            status,
            punch_in,
            punch_out,
            employee,
            attendance_date,
        ),
    }
    user_fields = ("status", "punch_in", "punch_out")
    if not any(before[field] != after[field] for field in user_fields):
        raise HTTPException(422, "request does not change the attendance record")

    changes = {}
    for field in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        old_value = before[field]
        new_value = after[field]
        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    query = {
        "_id": record["_id"],
        "emp_code": emp_code,
        "date": attendance_date.isoformat(),
    }
    mutable_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
        "history",
    )
    for field in mutable_fields:
        if field in record:
            query[field] = {"$exists": True, "$eq": record[field]}
        else:
            query[field] = {"$exists": False}

    update_fields = {
        field: value.replace(tzinfo=None) if isinstance(value, datetime) else value
        for field, value in after.items()
    }
    updated = db.attendance_logs.find_one_and_update(
        query,
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record was modified concurrently")

    return _attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_attendance_response(document) for document in page_docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError:
        raise HTTPException(422, "month must be a valid YYYY-MM month")
    month_end = month_start.replace(
        day=calendar.monthrange(month_start.year, month_start.month)[1]
    )
    month_start_datetime = datetime.combine(month_start, datetime.min.time())
    month_end_datetime = datetime.combine(month_end, datetime.min.time())

    is_weekday = lambda date_expression: {
        "$lte": [
            {"$isoDayOfWeek": date_expression},
            5,
        ]
    }
    log_date = {
        "$dateFromString": {
            "dateString": "$$this.date",
        }
    }
    working_day_difference = {
        "$dateDiff": {
            "startDate": "$_working_start",
            "endDate": "$_working_end",
            "unit": "day",
            "timezone": "UTC",
        }
    }

    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lte": ["$date", month_end.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1,
                        }
                    },
                ],
                "as": "monthly_logs",
            }
        },
        {
            "$set": {
                "_working_start": {
                    "$max": [
                        {"$dateFromString": {"dateString": "$joined_on"}},
                        month_start_datetime,
                    ]
                },
                "_working_end": month_end_datetime,
            }
        },
        {
            "$set": {
                "working_days": {
                    "$let": {
                        "vars": {"day_difference": working_day_difference},
                        "in": {
                            "$size": {
                                "$filter": {
                                    "input": {
                                        "$map": {
                                            "input": {
                                                "$range": [
                                                    0,
                                                    {
                                                        "$cond": [
                                                            {
                                                                "$gte": [
                                                                    "$$day_difference",
                                                                    0,
                                                                ]
                                                            },
                                                            {
                                                                "$add": [
                                                                    "$$day_difference",
                                                                    1,
                                                                ]
                                                            },
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "as": "day_offset",
                                            "in": {
                                                "$dateAdd": {
                                                    "startDate": "$_working_start",
                                                    "unit": "day",
                                                    "amount": "$$day_offset",
                                                    "timezone": "UTC",
                                                }
                                            },
                                        }
                                    },
                                    "as": "working_day",
                                    "cond": {
                                        "$lte": [
                                            {"$isoDayOfWeek": "$$working_day"},
                                            5,
                                        ]
                                    },
                                }
                            }
                        },
                    }
                },
                "present_half_day_units": {
                    "$reduce": {
                        "input": "$monthly_logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$$this.status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                is_weekday(log_date),
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$ifNull": [
                                                        "$$this.half_day",
                                                        False,
                                                    ]
                                                },
                                                1,
                                                2,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.overtime_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_pct": {
                    "$cond": [
                        {"$eq": ["$working_days", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {
                                                "$add": [
                                                    {
                                                        "$multiply": [
                                                            "$present_half_day_units",
                                                            1_000_000,
                                                        ]
                                                    },
                                                    "$working_days",
                                                ]
                                            },
                                            {"$multiply": ["$working_days", 2]},
                                        ]
                                    }
                                },
                                10_000,
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": {
                    "$divide": ["$present_half_day_units", 2]
                },
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": 1,
            }
        },
    ]
    return pipeline


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthlyOut,
    responses={
        404: {
            "description": "Unknown employee",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["detail"],
                        "properties": {"detail": {"type": "string"}},
                    }
                }
            },
        }
    },
)
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    pipeline = _employee_monthly_pipeline(emp_code, month)
    result = next(iter(db.employees.aggregate(pipeline)), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    return result


def _month_end(month: str) -> date:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must use YYYY-MM format")
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError:
        raise HTTPException(422, "month must be a valid YYYY-MM month")
    return start.replace(day=calendar.monthrange(start.year, start.month)[1])


def _department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    end = _month_end(month)
    match: dict = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "monthly_logs",
            }
        },
        {
            "$set": {
                "present_half_day_units": {
                    "$reduce": {
                        "input": "$monthly_logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$$this.status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$lte": [
                                                        {
                                                            "$isoDayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$this.date"
                                                                }
                                                            }
                                                        },
                                                        5,
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$ifNull": [
                                                        "$$this.half_day",
                                                        False,
                                                    ]
                                                },
                                                1,
                                                2,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "work_hours_sum_cents": {
                    "$sum": {
                        "$map": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {"$ne": ["$$log.work_hours", None]},
                                        ]
                                    },
                                    {
                                        "$round": [
                                            {"$multiply": ["$$log.work_hours", 100]},
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$map": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {"$ne": ["$$log.work_hours", None]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$$log.late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$monthly_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_half_day_units": {"$sum": "$present_half_day_units"},
                "work_hours_sum_cents": {"$sum": "$work_hours_sum_cents"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$divide": ["$present_half_day_units", 2]
                },
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$work_hours_count", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {
                                                "$add": [
                                                    {
                                                        "$multiply": [
                                                            "$work_hours_sum_cents",
                                                            2,
                                                        ]
                                                    },
                                                    "$work_hours_count",
                                                ]
                                            },
                                            {"$multiply": ["$work_hours_count", 2]},
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = _department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


def _late_leaderboard_pipeline(
    month: str,
    limit: int,
    department: Optional[str],
) -> list[dict]:
    end = _month_end(month)
    employee_pipeline: list[dict] = [
        {
            "$match": {
                "$expr": {"$eq": ["$emp_code", "$$employee_code"]},
            }
        },
        {"$project": {"_id": 0, "name": 1, "department": 1}},
    ]
    if department is not None:
        employee_pipeline.insert(0, {"$match": {"department": department}})

    pipeline: list[dict] = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": end.isoformat()},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"employee_code": "$_id"},
                "pipeline": employee_pipeline,
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$set": {
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
            }
        },
    ]
    pipeline.append(
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        }
    )
    pipeline.extend(
        [
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = _late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def _department_trend_pipeline(
    department: str,
    start: date,
    end: date,
) -> list[dict]:
    range_days = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_codes": {
                        "$map": {
                            "input": "$employees",
                            "as": "employee",
                            "in": "$$employee.emp_code",
                        }
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$employee_codes"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": "$date",
                            "present_half_day_units": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$ifNull": ["$half_day", False]},
                                                1,
                                                2,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_logs",
            }
        },
        {
            "$set": {
                "items": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                range_days,
                            ]
                        },
                        "as": "day_offset",
                        "in": {
                            "$let": {
                                "vars": {
                                    "day_date": {
                                        "$dateToString": {
                                            "date": {
                                                "$dateAdd": {
                                                    "startDate": datetime.combine(
                                                        start, datetime.min.time()
                                                    ),
                                                    "unit": "day",
                                                    "amount": "$$day_offset",
                                                    "timezone": "UTC",
                                                }
                                            },
                                            "format": "%Y-%m-%d",
                                            "timezone": "UTC",
                                        }
                                    }
                                },
                                "in": {
                                    "$let": {
                                        "vars": {
                                            "day_logs": {
                                                "$arrayElemAt": [
                                                    {
                                                        "$filter": {
                                                            "input": "$daily_logs",
                                                            "as": "log",
                                                            "cond": {
                                                                "$eq": [
                                                                    "$$log._id",
                                                                    "$$day_date",
                                                                ]
                                                            },
                                                        }
                                                    },
                                                    0,
                                                ]
                                            },
                                            "day_headcount": {
                                                "$size": {
                                                    "$filter": {
                                                        "input": "$employees",
                                                        "as": "employee",
                                                        "cond": {
                                                            "$lte": [
                                                                "$$employee.joined_on",
                                                                "$$day_date",
                                                            ]
                                                        },
                                                    }
                                                }
                                            },
                                        },
                                        "in": {
                                            "$let": {
                                                "vars": {
                                                    "present_units": {
                                                        "$ifNull": [
                                                            "$$day_logs.present_half_day_units",
                                                            0,
                                                        ]
                                                    },
                                                    "working_day": {
                                                        "$lte": [
                                                            {
                                                                "$isoDayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$$day_date"
                                                                    }
                                                                }
                                                            },
                                                            5,
                                                        ]
                                                    },
                                                },
                                                "in": {
                                                    "date": "$$day_date",
                                                    "is_working_day": "$$working_day",
                                                    "headcount": "$$day_headcount",
                                                    "present_count": {
                                                        "$divide": ["$$present_units", 2]
                                                    },
                                                    "late_count": {
                                                        "$ifNull": [
                                                            "$$day_logs.late_count",
                                                            0,
                                                        ]
                                                    },
                                                    "attendance_rate": {
                                                        "$cond": [
                                                            {
                                                                "$and": [
                                                                    "$$working_day",
                                                                    {
                                                                        "$gt": [
                                                                            "$$day_headcount",
                                                                            0,
                                                                        ]
                                                                    },
                                                                ]
                                                            },
                                                            {
                                                                "$divide": [
                                                                    {
                                                                        "$floor": {
                                                                            "$divide": [
                                                                                {
                                                                                    "$add": [
                                                                                        {
                                                                                            "$multiply": [
                                                                                                "$$present_units",
                                                                                                10000,
                                                                                            ]
                                                                                        },
                                                                                        "$$day_headcount",
                                                                                    ]
                                                                                },
                                                                                {
                                                                                    "$multiply": [
                                                                                        "$$day_headcount",
                                                                                        2,
                                                                                    ]
                                                                                },
                                                                            ]
                                                                        }
                                                                    },
                                                                    10000,
                                                                ]
                                                            },
                                                            None,
                                                        ]
                                                    },
                                                },
                                            }
                                        },
                                    }
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$items"},
        {"$replaceRoot": {"newRoot": "$items"}},
        {
            "$set": {
                "_attendance_rate_units": {
                    "$cond": [
                        {"$eq": ["$attendance_rate", None]},
                        None,
                        {
                            "$round": [
                                {"$multiply": ["$attendance_rate", 10000]},
                                0,
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_rate_sum_units": {
                        "$sum": "$_attendance_rate_units",
                        "window": {"documents": [-6, 0]},
                    },
                    "_rate_count": {
                        "$sum": {
                            "$cond": [
                                {"$ne": ["$_attendance_rate_units", None]},
                                1,
                                0,
                            ]
                        },
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_rate_count", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {
                                                "$add": [
                                                    {
                                                        "$multiply": [
                                                            "$_rate_sum_units",
                                                            2,
                                                        ]
                                                    },
                                                    "$_rate_count",
                                                ]
                                            },
                                            {"$multiply": ["$_rate_count", 2]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                }
            }
        },
        {"$unset": ["_attendance_rate_units", "_rate_sum_units", "_rate_count"]},
        {"$sort": {"date": 1}},
        {
            "$group": {
                "_id": None,
                "department": {"$first": department},
                "items": {"$push": "$$ROOT"},
            }
        },
        {"$project": {"_id": 0, "department": 1, "items": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date or (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range must be ordered and no longer than 92 days")
    pipeline = _department_trend_pipeline(department, from_date, to_date)
    result = next(iter(db.employees.aggregate(pipeline)), None)
    if result is None:
        raise HTTPException(404, "department not found")
    return result


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        filter_query: dict = {}
        if emp_code:
            filter_query["emp_code"] = emp_code
        if date_from or date_to:
            filter_query["date"] = {}
            if date_from:
                filter_query["date"]["$gte"] = date_from.isoformat()
            if date_to:
                filter_query["date"]["$lte"] = date_to.isoformat()
        if status:
            filter_query["status"] = status
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": filter_query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        collection = "attendance_logs"
    else:
        if month is not None:
            _month_end(month)
        if endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(422, "emp_code and month are required")
            command = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": _employee_monthly_pipeline(emp_code, month),
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            collection = "employees"
        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(422, "month is required")
            command = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": _department_summary_pipeline(month, department),
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            collection = "employees"
        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, "month is required")
            command = {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": _late_leaderboard_pipeline(month, limit, department),
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            collection = "attendance_logs"
        else:
            if department is None or from_date is None or to_date is None:
                raise HTTPException(422, "department, from and to are required")
            if to_date < from_date or (to_date - from_date).days + 1 > 92:
                raise HTTPException(
                    422,
                    "date range must be ordered and no longer than 92 days",
                )
            command = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": _department_trend_pipeline(
                        department,
                        from_date,
                        to_date,
                    ),
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            collection = "employees"

    explanation = db.command(command)
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explanation)),
    }
