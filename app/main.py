"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file
    1. configuration and constants
    2. time, rounding and rule helpers (R1 - R5, R8)
    3. request models and serialisation
    4. employees / attendance endpoints
    5. aggregation pipeline builders + analytics endpoints (R6 - R9)
    6. explain endpoint
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and constants
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones, so
# arithmetic between stored and incoming instants can never mix naive and aware values.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92
MAX_PAGE = 10_000_000  # keeps skip() inside a 32-bit int

MIN_EPOCH_MS, MAX_EPOCH_MS = 100_000_000_000, 4_102_444_800_000
# strict=True: floats, strings and booleans are rejected with 422, only a JSON integer passes.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
HHMM_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
CODE_RE = re.compile(r"EMP[0-9]{4,6}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# Every index is created at startup (idempotent: same name and keys -> no-op).
INDEXES = {
    "employees": [
        # unique business key: enforces "one employee per emp_code" even under concurrent creates
        dict(keys=[("emp_code", ASCENDING)], unique=True, name="uniq_emp_code"),
        # GET /employees?department=  (filter + sort by emp_code), trend/summary department lookups
        dict(keys=[("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code"),
        # R9 headcount: department + joined_on <= end of period
        dict(keys=[("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on"),
        # department summary without a department filter
        dict(keys=[("joined_on", ASCENDING)], name="joined_on"),
    ],
    "attendance_logs": [
        # one record per (emp_code, date): the punch-in race guard; also serves employee monthly,
        # per-employee listing, and the $lookup of logs per employee in the analytics
        dict(keys=[("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"),
        # GET /attendance without emp_code (sort date desc, emp_code asc) and the monthly leaderboard
        dict(keys=[("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp"),
        # GET /attendance?status=...: equality prefix, then the same sort order
        dict(keys=[("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"),
        # punch-out: "latest record of this employee whose punch_in <= punched_at"
        dict(keys=[("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in"),
    ],
}
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    for coll, specs in INDEXES.items():
        for spec in specs:
            spec = dict(spec)
            db[coll].create_index(spec.pop("keys"), **spec)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the process alive; /health reports 503 until Mongo answers
        log.warning("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def unprocessable(loc: tuple, msg: str):
    """Raise a 422 with FastAPI's standard validation body, for checks pydantic cannot express."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# 2. Time, rounding and rule helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat a naive value as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    """Current time, millisecond precision (what MongoDB stores)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def num(value: Any, places: int) -> float:
    """R8: round half-up (not banker's rounding, not binary-float rounding)."""
    return float(to_decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_date(value: str, loc: tuple) -> date:
    if not DATE_RE.fullmatch(value):
        unprocessable(loc, "must be a date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date")


def parse_month(value: str, loc: tuple = ("query", "month")) -> tuple[date, date]:
    """'YYYY-MM' -> (first day, last day)."""
    if not MONTH_RE.fullmatch(value):
        unprocessable(loc, "must be a month in YYYY-MM format")
    year, month = int(value[:4]), int(value[5:])
    try:
        first = date(year, month, 1)
        nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    except ValueError:
        unprocessable(loc, "not a valid month")
    return first, nxt - timedelta(days=1)


def at_ist(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = date.fromisoformat(date_str) + timedelta(days=plus_days)
    h, m = int(hhmm[:2]), int(hhmm[3:])
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs
    to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < time(int(shift_end[:2]), int(shift_end[3:])):
        d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start.
    The shift start is anchored on the attendance date in IST (so an overnight shift works)."""
    delta = int((punch_in.replace(microsecond=0) - at_ist(date_str, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals, using exact decimal arithmetic."""
    secs = int((punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds())
    return num(Decimal(secs) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes past shift end (next calendar day for an overnight shift), only if >= 30."""
    end = at_ist(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    minutes = int((punch_out.replace(microsecond=0) - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: judged on the already rounded work_hours."""
    return Decimal(repr(work_hours)) < HALF_DAY_BELOW


def derive(emp: dict, date_str: str, status: str, punch_in, punch_out) -> dict:
    """All stored derived fields for a final (status, punch_in, punch_out) - used by punch-out and PATCH."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp["shift_start"], date_str)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str),
        "half_day": compute_half_day(hours),
    }


# --------------------------------------------------------------------------- #
# 3. Request models and serialisation
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def _code(cls, v):
        if not CODE_RE.fullmatch(v):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if not EMAIL_RE.fullmatch(v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def _hhmm(cls, v):
        if not HHMM_RE.fullmatch(v):
            raise ValueError("must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def _joined(cls, v):
        if not DATE_RE.fullmatch(v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError -> 422 for 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields (work_hours, late_minutes, ...) are not declared: if a client sends them they are ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null; omit it to leave it unchanged")
        return self


def serialize_employee(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }


def _ms_if_dt(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def serialize_history(h: dict) -> dict:
    return {
        "at": dt_to_ms(h.get("at")),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": {
            k: {"from": _ms_if_dt(c.get("from")), "to": _ms_if_dt(c.get("to"))}
            for k, c in (h.get("changes") or {}).items()
        },
    }


def serialize_record(d: dict) -> dict:
    """Stored document -> API shape. Missing legacy fields get their documented defaults."""
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [serialize_history(h) for h in (d.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Employees and attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides the winner of a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc().replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc().replace(microsecond=0)
    # latest record whose punch_in <= punched_at: works across midnight because it ignores the date
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin:
        unprocessable(("body", "punched_at"), "punched_at must be after the punch-in")
    if ts - pin > MAX_SHIFT:
        unprocessable(("body", "punched_at"), "punched_at is more than 24 hours after the punch-in")
    d = derive(emp, rec["date"], rec["status"], pin, ts)
    d.pop("late_minutes")  # late_minutes belongs to punch-in and is left as stored
    # compare-and-set on "still open": of two simultaneous punch-outs exactly one matches
    res = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **d}},
        return_document=ReturnDocument.AFTER,
    )
    if res is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(res)


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Filter, sort, skip and limit of GET /attendance. Shared with the explain endpoint."""
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    # with emp_code fixed, (emp_code, date) is unique, so sorting by date alone is the same order
    # and lets the (emp_code, date) index serve the sort
    sort = {"date": -1} if emp_code is not None else {"date": -1, "emp_code": 1}
    return q, sort, (page - 1) * page_size, page_size


def validate_attendance_params(date_from, date_to):
    f = parse_date(date_from, ("query", "date_from")) if date_from is not None else None
    t = parse_date(date_to, ("query", "date_to")) if date_to is not None else None
    if f and t and f > t:
        unprocessable(("query", "date_from"), "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_attendance_params(date_from, date_to)
    q, sort, skip, limit = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cur = db.attendance_logs.find(q, {"_id": 0}).sort(list(sort.items())).skip(skip).limit(limit)
    return {"items": [serialize_record(d) for d in cur], "total": total, "page": page, "page_size": page_size}


CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or rec["status"]

    if new_status not in PRESENCE:  # ABSENT / LEAVE clear both punch times
        if "punch_in" in sent or "punch_out" in sent:
            unprocessable(("body",), "punch times cannot be supplied with ABSENT/LEAVE; send a presence status to set them")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else old_in
        new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else old_out
        if new_in is None:
            unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            unprocessable(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT):
            unprocessable(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derive(emp, date, new_status, new_in, new_out)}
    old = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in CHANGE_FIELDS if old[k] != new[k]}
    if not changes:
        unprocessable(("body",), "the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    # Optimistic lock: only apply if the record is still exactly as read (same punch times, status and
    # exactly n history entries). A concurrent correction or punch-out makes this match nothing -> 409,
    # so no history entry is ever lost or overwritten.
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old["status"],
            "punch_in": old_in,
            "punch_out": old_out,
            f"history.{n}": {"$exists": False},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if res is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return serialize_record(res)


# --------------------------------------------------------------------------- #
# 5. Analytics: aggregation pipelines
# --------------------------------------------------------------------------- #
# Reusable expression fragments. They only read STORED values (never recompute from punch times).
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
# "date" is a YYYY-MM-DD calendar string; read as UTC midnight its weekday is the calendar weekday.
# $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6.
DAY_OF_WEEK = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}}
IS_WEEKDAY = {"$in": [DAY_OF_WEEK, [2, 3, 4, 5, 6]]}
DAY_VALUE = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}  # R5: half day = 0.5
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def month_group_fields() -> dict:
    return {
        "present_days": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, DAY_VALUE, 0]}},  # R6 + R7
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": IS_LATE},
        "late_total": {"$sum": LATE_MIN},
        "overtime_total": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # Decimal128 keeps the sum of 2-decimal hours exact; the mean is formed from sum / count later
        "hours_sum": {"$sum": {"$cond": [HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
        "hours_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
    }


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # (emp_code, date) index
        {"$group": {"_id": None, **month_group_fields()}},
    ]


def working_days(first: date, last: date) -> int:
    """R7: Monday-Friday days in [first, last] (at most 31 iterations; no documents involved)."""
    return sum(1 for i in range((last - first).days + 1) if (first + timedelta(days=i)).weekday() < 5)


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    # Start from EMPLOYEES so people with zero logs are still counted (R9), then pull each person's
    # month of logs through an indexed $lookup and reduce it to one small document.
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$group": {"_id": None, **month_group_fields()}},
                ],
                "as": "m",
            }
        },
        {"$set": {"m": {"$arrayElemAt": ["$m", 0]}}},  # null for employees without logs
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{
                    f: {"$sum": {"$ifNull": [f"$m.{f}", 0]}}
                    for f in ("present_days", "leave_days", "on_duty_count", "late_count", "late_total", "hours_sum", "hours_n")
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    emp_match = [{"$match": {"department": department}}] if department is not None else []
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": emp_match + [{"$project": {"_id": 0, "name": 1, "department": 1}}],
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # drops logs of unknown employees and (with a filter) other departments
        # $rank is standard competition ranking: 1, 2, 2, 4. Ranking happens BEFORE the limit.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


def trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    """One row per calendar day. Runs on employees (indexed by department); logs are joined per employee."""
    start, end = d_from.isoformat(), d_to.isoformat()
    utc_midnight = lambda d: datetime(d.year, d.month, d.day, tzinfo=UTC)  # noqa: E731
    day_str = {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}
    ratio = {"$divide": [{"$toDecimal": "$present"}, {"$toDecimal": "$headcount"}]}
    # round half-up to 4 decimals in exact Decimal128 arithmetic ($round would round half to even)
    rate4 = {"$divide": [{"$floor": {"$add": [{"$multiply": [ratio, 10000]}, Decimal128("0.5")]}}, 10000]}
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$project": {"_id": 0, "date": 1, "p": {"$cond": [IS_PRESENT, DAY_VALUE, 0]}, "l": IS_LATE}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        {"$group": {"_id": "$logs.date", "present": {"$sum": "$logs.p"}, "late": {"$sum": "$logs.l"}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}, "present": 1, "late": 1}},
        # anchor rows on the first and last day so $densify spans the whole range even if no logs exist
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$replaceWith": {"d": {"$literal": [utc_midnight(d_from), utc_midnight(d_to)]}}},
                    {"$unwind": "$d"},
                    {"$replaceWith": {"day": "$d", "present": 0, "late": 0}},
                ],
            }
        },
        {"$group": {"_id": "$day", "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
        {"$project": {"_id": 0, "day": "$_id", "present": 1, "late": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},  # gap filling
        {"$set": {"present": {"$ifNull": ["$present", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        {
            "$lookup": {  # R9: headcount that day = department employees with joined_on <= day
                "from": "employees",
                "let": {"d": day_str},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "dow": {"$dayOfWeek": "$day"}}},
        {"$set": {"working": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        {"$set": {"rate": {"$cond": [{"$and": ["$working", {"$gt": ["$headcount", 0]}]}, rate4, None]}}},
        {
            "$setWindowFields": {  # shorter window at the start: rows before "from" do not exist here
                "sortBy": {"day": 1},
                "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": day_str,
                "is_working_day": "$working",
                "headcount": 1,
                "present_count": "$present",
                "late_count": "$late",
                "rate": 1,
                "moving": 1,
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last = parse_month(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    m = rows[0] if rows else {}
    joined = date.fromisoformat(emp["joined_on"])
    wd = working_days(max(first, joined), last)  # R7: count from joined_on for mid-month joiners
    present = to_decimal(m.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": num(present, 2),
        "leave_days": int(m.get("leave_days", 0)),
        "late_count": int(m.get("late_count", 0)),
        "total_late_minutes": int(m.get("late_total", 0)),
        "total_overtime_minutes": int(m.get("overtime_total", 0)),
        "attendance_pct": None if wd == 0 else num(present * 100 / Decimal(wd), 2),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = parse_month(month)
    rows = db.employees.aggregate(department_summary_pipeline(first.isoformat(), last.isoformat(), department))
    items = []
    for r in rows:
        n = int(r["hours_n"])
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": num(r["present_days"], 2),
                # mean over RECORDS: total hours / number of records, not an average of averages
                "avg_work_hours": num(to_decimal(r["hours_sum"]) / Decimal(n), 2) if n else None,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["late_total"]),
                "leave_count": int(r["leave_days"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = parse_month(month)
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


def validate_trend_range(from_: str, to: str) -> tuple[date, date]:
    d_from = parse_date(from_, ("query", "from"))
    d_to = parse_date(to, ("query", "to"))
    if d_to < d_from:
        unprocessable(("query", "to"), "to must not be before from")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        unprocessable(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = validate_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = []
    for r in db.employees.aggregate(trend_pipeline(department, d_from, d_to)):
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": num(r["present_count"], 2),
                "late_count": int(r["late_count"]),
                "attendance_rate": None if r.get("rate") is None else num(r["rate"], 4),
                "moving_avg_7d": None if r.get("moving") is None else num(r["moving"], 4),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 6. Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(value, name):
        if value is None:
            unprocessable(("query", name), f"{name} is required for {endpoint}")

    # Each branch builds the query with the very same builder function the real endpoint uses.
    if endpoint == "attendance_list":
        validate_attendance_params(date_from, date_to)
        q, sort, skip, lim = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll, cmd = "attendance_logs", {"find": "attendance_logs", "filter": q, "sort": sort, "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            need(emp_code, "emp_code"), need(month, "month")
            first, last = parse_month(month)
            coll, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())
        elif endpoint == "department_summary":
            need(month, "month")
            first, last = parse_month(month)
            coll, pipeline = "employees", department_summary_pipeline(first.isoformat(), last.isoformat(), department)
        elif endpoint == "late_leaderboard":
            need(month, "month")
            first, last = parse_month(month)
            coll, pipeline = "attendance_logs", late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
        else:
            need(department, "department"), need(from_, "from"), need(to, "to")
            d_from, d_to = validate_trend_range(from_, to)
            coll, pipeline = "employees", trend_pipeline(department, d_from, d_to)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", cmd, verbosity="executionStats")
    # explain output holds BSON types (ObjectId, datetimes, Int64, Decimal128): make it plain JSON
    return {
        "endpoint": endpoint,
        "collection": coll,
        "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
