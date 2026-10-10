"""Employees, punch-in/out, listing and regularization against a real MongoDB."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import main
from conftest import dt, make_emp, make_log, ms


def emp_body(code="EMP0001", **kw):
    body = {"emp_code": code, "name": "Asha Rao", "email": "asha@example.com", "department": "Engineering", "joined_on": "2026-01-05"}
    body.update(kw)
    return body


def punch_in(c, code, at, **kw):
    return c.post("/attendance/punch-in", json={"emp_code": code, "punched_at": at, **kw})


def punch_out(c, code, at):
    return c.post("/attendance/punch-out", json={"emp_code": code, "punched_at": at})


# ----------------------------- startup / health ---------------------------- #
def test_health_and_indexes(clean):
    assert clean.get("/health").json() == {"status": "ok"}
    names = {i["name"] for i in main.logs.list_indexes()} | {i["name"] for i in main.employees.list_indexes()}
    assert {"uniq_emp_date", "date_emp", "uniq_emp_code"} <= names
    main.ensure_indexes()  # idempotent


def test_health_503_when_mongo_down(monkeypatch, clean):
    class Down:
        def command(self, *a, **k):
            raise main.PyMongoError("down")
    monkeypatch.setattr(main.client, "admin", Down())
    r = clean.get("/health")
    assert r.status_code == 503 and r.json() == {"detail": "database unavailable"}


def test_health_ok_even_if_index_retry_fails(monkeypatch, clean):
    def boom():
        raise main.PyMongoError("index build failed")
    monkeypatch.setattr(main, "_indexes_ready", False)
    monkeypatch.setattr(main, "ensure_indexes", boom)
    assert clean.get("/health").json() == {"status": "ok"}  # readiness = MongoDB answers a ping


def test_database_error_is_503_not_500(monkeypatch, clean):
    def boom(*a, **k):
        raise main.PyMongoError("secret-host:27017 refused")
    monkeypatch.setattr(main.employees, "count_documents", boom)
    r = clean.get("/employees")
    assert r.status_code == 503 and "secret" not in r.text


# --------------------------------- employees ------------------------------- #
def test_create_employee(clean):
    r = clean.post("/employees", json=emp_body())
    assert r.status_code == 201
    body = r.json()
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30" and "_id" not in body
    assert isinstance(body["created_at"], int) and body["created_at"] > 1_000_000_000_000


def test_create_employee_duplicate_and_race(clean):
    assert clean.post("/employees", json=emp_body()).status_code == 201
    assert clean.post("/employees", json=emp_body()).status_code == 409
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: clean.post("/employees", json=emp_body("EMP0002")).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7


@pytest.mark.parametrize("patch", [
    {"emp_code": "E1"}, {"emp_code": "emp0001"}, {"emp_code": "EMP123"}, {"name": ""}, {"email": "nope"},
    {"shift_start": "9:30"}, {"shift_start": "25:00"}, {"shift_start": "10:00", "shift_end": "10:00"},
    {"joined_on": "2026-13-01"}, {"joined_on": "20260105"}, {"department": ""}])
def test_create_employee_invalid(clean, patch):
    assert clean.post("/employees", json=emp_body(**patch)).status_code == 422


def test_list_employees_pagination_filter_sort(clean):
    main.employees.insert_many([make_emp("EMP0003", "Sales"), make_emp("EMP0001"), make_emp("EMP0002", "Sales"), make_emp("EMP0004")])
    r = clean.get("/employees", params={"page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 4 and r["page"] == 1
    r = clean.get("/employees", params={"page_size": 2, "page": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"]
    r = clean.get("/employees", params={"department": "Sales"}).json()
    assert r["total"] == 2 and [e["emp_code"] for e in r["items"]] == ["EMP0002", "EMP0003"]
    assert clean.get("/employees", params={"department": "sales"}).json()["total"] == 0  # case-sensitive
    for bad in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}):
        assert clean.get("/employees", params=bad).status_code == 422


# --------------------------------- punch-in -------------------------------- #
@pytest.mark.parametrize("h,mi,s,ms_,late", [
    (9, 40, 0, 0, 0), (9, 40, 1, 0, 10), (10, 15, 59, 0, 45), (9, 40, 0, 900, 0), (9, 29, 0, 0, 0), (9, 30, 0, 0, 0), (11, 0, 0, 0, 90)])
def test_punch_in_late_rule_r2(clean, h, mi, s, ms_, late):
    main.employees.insert_one(make_emp("EMP0001"))
    r = punch_in(clean, "EMP0001", ms(2026, 7, 6, h, mi, s, ms_))
    assert r.status_code == 201
    b = r.json()
    assert b["late_minutes"] == late and b["date"] == "2026-07-06" and b["status"] == "PRESENT"
    assert b["punch_out"] is None and b["work_hours"] is None and b["overtime_minutes"] == 0
    assert b["half_day"] is False and b["history"] == [] and "id" not in b and "_id" not in b
    assert b["punch_in"] % 1000 == 0  # truncated to whole seconds


def test_punch_in_status_and_default_time(clean):
    main.employees.insert_one(make_emp("EMP0001"))
    r = clean.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH"})
    assert r.status_code == 201 and r.json()["status"] == "WFH" and r.json()["punch_in"] % 1000 == 0


@pytest.mark.parametrize("body", [
    {"punched_at": 1783312500}, {"punched_at": 1783312500000.5}, {"punched_at": "1783312500000"}, {"punched_at": 5_000_000_000_000},
    {"punched_at": True}, {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "bogus"}, {"emp_code": 5}])
def test_punch_in_invalid_body(clean, body):
    main.employees.insert_one(make_emp("EMP0001"))
    payload = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0), **body}
    assert clean.post("/attendance/punch-in", json=payload).status_code == 422


def test_punch_in_missing_emp_code(clean):
    assert clean.post("/attendance/punch-in", json={}).status_code == 422


def test_punch_in_unknown_employee_and_duplicate(clean):
    assert punch_in(clean, "EMP9999", ms(2026, 7, 6, 9, 0)).status_code == 404
    main.employees.insert_one(make_emp("EMP0001"))
    assert punch_in(clean, "EMP0001", ms(2026, 7, 6, 9, 0)).status_code == 201
    assert punch_in(clean, "EMP0001", ms(2026, 7, 6, 15, 0)).status_code == 409
    assert punch_in(clean, "EMP0001", ms(2026, 7, 7, 9, 0)).status_code == 201  # other date is fine
    assert main.logs.count_documents({"emp_code": "EMP0001"}) == 2


def test_punch_in_date_is_ist_not_utc(clean):
    main.employees.insert_one(make_emp("EMP0001"))
    # 00:30 IST on 07-07 is 19:00 UTC on 07-06; the attendance date is the IST one
    assert punch_in(clean, "EMP0001", ms(2026, 7, 7, 0, 30)).json()["date"] == "2026-07-07"


def test_punch_in_overnight_shift_date_r1(clean):
    main.employees.insert_one(make_emp("EMP0005", "Support", "22:00", "06:00"))
    b = punch_in(clean, "EMP0005", ms(2026, 7, 7, 3, 0)).json()  # before shift_end -> belongs to the shift of 07-06
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 300
    b = punch_in(clean, "EMP0005", ms(2026, 7, 7, 21, 55)).json()
    assert b["date"] == "2026-07-07" and b["late_minutes"] == 0
    main.employees.insert_one(make_emp("EMP0006", "Support", "22:00", "06:00"))
    assert punch_in(clean, "EMP0006", ms(2026, 7, 8, 5, 59, 59)).json()["date"] == "2026-07-07"
    assert punch_in(clean, "EMP0006", ms(2026, 7, 8, 6, 0, 0)).json()["date"] == "2026-07-08"  # 06:00 is not "earlier than" end


def test_punch_in_concurrent_exactly_one_201(clean):
    main.employees.insert_one(make_emp("EMP0001"))
    at = ms(2026, 7, 6, 9, 0)
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda _: punch_in(clean, "EMP0001", at).status_code, range(16)))
    assert sorted(codes) == [201] + [409] * 15
    assert main.logs.count_documents({"emp_code": "EMP0001"}) == 1


# --------------------------------- punch-out ------------------------------- #
def test_punch_out_valid_and_rules(clean):
    main.employees.insert_one(make_emp("EMP0003", "Sales"))
    punch_in(clean, "EMP0003", ms(2026, 7, 6, 10, 5))
    r = punch_out(clean, "EMP0003", ms(2026, 7, 6, 19, 10))
    assert r.status_code == 200
    b = r.json()
    assert b["work_hours"] == 9.08 and b["overtime_minutes"] == 40 and b["late_minutes"] == 35 and b["half_day"] is False
    assert b["punch_out"] == ms(2026, 7, 6, 19, 10) and b["history"] == []  # punching never writes history


@pytest.mark.parametrize("out_h,out_m,out_s,ot", [(18, 59, 59, 0), (19, 0, 0, 30), (18, 58, 0, 0), (19, 0, 59, 30), (19, 29, 59, 59)])
def test_overtime_threshold_r3(clean, out_h, out_m, out_s, ot):
    main.employees.insert_one(make_emp("EMP0001"))
    punch_in(clean, "EMP0001", ms(2026, 7, 6, 9, 30))
    assert punch_out(clean, "EMP0001", ms(2026, 7, 6, out_h, out_m, out_s)).json()["overtime_minutes"] == ot


@pytest.mark.parametrize("seconds,hours,half", [
    (54, 0.02, True),            # 0.015 -> half-up 0.02 (float round() gives 0.01)
    (18, 0.01, True), (16182, 4.5, False),   # 4.495h rounds up to 4.50 -> not a half day
    (16181, 4.49, True), (16200, 4.5, False), (32400, 9.0, False), (3600, 1.0, True)])
def test_work_hours_rounding_and_half_day_r4_r5(clean, seconds, hours, half):
    main.employees.insert_one(make_emp("EMP0001"))
    start = ms(2026, 7, 6, 9, 30)
    punch_in(clean, "EMP0001", start)
    b = punch_out(clean, "EMP0001", start + seconds * 1000).json()
    assert b["work_hours"] == hours and b["half_day"] is half


def test_punch_out_overnight_across_midnight(clean):
    main.employees.insert_one(make_emp("EMP0005", "Support", "22:00", "06:00"))
    punch_in(clean, "EMP0005", ms(2026, 7, 6, 21, 55))
    b = punch_out(clean, "EMP0005", ms(2026, 7, 7, 6, 40)).json()
    assert b["date"] == "2026-07-06" and b["work_hours"] == 8.75 and b["overtime_minutes"] == 40 and b["late_minutes"] == 0


def test_punch_out_errors(clean):
    assert punch_out(clean, "EMP9999", ms(2026, 7, 6, 18, 0)).status_code == 404
    main.employees.insert_one(make_emp("EMP0001"))
    assert punch_out(clean, "EMP0001", ms(2026, 7, 6, 18, 0)).status_code == 404  # no punch-in at all
    punch_in(clean, "EMP0001", ms(2026, 7, 6, 9, 30))
    assert punch_out(clean, "EMP0001", ms(2026, 7, 6, 9, 0)).status_code == 404  # no punch-in at or before that time
    assert punch_out(clean, "EMP0001", ms(2026, 7, 6, 9, 30)).status_code == 422  # not after punch_in
    assert punch_out(clean, "EMP0001", ms(2026, 7, 7, 9, 30, 1)).status_code == 422  # > 24h
    assert punch_out(clean, "EMP0001", 1783312500).status_code == 422  # seconds, not ms
    assert punch_out(clean, "EMP0001", ms(2026, 7, 7, 9, 30)).status_code == 200  # exactly 24h is allowed
    assert punch_out(clean, "EMP0001", ms(2026, 7, 7, 9, 30)).status_code == 409  # repeated


def test_punch_out_ignores_absent_records(clean):
    main.employees.insert_one(make_emp("EMP0001"))
    main.logs.insert_one(make_log("EMP0001", "2026-07-05", "ABSENT"))
    assert punch_out(clean, "EMP0001", ms(2026, 7, 6, 18, 0)).status_code == 404


def test_punch_out_concurrent_exactly_one_200(clean):
    main.employees.insert_one(make_emp("EMP0001"))
    punch_in(clean, "EMP0001", ms(2026, 7, 6, 9, 30))
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda i: punch_out(clean, "EMP0001", ms(2026, 7, 6, 18, 0, i)).status_code, range(16)))
    assert sorted(codes) == [200] + [409] * 15


# ------------------------------ list attendance ---------------------------- #
def test_list_attendance(clean):
    main.employees.insert_many([make_emp("EMP0001"), make_emp("EMP0002")])
    main.logs.insert_many([make_log("EMP0002", "2026-07-06", wh=8.0), make_log("EMP0001", "2026-07-06", wh=8.0),
                           make_log("EMP0001", "2026-07-07", "LEAVE"), make_log("EMP0001", "2026-07-05", "WFH", wh=3.0, half=True)])
    legacy = make_log("EMP0002", "2026-07-02", wh=8.0)
    del legacy["history"], legacy["half_day"]
    main.logs.insert_one(legacy)
    r = clean.get("/attendance").json()
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002"), ("2026-07-05", "EMP0001"), ("2026-07-02", "EMP0002")]
    assert r["total"] == 5 and r["page"] == 1 and r["page_size"] == 20
    assert r["items"][-1]["history"] == [] and r["items"][-1]["half_day"] is False  # legacy defaults
    assert all("id" not in i and "_id" not in i for i in r["items"])
    r = clean.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-07"}).json()
    assert r["total"] == 2
    r = clean.get("/attendance", params={"status": "WFH"}).json()
    assert r["total"] == 1 and r["items"][0]["half_day"] is True
    r = clean.get("/attendance", params={"page": 2, "page_size": 2}).json()
    assert r["total"] == 5 and len(r["items"]) == 2 and r["items"][0]["date"] == "2026-07-06" and r["items"][0]["emp_code"] == "EMP0002"
    assert clean.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"}).status_code == 422
    for bad in ({"date_from": "2026-7-1"}, {"date_to": "2026-02-30"}, {"status": "NOPE"}, {"page": 0}, {"page_size": 101}):
        assert clean.get("/attendance", params=bad).status_code == 422


# ------------------------------- regularization ---------------------------- #
def patch(c, code, day, **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return c.patch(f"/attendance/{code}/{day}", json=body)


def seed_closed(code="EMP0001", day="2026-07-07"):
    main.employees.insert_one(make_emp(code))
    main.logs.insert_one(make_log(code, day, wh=8.42, late=35, ot=0, punch_in=dt(2026, 7, 7, 10, 5), punch_out=dt(2026, 7, 7, 18, 30)))


def test_regularize_recomputes_and_audits(clean):
    seed_closed()
    before = main.logs.find_one({"emp_code": "EMP0001"})
    r = patch(clean, "EMP0001", "2026-07-07", punch_in=ms(2026, 7, 7, 9, 28))
    assert r.status_code == 200
    b = r.json()
    assert b["punch_in"] == ms(2026, 7, 7, 9, 28) and b["late_minutes"] == 0 and b["work_hours"] == 9.03 and b["overtime_minutes"] == 0
    assert len(b["history"]) == 1
    h = b["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"] == {"punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
                            "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}
    stored = main.logs.find_one({"emp_code": "EMP0001"})
    assert stored["history"][0]["changes"]["punch_in"]["from"] == before["punch_in"]  # BSON dates inside history
    assert stored["_id"] == before["_id"]


def test_regularize_history_is_append_only(clean):
    seed_closed()
    assert patch(clean, "EMP0001", "2026-07-07", punch_in=ms(2026, 7, 7, 9, 28), reason="first fix").status_code == 200
    r = patch(clean, "EMP0001", "2026-07-07", punch_out=ms(2026, 7, 7, 19, 30), regularized_by="mgr", reason="second fix")
    h = r.json()["history"]
    assert [e["reason"] for e in h] == ["first fix", "second fix"] and r.json()["overtime_minutes"] == 60
    assert set(h[1]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}
    assert h[0]["changes"]["punch_in"]["to"] == ms(2026, 7, 7, 9, 28)  # first entry untouched


def test_regularize_to_absent_and_back(clean):
    seed_closed()
    r = patch(clean, "EMP0001", "2026-07-07", status="LEAVE")
    b = r.json()
    assert r.status_code == 200 and b["punch_in"] is None and b["punch_out"] is None and b["work_hours"] is None
    assert b["late_minutes"] == 0 and b["half_day"] is False
    assert b["history"][0]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}
    assert b["history"][0]["changes"]["punch_in"]["to"] is None
    assert patch(clean, "EMP0001", "2026-07-07", status="WFH").status_code == 422  # no punch_in stored any more
    r = patch(clean, "EMP0001", "2026-07-07", status="WFH", punch_in=ms(2026, 7, 7, 9, 30), punch_out=ms(2026, 7, 7, 12, 30))
    assert r.status_code == 200 and r.json()["half_day"] is True and r.json()["work_hours"] == 3.0 and len(r.json()["history"]) == 2


def test_regularize_validation(clean):
    seed_closed()
    d = "2026-07-07"
    assert patch(clean, "EMP0001", d).status_code == 422  # changes nothing
    assert patch(clean, "EMP0001", d, status="PRESENT").status_code == 422
    assert patch(clean, "EMP0001", d, punch_in=ms(2026, 7, 7, 10, 5)).status_code == 422  # same value
    assert patch(clean, "EMP0001", d, status="ABSENT", punch_in=ms(2026, 7, 7, 9, 0)).status_code == 422
    assert patch(clean, "EMP0001", d, status="LEAVE", punch_out=ms(2026, 7, 7, 9, 0)).status_code == 422
    assert patch(clean, "EMP0001", d, punch_in=ms(2026, 7, 8, 9, 0)).status_code == 422  # different attendance date
    assert patch(clean, "EMP0001", d, punch_in=ms(2026, 7, 7, 19, 0)).status_code == 422  # now after punch_out
    assert patch(clean, "EMP0001", d, punch_out=ms(2026, 7, 7, 10, 5)).status_code == 422  # not after punch_in
    assert patch(clean, "EMP0001", d, punch_out=ms(2026, 7, 8, 10, 5, 1)).status_code == 422  # > 24h
    assert patch(clean, "EMP0001", d, punch_in=1783312500).status_code == 422
    assert patch(clean, "EMP0001", d, punch_in=ms(2026, 7, 7, 9, 0), reason="no").status_code == 422
    assert patch(clean, "EMP0001", d, punch_in=ms(2026, 7, 7, 9, 0), regularized_by="").status_code == 422
    assert clean.patch(f"/attendance/EMP0001/{d}", json={"punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422  # reason required
    assert patch(clean, "EMP0001", "2026-02-30", status="LEAVE").status_code == 422
    assert main.logs.find_one({"emp_code": "EMP0001"})["history"] == []  # nothing was written by the failures


def test_regularize_not_found(clean):
    seed_closed()
    assert patch(clean, "EMP9999", "2026-07-07", status="LEAVE").status_code == 404
    assert patch(clean, "EMP0001", "2026-07-09", status="LEAVE").status_code == 404


def test_regularize_ignores_client_derived_fields(clean):
    seed_closed()
    r = patch(clean, "EMP0001", "2026-07-07", punch_in=ms(2026, 7, 7, 9, 28), work_hours=1.0, late_minutes=99, half_day=True, overtime_minutes=5)
    assert r.status_code == 200 and r.json()["work_hours"] == 9.03 and r.json()["late_minutes"] == 0 and r.json()["half_day"] is False


def test_regularize_overnight_and_open_record(clean):
    main.employees.insert_one(make_emp("EMP0005", "Support", "22:00", "06:00"))
    punch_in(clean, "EMP0005", ms(2026, 7, 6, 22, 30))
    r = patch(clean, "EMP0005", "2026-07-06", punch_out=ms(2026, 7, 7, 7, 0))
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 60 and r.json()["work_hours"] == 8.5
    assert patch(clean, "EMP0005", "2026-07-06", punch_in=ms(2026, 7, 7, 7, 0)).status_code == 422  # 07:00 on 07-07 is the 07-07 shift


def test_regularize_concurrent_edits_never_lose_history(clean):
    seed_closed()

    def edit(i):
        return patch(clean, "EMP0001", "2026-07-07", punch_in=ms(2026, 7, 7, 9, 0, i), reason=f"edit number {i}").status_code
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(edit, range(1, 11)))
    assert set(codes) <= {200, 409} and codes.count(200) >= 1
    assert len(main.logs.find_one({"emp_code": "EMP0001"})["history"]) == codes.count(200)


def test_absurd_page_numbers_give_empty_page_not_500(clean):
    main.employees.insert_one(make_emp("EMP0001"))
    main.logs.insert_one(make_log("EMP0001", "2026-07-06", wh=8.0))
    for url in ("/employees", "/attendance"):
        for page in (10 ** 15, 10 ** 30):  # 10**30 overflows MongoDB's int64 skip if passed through unchanged
            r = clean.get(url, params={"page": page, "page_size": 100})
            assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 1
    assert clean.get("/admin/explain/attendance_list", params={"page": 10 ** 30}).status_code == 200
