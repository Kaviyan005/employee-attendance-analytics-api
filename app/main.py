"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi import Query
from pydantic import BaseModel
from pymongo import MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employees_department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="ix_attendance_emp_punchin",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", 1)],
        name="ix_attendance_status_date",
    )

IST = timezone(timedelta(hours=5, minutes=30))

# TODO: adjust to the values listed in openapi.yaml
ALLOWED_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
NON_PRESENCE_STATUSES = {"ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def startup_event():
    create_indexes()

# --------------------------------------------------------------------------- #
# Helpers
# All datetimes are stored as naive IST wall-clock values, consistently.
# --------------------------------------------------------------------------- #
def now_ist() -> datetime:
    return datetime.now(IST).replace(microsecond=0)

def from_millis(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(IST).replace(microsecond=0)

def to_millis(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000) 

def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str
) -> int:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    punch = punch_in.astimezone(IST)

    shift_start_dt = punch.replace(
        hour=start_h,
        minute=start_m,
        second=0,
        microsecond=0
    )

    if (end_h, end_m) <= (start_h, start_m):
        if (punch.hour, punch.minute) < (end_h, end_m):
            shift_start_dt -= timedelta(days=1)

    elapsed_seconds = (punch - shift_start_dt).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return max(0, int(elapsed_seconds // 60))

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


def validate_paging(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = now_ist()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    validate_paging(page, page_size)

    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}



def serialize_attendance(doc):
    history = []

    for item in doc.get("history", []):
        history_item = item.copy()

        if history_item.get("at") is not None:
            history_item["at"] = to_millis(history_item["at"])

        history.append(history_item)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_millis(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": to_millis(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, "Invalid punch-in status")

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        if type(body.punched_at) is not int or not (
            100000000000 <= body.punched_at <= 4102444800000
        ):
            raise HTTPException(422, "punched_at must be epoch milliseconds")
        ts = from_millis(body.punched_at)
    else:
        ts = now_ist()

    ts = ts.replace(microsecond=0)
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    punch_time = ts.strftime("%H:%M")
    overnight = shift_end <= shift_start

    attendance_date = ts.date()
    if overnight and punch_time < shift_end:
        attendance_date -= timedelta(days=1)

    date_str = attendance_date.isoformat()
    late = compute_late_minutes(ts, shift_start, shift_end)
    print("DEBUG:", ts, ts.tzinfo, shift_start, shift_end)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": to_utc_naive(ts),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except Exception as exc:
        from pymongo.errors import DuplicateKeyError
        if isinstance(exc, DuplicateKeyError):
            raise HTTPException(409, "Already punched in for this date")
        raise

    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    from pymongo.errors import DuplicateKeyError

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        if type(body.punched_at) is not int or not (
            100000000000 <= body.punched_at <= 4102444800000
        ):
            raise HTTPException(422, "punched_at must be epoch milliseconds")
        ts = from_millis(body.punched_at)
    else:
        ts = now_ist()

    # Find the latest attendance record with a punch-in
    punch_in_utc = to_utc_naive(ts)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_in_utc},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(404, "No punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    start = record["punch_in"].replace(tzinfo=timezone.utc)
    end = ts.astimezone(timezone.utc)

    elapsed = (end - start).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "Invalid punch-out time")

    
    work_hours = compute_work_hours(start, end)

    shift_start_h, shift_start_m = map(
        int, emp.get("shift_start", "09:30").split(":")
    )
    shift_end_h, shift_end_m = map(
        int, emp.get("shift_end", "18:30").split(":")
    )

    start_ist = start.astimezone(IST)
    end_ist = end.astimezone(IST)

    shift_end_dt = start_ist.replace(
        hour=shift_end_h,
        minute=shift_end_m,
        second=0,
        microsecond=0,
    )

    if (shift_end_h, shift_end_m) <= (shift_start_h, shift_start_m):
        if (start_ist.hour, start_ist.minute) >= (shift_start_h, shift_start_m):
            shift_end_dt += timedelta(days=1)

    overtime = 0
    if end_ist > shift_end_dt:
        overtime = int((end_ist - shift_end_dt).total_seconds() // 60)
        if overtime < 30:
            overtime = 0

    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": to_utc_naive(end),
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record was updated concurrently")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated) 


from typing import Optional

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    validate_paging(page, page_size)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    if status:
        if status not in ALLOWED_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status")
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    records = db.attendance_logs.find(
        query, {"_id": 0}
    ).sort(
        [("date", -1), ("emp_code", 1)]
    ).skip(
        (page - 1) * page_size
    ).limit(page_size)

    items = [serialize_attendance(record) for record in records]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

class AttendancePatchIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(
    emp_code: str,
    date: str,
    data: AttendancePatchIn
):
    if not (5 <= len(data.reason) <= 200):
        raise HTTPException(
            status_code=422,
            detail="Reason must be 5 to 200 characters"
        )

    if not (1 <= len(data.regularized_by) <= 50):
        raise HTTPException(
            status_code=422,
            detail="regularized_by must be 1 to 50 characters"
        )

    if data.status is not None and data.status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })

    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    updates = {}

    if data.status is not None and data.status != record.get("status"):
        updates["status"] = data.status

    
    if data.status in ("ABSENT", "LEAVE"):
        if data.punch_in is not None or data.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot provide punch times for ABSENT or LEAVE"
            )
        if record.get("punch_in") is not None:
            updates["punch_in"] = None
        if record.get("punch_out") is not None:
            updates["punch_out"] = None
        if record.get("work_hours") is not None:
            updates["work_hours"] = None
        if record.get("late_minutes", 0) != 0:
            updates["late_minutes"] = 0
        if record.get("overtime_minutes", 0) != 0:
            updates["overtime_minutes"] = 0
        if record.get("half_day", False):
            updates["half_day"] = False

    if data.punch_in is not None:
        new_punch_in = from_millis(data.punch_in)
        if (
            record.get("punch_in") is None
            or to_millis(record.get("punch_in")) != data.punch_in
        ):
            updates["punch_in"] = to_utc_naive(new_punch_in)

    if data.punch_out is not None:
        new_punch_out = from_millis(data.punch_out)
        if (
            record.get("punch_out") is None
            or to_millis(record.get("punch_out")) != data.punch_out
        ):
            updates["punch_out"] = to_utc_naive(new_punch_out)


    # Recalculate attendance values when punch times are updated
    current_punch_in = updates.get("punch_in", record.get("punch_in"))
    current_punch_out = updates.get("punch_out", record.get("punch_out"))

    current_status = updates.get("status", record.get("status"))

    if current_status in ("PRESENT", "WFH", "ON_DUTY"):
        if current_punch_in is not None and current_punch_out is not None:
            duration_seconds = (
                current_punch_out - current_punch_in
            ).total_seconds()

            if duration_seconds <= 0 or duration_seconds > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid punch-out time"
                )

            updates["work_hours"] = float(
                (Decimal(str(duration_seconds / 3600))).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
            )

            updates["half_day"] = updates["work_hours"] < 4.50


    
    history_item = {
    "at": to_utc_naive(now_ist()),
    "reason": data.reason,
    "regularized_by": data.regularized_by,
    "changes": {
        key: {
            "old": (
                to_millis(record.get(key))
                if key in ("punch_in", "punch_out")
                and record.get(key) is not None
                else record.get(key)
            ),
            "new": (
                to_millis(value)
                if key in ("punch_in", "punch_out")
                and value is not None
                else value
            )
        }
        for key, value in updates.items()
    }
}


    updates["history"] = record.get("history", []) + [history_item]

    db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": updates}
    )

    updated_record = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated_record) 

from calendar import monthrange 

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str):
    import re
    from datetime import date

    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format"
        )

    year, month_num = map(int, month.split("-"))

    if month_num < 1 or month_num > 12:
        raise HTTPException(status_code=422, detail="Invalid month")

    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_start = date(year, month_num, 1)
    month_end = date(year, month_num, monthrange(year, month_num)[1])

    joined_on = employee.get("joined_on")
    if isinstance(joined_on, datetime):
        joined_on = joined_on.date()
    elif isinstance(joined_on, str):
        joined_on = date.fromisoformat(joined_on[:10])

    effective_start = max(month_start, joined_on) if joined_on else month_start

    working_days = sum(
        1 for day in range(1, monthrange(year, month_num)[1] + 1)
        if date(year, month_num, day) >= effective_start
        and date(year, month_num, day).weekday() < 5
    )

    records = list(db.attendance_logs.find({
        "emp_code": emp_code,
        "date": {
            "$gte": month_start.isoformat(),
            "$lte": month_end.isoformat()
        }
    }))

    presence_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for record in records:
        record_date = date.fromisoformat(record["date"])
        is_weekday = record_date.weekday() < 5

        if record["status"] == "LEAVE":
            leave_days += 1

        if record["status"] in presence_statuses and is_weekday:
            present_days += 0.5 if record.get("half_day", False) else 1.0

        late = record.get("late_minutes", 0)
        if late > 0:
            late_count += 1
            total_late_minutes += late

        total_overtime_minutes += record.get("overtime_minutes", 0)

    attendance_pct = (
        float((Decimal(str(present_days * 100 / working_days))).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        ))
        if working_days else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    import re
    from calendar import monthrange
    from datetime import date

    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format"
        )

    year, month_num = map(int, month.split("-"))

    if not 1 <= month_num <= 12:
        raise HTTPException(status_code=422, detail="Invalid month")

    month_start = date(year, month_num, 1)
    month_end = date(year, month_num, monthrange(year, month_num)[1])

    employee_filter = {}
    if department:
        employee_filter["department"] = department

    employees = list(db.employees.find(employee_filter, {"_id": 0}))

    summary = {}

    for employee in employees:
        joined_on = employee.get("joined_on")

        if isinstance(joined_on, datetime):
            joined_on = joined_on.date()
        elif isinstance(joined_on, str):
            try:
                joined_on = date.fromisoformat(joined_on[:10])
            except ValueError:
                joined_on = None

        if joined_on and joined_on > month_end:
            continue

    dept = employee.get("department", "Unknown")

    if dept not in summary:
        summary[dept] = {
            "department": dept,
            "headcount": 0,
            "present_days": 0.0,
            "work_hours_total": 0.0,
            "work_hours_count": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        }

        summary[dept]["headcount"] += 1

    for record in db.attendance_logs.find({
        "date": {
            "$gte": month_start.isoformat(),
            "$lte": month_end.isoformat()
        }
    }):
        employee = db.employees.find_one({"emp_code": record["emp_code"]})

        if not employee:
            continue

        dept = employee.get("department", "Unknown")

        if department and dept != department:
            continue

        if dept not in summary or summary[dept]["headcount"] == 0:
            continue

        record_date = date.fromisoformat(record["date"])
        is_weekday = record_date.weekday() < 5
        status = record.get("status")

        if status in ("PRESENT", "WFH", "ON_DUTY") and is_weekday:
            summary[dept]["present_days"] += (
                0.5 if record.get("half_day", False) else 1.0
            )

        if status in ("PRESENT", "WFH", "ON_DUTY"):
            hours = record.get("work_hours")
            if hours is not None:
                summary[dept]["work_hours_total"] += hours
                summary[dept]["work_hours_count"] += 1

        if record.get("late_minutes", 0) > 0:
            summary[dept]["late_count"] += 1

        summary[dept]["total_late_minutes"] += record.get("late_minutes", 0)

        if status == "LEAVE":
            summary[dept]["leave_count"] += 1

        if status == "ON_DUTY":
            summary[dept]["on_duty_count"] += 1

    result = []

    for dept in sorted(summary):
        item = summary[dept]
        count = item.pop("work_hours_count")
        total = item.pop("work_hours_total")

        item["avg_work_hours"] = (
            float(
                Decimal(str(total / count)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
            if count else None
        )

        item["present_days"] = float(
            Decimal(str(item["present_days"])).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )

        result.append(item)

    return {"month": month, "departments": result}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None
):
    import re
    from calendar import monthrange
    from datetime import date

    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format"
        )

    year, month_num = map(int, month.split("-"))

    if not 1 <= month_num <= 12:
        raise HTTPException(status_code=422, detail="Invalid month")

    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 100"
        )

    last_day = monthrange(year, month_num)[1]
    month_start = date(year, month_num, 1).isoformat()
    month_end = date(year, month_num, last_day).isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "rank": 1
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "limit": limit,
        "leaderboard": results
    }

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    from datetime import date, datetime, timedelta

    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format"
        )

    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="to date must be on or after from date"
        )

    if (end_date - start_date).days > 91:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days"
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(
        end_date + timedelta(days=1), datetime.min.time()
    )

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": from_date,
                    "$lte": to_date
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$set": {
                "day": {
                    "$dateFromString": {
                        "dateString": "$date"
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$day",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$status",
                                    ["PRESENT", "WFH", "ON_DUTY"]
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$unionWith": {
                "pipeline": [
                    {
                        "$documents": [
                            {
                                "_id": start_dt,
                                "present_count": 0,
                                "late_count": 0
                            }
                        ]
                    }
                ]
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"}
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt]
                }
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_day": "$_id"},
                "pipeline": [
                    {
                        "$set": {
                            "join_date": {
                                "$convert": {
                                    "input": "$joined_on",
                                    "to": "date",
                                    "onError": None,
                                    "onNull": None
                                }
                            }
                        }
                    },
                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$or": [
                                    {"$eq": ["$join_date", None]},
                                    {"$lte": ["$join_date", "$$trend_day"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "joined_employees"
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$joined_employees"},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]}
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$multiply": [
                                        {
                                            "$divide": [
                                                "$present_count",
                                                "$headcount"
                                            ]
                                        },
                                        100
                                    ]
                                },
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "range": [-6, 0],
                            "unit": "day"
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id"
                    }
                },
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$round": ["$moving_avg_7d", 4]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "trend": rows
    }


from fastapi import Path
from pymongo.errors import PyMongoError


EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    month: str = "2026-10",
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    from_date: str = "2026-10-01",
    to_date: str = "2026-10-31",
    page: int = 1,
    page_size: int = 20,
    limit: int = 10,
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Unsupported endpoint",
                "allowed": sorted(EXPLAIN_ENDPOINTS),
            },
        )

    validate_paging(page, page_size)

    if not (1 <= limit <= 100):
        raise HTTPException(422, "limit must be between 1 and 100")

    try:
        month_start = date.fromisoformat(month + "-01")
        next_month = (
            date(month_start.year + (month_start.month == 12),
                 1 if month_start.month == 12 else month_start.month + 1,
                 1)
        )
        start = month_start.isoformat()
        end = (next_month - timedelta(days=1)).isoformat()

        if endpoint == "attendance_list":
            query = {}
            if emp_code:
                query["emp_code"] = emp_code
            query["date"] = {"$gte": start, "$lte": end}

            command = {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            }
            collection = "attendance_logs"

        elif endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required")
            command = {
                "find": "attendance_logs",
                "filter": {
                    "emp_code": emp_code,
                    "date": {"$gte": start, "$lte": end},
                },
            }
            collection = "attendance_logs"

        elif endpoint == "department_summary":
            pipeline = [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
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
            if department:
                pipeline.append(
                    {"$match": {"employee.department": department}}
                )

            command = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection = "attendance_logs"

        elif endpoint == "late_leaderboard":
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": start, "$lte": end},
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                    }
                },
                {
                    "$sort": {
                        "total_late_minutes": -1,
                        "_id": 1,
                    }
                },
                {"$limit": limit},
            ]
            command = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection = "attendance_logs"

        else:  # department_trend
            start_date = date.fromisoformat(from_date)
            end_date = date.fromisoformat(to_date)
            if end_date < start_date:
                raise HTTPException(422, "'to' must be on or after 'from'")

            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": start_date.isoformat(),
                            "$lte": end_date.isoformat(),
                        }
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
            ]
            command = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection = "attendance_logs"

        explain = db.command({
            "explain": command,
            "verbosity": "executionStats",
        })

        stats = explain.get("executionStats", {})
        planner = explain.get("queryPlanner", {})
        winning_plan = planner.get("winningPlan", {})

        return {
            "endpoint": endpoint,
            "collection": collection,
            "executionStats": {
                "nReturned": stats.get("nReturned"),
                "executionTimeMillis": stats.get("executionTimeMillis"),
                "totalKeysExamined": stats.get("totalKeysExamined"),
                "totalDocsExamined": stats.get("totalDocsExamined"),
            },
            "winningPlan": winning_plan,
            "queryPlanner": planner,
        }

    except HTTPException:
        raise
    except (PyMongoError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not explain query: {exc}",
        )
