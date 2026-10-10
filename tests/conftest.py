"""Integration-test setup: talks to a REAL MongoDB, in a throw-away database.

TEST_MONGO_URI (default mongodb://localhost:27017) selects the server. The database name is generated
(hrone_test_<random>) and dropped at the end, so no existing database or dataset is touched.
If no MongoDB answers, every test is skipped with an explicit reason (nothing is faked).
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TEST_DB = f"hrone_test_{uuid.uuid4().hex[:8]}"
os.environ["MONGO_URI"] = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = TEST_DB
os.environ["MONGO_TIMEOUT_MS"] = "2000"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ms(y, mo, d, h=0, mi=0, s=0, ms_=0):
    """IST wall-clock -> epoch milliseconds."""
    dt = datetime(y, mo, d, h, mi, s, tzinfo=IST)
    return int(dt.timestamp()) * 1000 + ms_


def dt(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


@pytest.fixture(scope="session")
def client():
    assert main.db.name == TEST_DB and TEST_DB.startswith("hrone_test_")
    try:
        main.client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB not reachable at TEST_MONGO_URI: {exc.__class__.__name__}", allow_module_level=False)
    with TestClient(main.app) as c:  # runs the lifespan -> creates indexes
        yield c
    from pymongo import MongoClient  # the app's own client is closed by the lifespan shutdown
    with MongoClient(os.environ["MONGO_URI"]) as admin:
        admin.drop_database(TEST_DB)


@pytest.fixture()
def clean(client):
    main.employees.delete_many({})
    main.logs.delete_many({})
    return client


def make_emp(code, dept="Engineering", start="09:30", end="18:30", joined="2026-01-05"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": dt(2026, 1, 5, 10)}


def make_log(code, day, status="PRESENT", wh=None, late=0, ot=0, half=False, **extra):
    punch = status in ("PRESENT", "WFH", "ON_DUTY")
    doc = {"emp_code": code, "date": day, "status": status, "punch_in": dt(*map(int, day.split("-")), 9, 30) if punch else None,
           "punch_out": None, "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
    doc.update(extra)
    return doc
