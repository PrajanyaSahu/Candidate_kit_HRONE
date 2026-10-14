"""Offline tests (no MongoDB server needed): business rules R1-R5 and the CRUD endpoints, run against
mongomock. The aggregation analytics need a real server: see tests/live_smoke.py.
    pip install pytest httpx mongomock && pytest tests
"""
import threading
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as m

UTC = timezone.utc


def ms(y, mo, d, h, mi, s=0, ms_=0):
    """IST wall-clock -> epoch ms."""
    dt = datetime(y, mo, d, h, mi, s, ms_ * 1000, tzinfo=m.IST)
    return m.dt_to_ms(dt)


@pytest.fixture()
def client(monkeypatch):
    mock = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(m, "client", mock)
    monkeypatch.setattr(m, "db", mock["t"])
    m.ensure_indexes()
    return TestClient(m.app)


def mk_emp(c, code="EMP0001", dept="Eng", start="09:30", end="18:30", joined="2026-01-05"):
    r = c.post("/employees", json=dict(emp_code=code, name="A", email="a@b.co", department=dept,
                                       shift_start=start, shift_end=end, joined_on=joined))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- pure rule helpers ----------------
def dt(h, mi, s=0, d=6):
    return datetime(2026, 7, d, h, mi, s, tzinfo=m.IST)


def test_r2_late_boundaries():
    f = lambda h, mi, s: m.compute_late_minutes(dt(h, mi, s), "09:30", "2026-07-06")
    assert f(9, 40, 0) == 0      # exactly 10:00 -> not late
    assert f(9, 40, 1) == 10     # 10:01 -> late, minutes since shift start
    assert f(10, 15, 59) == 45
    assert f(9, 0, 0) == 0       # early


def test_r3_overtime_min_30():
    f = lambda h, mi, s=0: m.compute_overtime(dt(h, mi, s), "09:30", "18:30", "2026-07-06")
    assert f(18, 59, 59) == 0    # 29 min
    assert f(19, 0) == 30
    assert f(19, 10) == 40


def test_r3_overnight_end_is_next_day():
    out = dt(6, 40, d=7)
    assert m.compute_overtime(out, "22:00", "06:00", "2026-07-06") == 40


def test_r4_half_up():
    pin = datetime(2026, 7, 6, 9, 0, tzinfo=m.IST)
    from datetime import timedelta
    # 29250 s = 8.125 h -> half-up 8.13 (float round() gives 8.12)
    assert m.compute_work_hours(pin, pin + timedelta(seconds=29250)) == 8.13
    assert m.compute_work_hours(pin, pin + timedelta(seconds=12600)) == 3.5


def test_r5_half_day_uses_rounded_hours():
    assert m.compute_half_day(4.49) is True
    assert m.compute_half_day(4.5) is False


def test_r1_attendance_date():
    # overnight 22:00-06:00: 03:00 IST on the 7th belongs to the 6th; 21:55 on the 6th is the 6th
    assert m.attendance_date(dt(3, 0, d=7), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(dt(21, 55), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(dt(6, 0, d=7), "22:00", "06:00") == "2026-07-07"  # not earlier than shift_end
    # day shift: IST date, even when the UTC date differs (00:30 IST = 19:00 UTC the day before)
    assert m.attendance_date(dt(0, 30), "09:30", "18:30") == "2026-07-06"


# ---------------- employees ----------------
def test_employee_create_validation_and_conflict(client):
    e = mk_emp(client)
    assert isinstance(e["created_at"], int) and "_id" not in e
    assert client.post("/employees", json=dict(emp_code="EMP0001", name="A", email="a@b.co", department="Eng", joined_on="2026-01-05")).status_code == 409
    bad = dict(emp_code="EMP0002", name="A", email="a@b.co", department="Eng", joined_on="2026-01-05")
    for patch in (dict(emp_code="X1"), dict(email="nope"), dict(joined_on="2026-02-30"), dict(shift_start="10:00", shift_end="10:00"), dict(shift_start="25:00")):
        assert client.post("/employees", json={**bad, **patch}).status_code == 422, patch


def test_employee_list_pagination_filter_sort(client):
    for i in range(1, 6):
        mk_emp(client, f"EMP000{i}", dept="A" if i % 2 else "B")
    r = client.get("/employees", params=dict(page=1, page_size=2)).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 5
    r = client.get("/employees", params=dict(department="A", page=2, page_size=2)).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0005"] and r["total"] == 3
    for bad in (dict(page=0), dict(page_size=0), dict(page_size=101)):
        assert client.get("/employees", params=bad).status_code == 422


# ---------------- punch in / out ----------------
def test_punch_flow_and_rules(client):
    mk_emp(client)
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 10, 5, 0, 900)))
    assert r.status_code == 201
    b = r.json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 35 and b["punch_in"] == ms(2026, 7, 6, 10, 5)  # ms truncated
    assert b["work_hours"] is None and b["history"] == [] and "id" not in b
    assert client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 11, 0))).status_code == 409
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 19, 10)))
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["overtime_minutes"], b["half_day"], b["late_minutes"]) == (9.08, 40, False, 35)
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 19, 20))).status_code == 409


def test_punch_in_errors(client):
    mk_emp(client)
    assert client.post("/attendance/punch-in", json=dict(emp_code="EMP9999")).status_code == 404
    for pa in (1783312500, 1783312500000.5, "1783312500000", True, 5):  # seconds, float, string, bool, tiny
        r = client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=pa))
        assert r.status_code == 422, pa
    for st in ("ABSENT", "LEAVE", "x"):
        assert client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", status=st, punched_at=ms(2026, 7, 6, 9, 0))).status_code == 422


def test_overnight_across_midnight(client):
    mk_emp(client, "EMP0005", "Support", "22:00", "06:00", "2026-03-10")
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP0005", punched_at=ms(2026, 7, 6, 21, 55)))
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP0005", punched_at=ms(2026, 7, 7, 6, 40)))
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["overtime_minutes"]) == (8.75, 40)


def test_punch_out_errors(client):
    mk_emp(client)
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP9999")).status_code == 404
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 9, 0))).status_code == 404
    client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 9, 0)))
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 9, 0))).status_code == 422  # equal
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 7, 9, 0, 1))).status_code == 422  # > 24 h
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 12, 30))).json()["half_day"] is True


def test_race_punch_in_and_out(client):
    mk_emp(client)
    codes = []
    def go(): codes.append(client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 9, 0))).status_code)
    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(codes) == [201] + [409] * 11
    codes.clear()
    def out(): codes.append(client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 18, 0))).status_code)
    ts = [threading.Thread(target=out) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(codes) == [200] + [409] * 11


# ---------------- list ----------------
def test_list_attendance(client):
    for code in ("EMP0001", "EMP0002"):
        mk_emp(client, code)
    for d in (6, 7, 8):
        for code in ("EMP0002", "EMP0001"):
            client.post("/attendance/punch-in", json=dict(emp_code=code, punched_at=ms(2026, 7, d, 9, 0), status="WFH" if d == 7 else "PRESENT"))
    r = client.get("/attendance", params=dict(page_size=4)).json()
    assert r["total"] == 6
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    r = client.get("/attendance", params=dict(emp_code="EMP0001", date_from="2026-07-07", date_to="2026-07-08")).json()
    assert r["total"] == 2 and [i["date"] for i in r["items"]] == ["2026-07-08", "2026-07-07"]
    assert client.get("/attendance", params=dict(status="WFH")).json()["total"] == 2
    assert client.get("/attendance", params=dict(date_from="2026-07-09", date_to="2026-07-01")).status_code == 422
    assert client.get("/attendance", params=dict(date_from="2026-7-1")).status_code == 422
    assert client.get("/attendance", params=dict(status="NOPE")).status_code == 422


def test_legacy_document_defaults(client):
    m.db.attendance_logs.insert_one(dict(emp_code="EMP0002", date="2026-07-14", status="PRESENT",
        punch_in=datetime(2026, 7, 14, 4, 2, tzinfo=UTC), punch_out=datetime(2026, 7, 14, 13, 1, tzinfo=UTC), work_hours=8.98, late_minutes=0, overtime_minutes=0))
    it = client.get("/attendance").json()["items"][0]
    assert it["half_day"] is False and it["history"] == [] and isinstance(it["punch_in"], int)


# ---------------- regularize ----------------
def seed_day(c):
    mk_emp(c)
    c.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 7, 10, 5)))
    c.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms(2026, 7, 7, 18, 30)))


def test_regularize_recompute_and_history(client):
    seed_day(client)
    body = dict(punch_in=ms(2026, 7, 7, 9, 28), reason="biometric glitch", regularized_by="hr", work_hours=1, late_minutes=99)
    r = client.patch("/attendance/EMP0001/2026-07-07", json=body)
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["late_minutes"], b["work_hours"]) == (0, 9.03)
    h = b["history"]
    assert len(h) == 1 and h[0]["by"] == "hr" and isinstance(h[0]["at"], int)
    assert set(h[0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}  # only what changed
    assert h[0]["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    # second correction appends; first entry is untouched
    r = client.patch("/attendance/EMP0001/2026-07-07", json=dict(status="WFH", reason="was remote", regularized_by="hr2"))
    assert len(r.json()["history"]) == 2 and r.json()["history"][0] == h[0]


def test_regularize_absent_and_rules(client):
    seed_day(client)
    P = "/attendance/EMP0001/2026-07-07"
    ok = dict(reason="because", regularized_by="hr")
    assert client.patch(P, json={**ok}).status_code == 422                                            # changes nothing
    assert client.patch(P, json={**ok, "punch_in": ms(2026, 7, 7, 10, 5)}).status_code == 422          # same value
    assert client.patch(P, json={**ok, "status": "LEAVE", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert client.patch(P, json={**ok, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422          # other attendance date
    assert client.patch(P, json={**ok, "punch_out": ms(2026, 7, 7, 10, 0)}).status_code == 422         # before punch_in
    assert client.patch(P, json={**ok, "punch_out": ms(2026, 7, 8, 11, 0)}).status_code == 422         # > 24 h
    assert client.patch(P, json={"reason": "x", "regularized_by": "hr", "status": "LEAVE"}).status_code == 422
    assert client.patch(P, json={**ok, "punch_in": None}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-13-01", json={**ok, "status": "LEAVE"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-09", json={**ok, "status": "LEAVE"}).status_code == 404
    assert client.patch("/attendance/EMP0404/2026-07-07", json={**ok, "status": "LEAVE"}).status_code == 404
    r = client.patch(P, json={**ok, "status": "ABSENT"})
    b = r.json()
    assert r.status_code == 200 and (b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"], b["half_day"]) == (None, None, None, 0, False)
    # back to a presence status needs a punch_in
    assert client.patch(P, json={**ok, "status": "PRESENT"}).status_code == 422
    assert client.patch(P, json={**ok, "status": "PRESENT", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 200


def test_regularize_concurrent_no_lost_history(client):
    seed_day(client)
    codes = []
    def go(i):
        codes.append(client.patch("/attendance/EMP0001/2026-07-07", json=dict(punch_in=ms(2026, 7, 7, 9, i), reason="concurrent", regularized_by=f"u{i}")).status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(1, 9)]
    [t.start() for t in ts]; [t.join() for t in ts]
    n_ok = codes.count(200)
    assert n_ok >= 1 and set(codes) <= {200, 409}
    assert len(client.get("/attendance", params=dict(emp_code="EMP0001")).json()["items"][0]["history"]) == n_ok  # every 200 left exactly one entry


def test_health_and_503(client, monkeypatch):
    assert client.get("/health").json() == {"status": "ok"}
    from pymongo.errors import PyMongoError

    def fail(*args, **kwargs):
        raise PyMongoError("down")

    monkeypatch.setattr(m.client.admin, "command", fail)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json() == {"detail": "database unavailable"}


def test_r7_working_days():
    from datetime import date
    # July 2026: 31 days, starts Wednesday July 1, ends Friday July 31 -> 23 working days
    assert m.working_days(date(2026, 7, 1), date(2026, 7, 31)) == 23
    # Mid-month joiner: joined July 15 (Wed) -> 13 working days
    assert m.working_days(date(2026, 7, 15), date(2026, 7, 31)) == 13
    # Joined next month: joined Aug 1 -> 0 working days
    assert m.working_days(date(2026, 8, 1), date(2026, 7, 31)) == 0


def test_explain_validations(client):
    assert client.get("/admin/explain/invalid_ep").status_code == 422
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/department_summary").status_code == 422
    assert client.get("/admin/explain/late_leaderboard").status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Eng", "from": "2026-07-01"}).status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Eng", "from": "2026-07-10", "to": "2026-07-01"}).status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Eng", "from": "2026-01-01", "to": "2026-04-10"}).status_code == 422

