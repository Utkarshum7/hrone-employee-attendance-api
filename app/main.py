"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Layout of this single file: config -> time helpers -> business rules (R1-R5) -> models ->
serialisers -> attendance/employee endpoints -> analytics pipelines -> explain endpoint.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: variables already in the environment take priority
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# Configuration / database
# --------------------------------------------------------------------------- #
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # PyMongo would otherwise hand back naive datetimes
    serverSelectionTimeoutMS=int(os.getenv("MONGO_TIMEOUT_MS", "5000")),
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the index already exists."""
    global _indexes_ready
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    # one record per (emp_code, date): the race-proof guarantee for punch-in; also serves per-employee queries
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # global list / month range scans: sort date desc, emp_code asc
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    # punch-out: latest record of an employee with punch_in <= t
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the process alive; /health reports 503 until MongoDB is back
        log.warning("index creation failed at startup: %s", exc.__class__.__name__)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("database error: %r", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def unprocessable(loc: str, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's usual body shape, for rules the schema cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": ("body", loc), "msg": msg, "input": None}])


def not_found(msg: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": msg})


def conflict(msg: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": msg})


# --------------------------------------------------------------------------- #
# Time helpers. API = epoch milliseconds, MongoDB = tz-aware UTC datetimes, calendar = IST strings
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
EpochMs = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]  # strict: rejects floats and strings
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def comparable(value: Any) -> Any:
    """Datetimes compare as epoch ms (naive vs aware safe); everything else as-is."""
    return dt_to_ms(value) if isinstance(value, datetime) else value


def now_dt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_date(value: str, loc: str = "date") -> date:
    if not DATE_RE.match(value):
        raise RequestValidationError([{"type": "value_error", "loc": ("query", loc), "msg": "expected YYYY-MM-DD", "input": value}])
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", loc), "msg": "invalid calendar date", "input": value}])


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "month"), "msg": "invalid year", "input": month}])
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def weekdays_between(start: date, end: date) -> int:
    """Number of Mon-Fri days in [start, end]."""
    if end < start:
        return 0
    total = (end - start).days + 1
    full_weeks, extra = divmod(total, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Business rules R1-R5 (pure functions)
# --------------------------------------------------------------------------- #
def hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; early-morning punches of an overnight shift belong to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def at_ist(day: date, clock: str, plus_days: int = 0) -> datetime:
    return datetime.combine(day + timedelta(days=plus_days), hhmm(clock), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, day: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((punch_in - at_ist(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half-up (Decimal avoids float and banker's rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, day: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only when at least 30. Overnight shifts end on the next calendar day."""
    end = at_ist(day, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derived_fields(emp: dict, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: date) -> dict:
    """The four stored derived values for a final (status, punch_in, punch_out) state."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {"work_hours": None, "late_minutes": compute_late_minutes(punch_in, day, emp["shift_start"]),
           "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=hours, overtime_minutes=compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"]),
                   half_day=hours < 4.5)  # R5 on the rounded value
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("expected YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class AttendanceStatus(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def skip_for(page: int, page_size: int) -> int:
    """Rows to skip. Capped below MongoDB's int64 limit so an absurd page gives an empty page instead of an error."""
    return min((page - 1) * page_size, 2 ** 62)


# --------------------------------------------------------------------------- #
# Serialisers (BSON -> API shape). _id never leaves the service.
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {"emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"], "department": doc["department"],
            "shift_start": doc["shift_start"], "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
            "created_at": dt_to_ms(doc.get("created_at"))}


def _history_value(field: str, value: Any) -> Any:
    return dt_to_ms(value) if field in ("punch_in", "punch_out") and isinstance(value, datetime) else value


def attendance_out(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        changes = {f: {"from": _history_value(f, c.get("from")), "to": _history_value(f, c.get("to"))}
                   for f, c in (h.get("changes") or {}).items()}
        history.append({"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return {"emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
            "punch_in": dt_to_ms(doc.get("punch_in")), "punch_out": dt_to_ms(doc.get("punch_out")),
            "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes") or 0,
            "overtime_minutes": doc.get("overtime_minutes") or 0, "half_day": bool(doc.get("half_day", False)),
            "history": history}


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    if not _indexes_ready:  # startup ran while MongoDB was down: retry, but readiness only means "MongoDB answers"
        try:
            ensure_indexes()
        except PyMongoError as exc:
            log.warning("index creation failed: %s", exc.__class__.__name__)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        employees.insert_one(doc)  # the unique index on emp_code decides races
    except DuplicateKeyError:
        return conflict("emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip(skip_for(page, page_size)).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        return not_found("employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status, "punch_in": ts, "punch_out": None,
           **derived_fields(emp, body.status, ts, None, day), "history": []}
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        return conflict("already punched in for this date")
    return JSONResponse(status_code=201, content=attendance_out(doc))


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        return not_found("employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        return not_found("no punch-in found")
    if rec.get("punch_out") is not None:
        return conflict("already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts > pin + timedelta(hours=24):
        raise unprocessable("punched_at", "punch-out must be after punch-in and within 24 hours of it")
    day = date.fromisoformat(rec["date"])
    new = derived_fields(emp, rec["status"], pin, ts, day)
    # the filter re-checks "still open", so of two simultaneous punch-outs only one matches
    updated = logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None, "punch_in": pin},
        {"$set": {"punch_out": ts, **new}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        return conflict("already punched out")
    return attendance_out(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[AttendanceStatus] = None, page: Page = 1, page_size: PageSize = 20):
    q, sort, skip = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q)
    cursor = logs.find(q).sort(sort).skip(skip).limit(page_size)
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size):
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    start = parse_date(date_from, "date_from").isoformat() if date_from else None
    end = parse_date(date_to, "date_to").isoformat() if date_to else None
    if start and end and start > end:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "date_from"), "msg": "date_from must be <= date_to", "input": date_from}])
    if start or end:
        q["date"] = {**({"$gte": start} if start else {}), **({"$lte": end} if end else {})}
    if status:
        q["status"] = status.value
    return q, [("date", DESCENDING), ("emp_code", ASCENDING)], skip_for(page, page_size)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, "date")
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        return not_found("employee not found")
    rec = logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if rec is None:
        return not_found("no attendance record for that date")

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    status = body.status or old_status
    if status in PRESENCE:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise unprocessable("punch_in", "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            raise unprocessable("punch_in", "punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
            raise unprocessable("punch_out", "punch_out must be after punch_in and within 24 hours of it")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable("punch_in", "punch times cannot be supplied with status ABSENT or LEAVE")
        new_in = new_out = None

    new_values = {"status": status, "punch_in": new_in, "punch_out": new_out,
                  **derived_fields(emp, status, new_in, new_out, day)}
    old_values = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
                  "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
                  "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {f: {"from": old_values[f], "to": v} for f, v in new_values.items() if comparable(v) != comparable(old_values[f])}
    if not any(f in changes for f in ("status", "punch_in", "punch_out")):
        raise unprocessable("body", "the request changes nothing")

    entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_history = len(rec.get("history") or [])
    # optimistic guard: only apply if nobody changed the record since we read it (same history length and punch state)
    guard = {"emp_code": emp_code, "date": rec["date"], "status": old_status, "punch_in": old_in, "punch_out": old_out,
             f"history.{n_history}": {"$exists": False}}
    if n_history:
        guard[f"history.{n_history - 1}"] = {"$exists": True}
    updated = logs.find_one_and_update(guard, {"$set": new_values, "$push": {"history": entry}},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        return conflict("record was modified concurrently, retry")
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# Analytics. Reported numbers are rounded half-up inside MongoDB with Decimal128 arithmetic
# ($round is banker's rounding, so it is not used).
# --------------------------------------------------------------------------- #
def half_up(expr: Any, places: int) -> dict:
    scale = Decimal128(str(10 ** places))
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}}, scale]}


def num(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value.to_decimal()) if isinstance(value, Decimal128) else float(value)


IS_WEEKDAY = {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date", "format": "%Y-%m-%d"}}}, [1, 7]]}]}
IS_PRESENT = {"$in": ["$$l.status", PRESENCE]}
LATE = {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}


def date_range(first: date, last: date) -> dict:
    return {"$gte": first.isoformat(), "$lte": last.isoformat()}


def present_weight() -> dict:
    """1 for a present record, 0.5 for a present half day, 0 for non-present."""
    return {"$cond": [IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1]}, 0]}


def employee_monthly_pipeline(emp_code: str, month: str, working_days: int) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": date_range(first, last)}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$let": {"vars": {"l": "$$ROOT"}, "in": {"$cond": [IS_WEEKDAY, present_weight(), 0]}}}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
        {"$set": {"attendance_pct": {"$cond": [{"$gt": [working_days, 0]},
                                               half_up({"$multiply": [{"$divide": [{"$toDecimal": "$present_days"}, working_days]}, 100]}, 2), None]}}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_PATTERN)]):
    first, last = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        return not_found("employee not found")
    working_days = weekdays_between(max(first, date.fromisoformat(emp["joined_on"])), last)
    row = next(logs.aggregate(employee_monthly_pipeline(emp_code, month, working_days)), None) or {
        "present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0,
        "attendance_pct": 0.0 if working_days else None}
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": num(row["present_days"]),
            "leave_days": int(row["leave_days"]), "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]), "total_overtime_minutes": int(row["total_overtime_minutes"]),
            "attendance_pct": num(row["attendance_pct"])}


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined on/before month end, logs or not
    if department:
        emp_match["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$$l.work_hours"}]}

    def over_logs(expr: Any) -> dict:
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    return [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs",
                     "pipeline": [{"$match": {"date": date_range(first, last)}},
                                  {"$project": {"_id": 0, "date": 1, "status": 1, "late_minutes": 1, "work_hours": 1, "half_day": 1}}]}},
        {"$project": {
            "department": 1,
            "present_days": over_logs({"$cond": [IS_WEEKDAY, present_weight(), 0]}),
            "hours_sum": over_logs({"$cond": [has_hours, {"$toDecimal": "$$l.work_hours"}, Decimal128("0")]}),
            "hours_n": over_logs({"$cond": [has_hours, 1, 0]}),
            "late_count": over_logs({"$cond": [LATE, 1, 0]}),
            "total_late_minutes": over_logs({"$ifNull": ["$$l.late_minutes", 0]}),
            "leave_count": over_logs({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
            "on_duty_count": over_logs({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]})}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},
                    "hours_sum": {"$sum": "$hours_sum"}, "hours_n": {"$sum": "$hours_n"}, "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"}, "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1,
                      "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
                      "avg_work_hours": {"$cond": [{"$gt": ["$hours_n", 0]}, half_up({"$divide": ["$hours_sum", "$hours_n"]}, 2), None]}}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_PATTERN)], department: Optional[str] = None):
    items = [{"department": r["department"], "headcount": int(r["headcount"]), "present_days": num(r["present_days"]),
              "avg_work_hours": num(r["avg_work_hours"]), "late_count": int(r["late_count"]),
              "total_late_minutes": int(r["total_late_minutes"]), "leave_count": int(r["leave_count"]),
              "on_duty_count": int(r["on_duty_count"])}
             for r in employees.aggregate(department_summary_pipeline(month, department))]
    return {"month": month, "items": items}


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": date_range(first, last)}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},  # ties share a rank, next skipped
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Annotated[str, Query(pattern=MONTH_PATTERN)], limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
              "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
             for r in logs.aggregate(late_leaderboard_pipeline(month, limit, department))]
    return {"month": month, "items": items}


def trend_pipeline(emp_codes: list[str], joined: list[str], start: date, end: date) -> list:
    day = {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}
    start_dt = datetime.combine(start, time(), tzinfo=UTC)
    end_dt = datetime.combine(end + timedelta(days=1), time(), tzinfo=UTC)  # $densify upper bound is exclusive
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": date_range(start, end)}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": {"$let": {"vars": {"l": "$$ROOT"}, "in": present_weight()}}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$set": {"d": day}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},  # one row per calendar day
        {"$set": {"date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                  "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$d"}, [1, 7]]}]},
                  "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": joined, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                               half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"moving": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": 1,
                      "moving_avg_7d": {"$cond": [{"$eq": [{"$ifNull": ["$moving", None]}, None]}, None, half_up("$moving", 4)]}}},
    ]


def trend_inputs(department: str, start: str, end: str) -> tuple[list[str], list[str], date, date]:
    first, last = parse_date(start, "from"), parse_date(end, "to")
    if last < first or (last - first).days + 1 > 92 or last >= date.max:  # date.max: the day after it cannot be represented
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "to"), "msg": "to must be >= from and the range at most 92 days", "input": end}])
    rows = list(employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))
    return [r["emp_code"] for r in rows], sorted(r["joined_on"] for r in rows), first, last


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    codes, joined, first, last = trend_inputs(department, from_, to)
    if not codes:
        return not_found("department not found")
    items = [{"date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
              "present_count": num(r["present_count"]), "late_count": int(r["late_count"]),
              "attendance_rate": num(r.get("attendance_rate")), "moving_avg_7d": num(r.get("moving_avg_7d"))}
             for r in logs.aggregate(trend_pipeline(codes, joined, first, last))]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def require(**params: Any) -> None:
    missing = [k for k, v in params.items() if v in (None, "")]
    if missing:
        raise RequestValidationError([{"type": "missing", "loc": ("query", k), "msg": "Field required", "input": None} for k in missing])


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: ExplainTarget, emp_code: Optional[str] = None, month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
            department: Optional[str] = None, limit: Annotated[int, Query(ge=1, le=50)] = 10, date_from: Optional[str] = None,
            date_to: Optional[str] = None, status: Optional[AttendanceStatus] = None, from_: Annotated[Optional[str], Query(alias="from")] = None,
            to: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    if endpoint is ExplainTarget.attendance_list:
        q, sort, skip = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q, "sort": dict(sort), "skip": skip, "limit": page_size}
    else:
        if endpoint is ExplainTarget.employee_monthly:
            require(emp_code=emp_code, month=month)
            first, last = month_bounds(month)
            emp = employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
            start = max(first, date.fromisoformat(emp["joined_on"])) if emp else first
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month, weekdays_between(start, last))
        elif endpoint is ExplainTarget.department_summary:
            require(month=month)
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint is ExplainTarget.late_leaderboard:
            require(month=month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:
            require(department=department, **{"from": from_, "to": to})
            codes, joined, first, last = trend_inputs(department, from_, to)
            collection, pipeline = "attendance_logs", trend_pipeline(codes, joined, first, last)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint.value, "collection": collection, "explain": json.loads(json_util.dumps(result))}
