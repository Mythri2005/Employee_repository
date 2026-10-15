"""Employee Attendance & Analytics API."""

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined"
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="employee_date_unique"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="attendance_late_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="employee_punch_in"
    )


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_employee_fields(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.joined_on) is None:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, value: object) -> object:
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, value: object) -> object:
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_null_punch_values(cls, value: object) -> object:
        if isinstance(value, dict):
            for field in ("punch_in", "punch_out"):
                if field in value and value[field] is None:
                    raise ValueError(f"{field} must be an epoch-millisecond integer")
            if "status" in value and value["status"] is None:
                raise ValueError("status must be a valid attendance status")
        return value


# --------------------------------------------------------------------------- #
# Time, serialization, and aggregation helpers
# --------------------------------------------------------------------------- #
def utc_now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def from_epoch_ms(value: int) -> datetime:
    seconds = value // 1000
    return datetime.fromtimestamp(seconds, UTC)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return int(value.timestamp()) * 1000


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day


def shift_boundary(day: date, shift_time: str, next_day: bool = False) -> datetime:
    hour, minute = (int(part) for part in shift_time.split(":"))
    boundary_day = day + timedelta(days=1) if next_day else day
    return datetime.combine(boundary_day, time(hour, minute), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: date) -> int:
    start = shift_boundary(attendance_day, shift_start)
    elapsed = (punch_in.astimezone(IST) - start).total_seconds()
    if elapsed <= 10 * 60:
        return 0
    return int(elapsed // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(
    punch_out: datetime, shift_end: str, shift_start: str, attendance_day: date
) -> int:
    overnight = shift_end <= shift_start
    end = shift_boundary(attendance_day, shift_end, next_day=overnight)
    elapsed = (punch_out.astimezone(IST) - end).total_seconds()
    if elapsed < 30 * 60:
        return 0
    return int(elapsed // 60)


def round_half_up(value: object, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def employee_response(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    result["created_at"] = to_epoch_ms(result.get("created_at"))
    return result


def attendance_response(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    result["punch_in"] = to_epoch_ms(result["punch_in"])
    result["punch_out"] = to_epoch_ms(result["punch_out"])
    for entry in result["history"]:
        entry["at"] = to_epoch_ms(entry.get("at"))
        for field in ("punch_in", "punch_out"):
            if field in entry.get("changes", {}):
                change = entry["changes"][field]
                change["from"] = to_epoch_ms(change.get("from"))
                change["to"] = to_epoch_ms(change.get("to"))
    return result


def month_bounds(month: str) -> tuple[str, str, date]:
    try:
        first = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be a valid YYYY-MM month") from exc
    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)
    return first.isoformat(), next_month.isoformat(), next_month - timedelta(days=1)


def present_expr(record: str = "$log") -> dict:
    return {"$in": [f"{record}.status", list(PRESENT_STATUSES)]}


def weekday_expr(date_value: str) -> dict:
    weekday = {"$dayOfWeek": {"$dateFromString": {"dateString": date_value}}}
    return {"$and": [{"$gte": [weekday, 2]}, {"$lte": [weekday, 6]}]}


def half_up_expression(value: object, places: int) -> dict:
    scale = Decimal128(str(10**places))
    return {
        "$divide": [
            {
                "$floor": {
                    "$add": [
                        {"$multiply": [{"$toDecimal": value}, scale]},
                        Decimal128("0.5"),
                    ]
                }
            },
            scale,
        ]
    }


def half_up_ratio_expression(numerator: object, denominator: object, places: int) -> dict:
    return half_up_expression(
        {
            "$divide": [
                {"$toDecimal": numerator},
                {"$toDecimal": denominator},
            ]
        },
        places,
    )


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    month_start, next_month, month_end = month_bounds(month)
    return [
        {        "$match": {"emp_code": emp_code}},
        {
            "$addFields": {
                "month_start": {"$dateFromString": {"dateString": month_start}},
                "month_end": {
                    "$dateAdd": {
                        "startDate": {"$dateFromString": {"dateString": next_month}},
                        "unit": "day",
                        "amount": -1,
                    }
                },
                "joined_date": {"$dateFromString": {"dateString": "$joined_on"}},
            }
        },
        {
            "$addFields": {
                "effective_start": {
                    "$cond": [
                        {"$gt": ["$joined_date", "$month_start"]},
                        "$joined_date",
                        "$month_start",
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lt": ["$date", next_month]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "working_day_numbers": {
                    "$map": {
                        "input": {
                            "$cond": [
                                {"$lte": ["$effective_start", "$month_end"]},
                                {
                                    "$range": [
                                        0,
                                        {
                                            "$add": [
                                                {
                                                    "$dateDiff": {
                                                        "startDate": "$effective_start",
                                                        "endDate": "$month_end",
                                                        "unit": "day",
                                                    }
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                [],
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$effective_start",
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
                "record_weekday": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                        weekday_expr("$logs.date"),
                        False,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$working_day_numbers",
                            "as": "day",
                            "cond": {
                                "$and": [
                                    {"$gte": [{"$dayOfWeek": "$$day"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$$day"}, 6]},
                                ]
                            },
                        }
                    }
                },
                "present_value": {
                    "$cond": [
                        {"$and": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, present_expr("$logs")]},
                        {
                            "$cond": [
                                {
                                    "$and": [
                                        "$record_weekday",
                                        {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    ]
                                },
                                0.5,
                                {
                                    "$cond": [
                                        "$record_weekday",
                                        1,
                                        0,
                                    ]
                                },
                            ]
                        },
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "working_days": {"$first": "$working_days"},
                "present_days": {"$sum": "$present_value"},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    month_start, next_month, month_end = month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lt": ["$date", next_month]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "record_exists": {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                "is_weekday": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                        weekday_expr("$logs.date"),
                        False,
                    ]
                },
                "is_present": {
                    "$and": [
                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                        present_expr("$logs"),
                    ]
                },
                "present_value": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                                present_expr("$logs"),
                                weekday_expr("$logs.date"),
                            ]
                        },
                        {
                            "$cond": [
                                {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                0.5,
                                1,
                            ]
                        },
                        0,
                    ]
                },
                "work_hour_value": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                                present_expr("$logs"),
                                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                            ]
                        },
                        "$logs.work_hours",
                        0,
                    ]
                },
                "has_work_hours": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                                present_expr("$logs"),
                                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                            ]
                        },
                        1,
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": {"emp_code": "$emp_code", "department": "$department"},
                "present_days": {"$sum": "$present_value"},
                "work_hours_sum": {"$sum": "$work_hour_value"},
                "work_hours_count": {"$sum": "$has_work_hours"},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
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
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
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


def late_leaderboard_pipeline(month: str, department: Optional[str], limit: int) -> list[dict]:
    month_start, next_month, _ = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lt": next_month}, "late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$emp_code",
                    "name": {"$first": "$employee.name"},
                    "department": {"$first": "$employee.department"},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def department_trend_pipeline(department: str, from_day: date, to_day: date) -> list[dict]:
    start = {"$dateFromString": {"dateString": from_day.isoformat()}}
    end = {"$dateFromString": {"dateString": to_day.isoformat()}}
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {"$dateDiff": {"startDate": start, "endDate": end, "unit": "day"}},
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}},
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "day_employees",
            }
        },
        {"$unwind": {"path": "$day_employees", "preserveNullAndEmptyArrays": True}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "code": "$day_employees.emp_code",
                    "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}},
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$eq": ["$date", "$$day"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "log",
            }
        },
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$days",
                "headcount": {
                    "$sum": {
                        "$cond": [
                            {"$ne": [{"$ifNull": ["$day_employees.emp_code", None]}, None]},
                            1,
                            0,
                        ]
                    }
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": [{"$ifNull": ["$log.date", None]}, None]},
                                    present_expr("$log"),
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$log.half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                    ]
                },
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {
                                    "$gte": [{"$dayOfWeek": "$_id"}, 2]
                                },
                                {
                                    "$lte": [{"$dayOfWeek": "$_id"}, 6]
                                },
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        half_up_ratio_expression("$present_count", "$headcount", 4),
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": [{"$ifNull": ["$moving_avg_7d", None]}, None]},
                        None,
                        half_up_expression("$moving_avg_7d", 4),
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
    ]


def run_explain(endpoint: str, collection_name: str, pipeline: list[dict]) -> dict:
    explanation = db.command(
        "explain",
        {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": json_safe_explain(explanation),
    }


def json_safe_explain(value: object) -> object:
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, dict):
        return {key: json_safe_explain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe_explain(item) for item in value]
    return value


# --------------------------------------------------------------------------- #
# System and employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict:
    doc = body.model_dump()
    doc["created_at"] = utc_now_seconds()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    docs = docs.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_response(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = from_epoch_ms(body.punched_at) if body.punched_at is not None else utc_now_seconds()
    day = attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    record = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return attendance_response(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_response(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = from_epoch_ms(body.punched_at) if body.punched_at is not None else utc_now_seconds()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    punch_in_time = record["punch_in"]
    elapsed = (punched_at - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    day = date.fromisoformat(record["date"])
    overnight = employee["shift_end"] <= employee["shift_start"]
    end = shift_boundary(day, employee["shift_end"], next_day=overnight)
    overtime_seconds = (punched_at.astimezone(IST) - end).total_seconds()
    overtime = int(overtime_seconds // 60) if overtime_seconds >= 30 * 60 else 0
    hours = compute_work_hours(punch_in_time, punched_at)
    updated = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": hours,
                "overtime_minutes": overtime,
                "half_day": hours < 4.5,
            }
        },
    )
    if updated.modified_count != 1:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return attendance_response(db.attendance_logs.find_one({"_id": record["_id"]}))


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str, record_date: Annotated[date, Path(alias="date")], body: RegularizeIn
) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date.isoformat()})
    if old is None:
        raise HTTPException(status_code=404, detail="attendance record not found")
    supplied = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and supplied.intersection({"punch_in", "punch_out"}):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot be supplied with punch times")

    status = body.status if "status" in supplied else old["status"]
    punch_in_time = (
        from_epoch_ms(body.punch_in) if "punch_in" in supplied else old.get("punch_in")
    )
    punch_out_time = (
        from_epoch_ms(body.punch_out) if "punch_out" in supplied else old.get("punch_out")
    )
    if status in ("ABSENT", "LEAVE"):
        punch_in_time = None
        punch_out_time = None
    elif punch_in_time is None:
        raise HTTPException(status_code=422, detail="a presence status requires punch_in")
    if punch_in_time is not None:
        punch_in_time = punch_in_time.replace(microsecond=0)
        actual_day = attendance_date(punch_in_time, employee["shift_start"], employee["shift_end"])
        if actual_day != record_date:
            raise HTTPException(status_code=422, detail="punch_in must fall on the record attendance date")
    if punch_out_time is not None:
        punch_out_time = punch_out_time.replace(microsecond=0)
        if punch_in_time is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        elapsed = (punch_out_time - punch_in_time).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if punch_in_time is None:
        derived = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        derived = {
            "late_minutes": compute_late_minutes(
                punch_in_time, employee["shift_start"], record_date
            ),
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
        if punch_out_time is not None:
            hours = compute_work_hours(punch_in_time, punch_out_time)
            derived.update(
                work_hours=hours,
                overtime_minutes=compute_overtime(
                    punch_out_time,
                    employee["shift_end"],
                    employee["shift_start"],
                    record_date,
                ),
                half_day=hours < 4.5,
            )

    final_values = {
        "status": status,
        "punch_in": punch_in_time,
        "punch_out": punch_out_time,
        **derived,
    }
    old_values = {
        "status": old.get("status"),
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        "work_hours": old.get("work_hours"),
        "late_minutes": old.get("late_minutes", 0),
        "overtime_minutes": old.get("overtime_minutes", 0),
        "half_day": old.get("half_day", False),
    }
    changes = {
        key: {"from": old_values[key], "to": value}
        for key, value in final_values.items()
        if old_values[key] != value
    }
    if not changes:
        raise HTTPException(status_code=422, detail="regularization does not change the record")
    entry = {
        "at": utc_now_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_filter: dict = {"_id": old["_id"]}
    if "history" in old:
        history_filter["history"] = old["history"]
    else:
        history_filter["history"] = {"$exists": False}
    result = db.attendance_logs.update_one(
        history_filter,
        {"$set": final_values, "$push": {"history": entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry regularization")
    return attendance_response(db.attendance_logs.find_one({"_id": old["_id"]}))


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month) -> dict:
    result = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not result:
        raise HTTPException(status_code=404, detail="employee not found")
    row = result[0]
    working_days = row["working_days"]
    present_days = round_half_up(row["present_days"])
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": row["leave_days"],
        "late_count": row["late_count"],
        "total_late_minutes": row["total_late_minutes"],
        "total_overtime_minutes": row["total_overtime_minutes"],
        "attendance_pct": (
            round_half_up(Decimal(str(present_days)) * 100 / Decimal(working_days), 4)
            if working_days
            else None
        ),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None) -> dict:
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for row in rows:
        row["present_days"] = round_half_up(row["present_days"])
        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = round_half_up(row["avg_work_hours"])
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Month,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
) -> dict:
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, department, limit)))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_day: Annotated[date, Query(alias="from")],
    to_day: Annotated[date, Query(alias="to")],
) -> dict:
    if to_day < from_day:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    rows = list(db.employees.aggregate(department_trend_pipeline(department, from_day, to_day)))
    for row in rows:
        if row["attendance_rate"] is not None:
            row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Explain endpoint
# --------------------------------------------------------------------------- #
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
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    from_day: Annotated[Optional[date], Query(alias="from")] = None,
    to_day: Annotated[Optional[date], Query(alias="to")] = None,
) -> dict:
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        query: dict = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None:
                query["date"]["$gte"] = date_from.isoformat()
            if date_to is not None:
                query["date"]["$lte"] = date_to.isoformat()
        if status is not None:
            query["status"] = status
        explanation = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_safe_explain(explanation),
        }

    if endpoint == "employee_monthly":
        if not emp_code or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc
        return run_explain(
            endpoint, "employees", employee_monthly_pipeline(emp_code, month)
        )

    if endpoint in ("department_summary", "late_leaderboard"):
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc
        if endpoint == "department_summary":
            return run_explain(
                endpoint, "employees", department_summary_pipeline(month, department)
            )
        return run_explain(
            endpoint, "attendance_logs", late_leaderboard_pipeline(month, department, limit)
        )

    if department is None or from_day is None or to_day is None:
        raise HTTPException(status_code=422, detail="department, from, and to are required")
    if to_day < from_day or (to_day - from_day).days + 1 > 92:
        raise HTTPException(status_code=422, detail="invalid trend date range")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    return run_explain(
        endpoint, "employees", department_trend_pipeline(department, from_day, to_day)
    )
