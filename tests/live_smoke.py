"""LIVE test for the three aggregation endpoints. Needs a real MongoDB 6.0+ (the pipelines use $densify,
$setWindowFields, $lookup with a sub-pipeline). It seeds a SCRATCH database with randomised awkward data
(ties, weekend records, employees with no logs, a mid-month joiner, missing weekdays, an unknown-employee log),
computes the expected answers with plain Python + Decimal (independent of the pipelines), and compares.

    MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_scratch python tests/live_smoke.py
Also checks that /admin/explain/* shows IXSCAN and no COLLSCAN on a larger dataset.
"""
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

os.environ.setdefault("MONGO_DB", "attendance_scratch")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

UTC = timezone.utc
PRES = {"PRESENT", "WFH", "ON_DUTY"}
D = Decimal


def hu(x, places):
    return float(D(x).quantize(D(1).scaleb(-places), rounding=ROUND_HALF_UP))


def weekday(d):
    return d.weekday() < 5


def build(seed=7, n_emp=40):
    rnd = random.Random(seed)
    depts = ["Eng", "Sales", "Support", "Empty"]
    emps, logs = [], []
    for i in range(1, n_emp + 1):
        dept = depts[i % 3]
        joined = date(2026, 1, 5)
        if i % 9 == 0:
            joined = date(2026, 7, rnd.choice([8, 15, 22]))      # mid-month joiner
        if i % 13 == 0:
            joined = date(2026, 8, 3)                            # joins after the month
        emps.append(dict(emp_code=f"EMP{i:04d}", name=f"N{i}", email=f"n{i}@x.io", department=dept, shift_start="09:30",
                         shift_end="18:30", joined_on=joined.isoformat(), created_at=datetime(2026, 1, 1, tzinfo=UTC)))
        if i % 11 == 0:
            continue                                             # employee with no logs at all
        for dd in range(1, 32):
            day = date(2026, 7, dd)
            if rnd.random() < 0.15:
                continue                                         # missing day (not ABSENT)
            if not weekday(day) and rnd.random() < 0.7:
                continue                                         # some weekend records survive
            st = rnd.choice(["PRESENT"] * 6 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])
            doc = dict(emp_code=f"EMP{i:04d}", date=day.isoformat(), status=st, punch_in=None, punch_out=None,
                       work_hours=None, late_minutes=0, overtime_minutes=0)
            if st in PRES:
                wh = round(rnd.uniform(2, 11), 2)
                base = datetime(2026, 7, dd, 4, 0, tzinfo=UTC)
                doc.update(punch_in=base, punch_out=base + timedelta(hours=wh), work_hours=wh if rnd.random() < 0.9 else None,
                           late_minutes=rnd.choice([0, 0, 0, 11, 20, 20, 45]), overtime_minutes=rnd.choice([0, 0, 30, 45]))
                if rnd.random() < 0.15:
                    doc.pop("half_day", None)                    # legacy: missing half_day
                else:
                    doc["half_day"] = bool(wh < 4.5)
                if doc["work_hours"] is None:
                    doc["punch_out"] = None
            logs.append(doc)
    logs.append(dict(emp_code="EMP9999", date="2026-07-06", status="PRESENT", punch_in=datetime(2026, 7, 6, 5, tzinfo=UTC),
                     punch_out=None, work_hours=None, late_minutes=999, overtime_minutes=0, half_day=False))  # unknown employee
    # force a tie at the top-3 boundary inside one department
    return emps, logs


def day_val(l):
    return D("0.5") if l.get("half_day") else D(1)


def ref_monthly(e, logs, y=2026, mo=7):
    first, last = date(y, mo, 1), date(y, mo, 31)
    start = max(first, date.fromisoformat(e["joined_on"]))
    wd = sum(1 for k in range((last - start).days + 1) if weekday(start + timedelta(days=k)))
    mine = [l for l in logs if l["emp_code"] == e["emp_code"]]
    present = sum((day_val(l) for l in mine if l["status"] in PRES and weekday(date.fromisoformat(l["date"]))), D(0))
    return dict(emp_code=e["emp_code"], month="2026-07", working_days=wd, present_days=hu(present, 2),
                leave_days=sum(l["status"] == "LEAVE" for l in mine), late_count=sum(l["late_minutes"] > 0 for l in mine),
                total_late_minutes=sum(l["late_minutes"] for l in mine), total_overtime_minutes=sum(l["overtime_minutes"] for l in mine),
                attendance_pct=None if wd == 0 else hu(present * 100 / D(wd), 2))


def ref_summary(emps, logs):
    out = {}
    byemp = {e["emp_code"]: e for e in emps}
    for e in emps:
        if e["joined_on"] <= "2026-07-31":
            out.setdefault(e["department"], dict(h=0, p=D(0), hs=D(0), hn=0, lc=0, lt=0, lv=0, od=0))["h"] += 1
    for l in logs:
        e = byemp.get(l["emp_code"])
        if not e or e["joined_on"] > "2026-07-31":
            continue
        o = out[e["department"]]
        if l["status"] in PRES and weekday(date.fromisoformat(l["date"])):
            o["p"] += day_val(l)
        if l["status"] in PRES and l["work_hours"] is not None:
            o["hs"] += D(repr(l["work_hours"])); o["hn"] += 1
        if l["late_minutes"] > 0:
            o["lc"] += 1; o["lt"] += l["late_minutes"]
        o["lv"] += l["status"] == "LEAVE"; o["od"] += l["status"] == "ON_DUTY"
    return [dict(department=d, headcount=o["h"], present_days=hu(o["p"], 2), avg_work_hours=hu(o["hs"] / o["hn"], 2) if o["hn"] else None,
                 late_count=o["lc"], total_late_minutes=o["lt"], leave_count=o["lv"], on_duty_count=o["od"]) for d, o in sorted(out.items())]


def ref_leaderboard(emps, logs, limit, dept=None):
    byemp = {e["emp_code"]: e for e in emps}
    tot = {}
    for l in logs:
        e = byemp.get(l["emp_code"])
        if e and l["late_minutes"] > 0 and (dept is None or e["department"] == dept):
            t = tot.setdefault(l["emp_code"], [0, 0]); t[0] += l["late_minutes"]; t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
    res = []
    for code, (t, c) in rows:
        rank = 1 + sum(1 for _, (t2, _) in rows if t2 > t)          # standard competition rank
        if rank <= limit:
            res.append(dict(rank=rank, emp_code=code, name=byemp[code]["name"], department=byemp[code]["department"], total_late_minutes=t, late_count=c))
    return res


def ref_trend(emps, logs, dept, d_from, d_to):
    mine = [e for e in emps if e["department"] == dept]
    codes = {e["emp_code"] for e in mine}
    rows, rates = [], []
    d = d_from
    while d <= d_to:
        ds = d.isoformat()
        hc = sum(e["joined_on"] <= ds for e in mine)
        todays = [l for l in logs if l["emp_code"] in codes and l["date"] == ds]
        present = sum((day_val(l) for l in todays if l["status"] in PRES), D(0))
        rate = None
        if weekday(d) and hc > 0:
            rate = D(repr(hu(present / D(hc), 4)))
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append(dict(date=ds, is_working_day=weekday(d), headcount=hc, present_count=hu(present, 2),
                         late_count=sum(l["late_minutes"] > 0 for l in todays),
                         attendance_rate=None if rate is None else float(rate),
                         moving_avg_7d=hu(sum(window, D(0)) / D(len(window)), 4) if window else None))
        d += timedelta(days=1)
    return rows


def check(name, got, exp):
    if got != exp:
        print(f"FAIL {name}\n  got: {got}\n  exp: {exp}")
        return False
    print(f"ok   {name}")
    return True


def main():
    emps, logs = build()
    m.client.drop_database(m.db.name)
    m.db.employees.insert_many([dict(e) for e in emps])
    m.db.attendance_logs.insert_many([dict(l) for l in logs])
    m.ensure_indexes()
    c = TestClient(m.app)
    ok = True
    for e in emps[:12] + [e for e in emps if e["joined_on"] > "2026-07-31"][:1]:
        r = c.get(f"/analytics/employees/{e['emp_code']}/monthly", params=dict(month="2026-07"))
        ok &= check(f"monthly {e['emp_code']}", r.json(), ref_monthly(e, logs))
    ok &= check("dept summary", c.get("/analytics/departments/summary", params=dict(month="2026-07")).json()["items"], ref_summary(emps, logs))
    ok &= check("dept summary 'Empty' omitted", [i["department"] for i in c.get("/analytics/departments/summary", params=dict(month="2026-07")).json()["items"]], ["Eng", "Sales", "Support"])
    for limit, dept in ((3, None), (10, None), (2, "Eng"), (50, "Sales")):
        r = c.get("/analytics/leaderboard/late", params={k: v for k, v in dict(month="2026-07", limit=limit, department=dept).items() if v is not None})
        ok &= check(f"leaderboard limit={limit} dept={dept}", r.json()["items"], ref_leaderboard(emps, logs, limit, dept))
    for dept, a, b in (("Eng", date(2026, 6, 25), date(2026, 8, 10)), ("Support", date(2026, 7, 1), date(2026, 7, 31)), ("Empty", date(2026, 7, 1), date(2026, 7, 5))):
        r = c.get(f"/analytics/departments/{dept}/trend", params={"from": a.isoformat(), "to": b.isoformat()})
        if dept == "Empty":
            ok &= check("trend unknown/empty dept -> 404", r.status_code, 404)
            continue
        ok &= check(f"trend {dept} rows", len(r.json()["items"]), (b - a).days + 1)
        ok &= check(f"trend {dept}", r.json()["items"], ref_trend(emps, logs, dept, a, b))
    ok &= check("trend 93 days -> 422", c.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code, 422)
    ok &= check("trend 92 days ok", c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code, 200)

    # explain on a bigger dataset
    big = []
    rnd = random.Random(1)
    for i in range(1, 1001):
        for k in range(100):
            big.append(dict(emp_code=f"EMP{i + 1000:04d}", date=(date(2026, 1, 1) + timedelta(days=k)).isoformat(), status="PRESENT",
                            punch_in=datetime(2026, 1, 1, 4, tzinfo=UTC), punch_out=None, work_hours=None, late_minutes=rnd.choice([0, 15]), overtime_minutes=0, half_day=False))
    m.db.attendance_logs.insert_many(big)
    m.db.employees.insert_many([dict(emp_code=f"EMP{i + 1000:04d}", name="b", email="b@x.io", department="Eng", shift_start="09:30", shift_end="18:30", joined_on="2026-01-01", created_at=datetime.now(UTC)) for i in range(1, 1001)])
    for ep, params in (("attendance_list", {}), ("attendance_list", dict(status="WFH", date_from="2026-02-01")), ("attendance_list", dict(emp_code="EMP1001")),
                       ("employee_monthly", dict(emp_code="EMP1001", month="2026-02")), ("department_summary", dict(month="2026-02")),
                       ("late_leaderboard", dict(month="2026-02")), ("department_trend", {"department": "Eng", "from": "2026-02-01", "to": "2026-03-15"})):
        text = c.get(f"/admin/explain/{ep}", params=params).text
        good = "IXSCAN" in text and "COLLSCAN" not in text
        print(("ok   " if good else "FAIL ") + f"explain {ep} {params}")
        ok &= good
    print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
    m.client.drop_database(m.db.name)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
