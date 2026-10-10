# Employee Attendance & Analytics API

HROne engineering assignment: a FastAPI + MongoDB service for punch-in/out, manual corrections with an audit trail
(`history`), and analytics built on MongoDB aggregation pipelines. All application code is in `app/main.py`.

**Stack:** Python 3.11+, FastAPI, PyMongo (sync, thread-pool endpoints), MongoDB 6.0+.

## Run it
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # optional; real environment variables win over .env
uvicorn app.main:app --port 8000
```
Environment: `MONGO_URI` (default `mongodb://localhost:27017`), `MONGO_DB` (default `attendance_db`).
Indexes are created at startup; no manual steps. `GET /health` returns 200 only when MongoDB answers.
Interactive docs: http://localhost:8000/docs

Any MongoDB 6.0+ works, e.g. a local container: `docker run -p 27017:27017 mongo:7`, or Atlas M0.

## Tests
```bash
pip install -r requirements-dev.txt
pytest -q tests                                        # needs a MongoDB; override with TEST_MONGO_URI
```
The tests are real integration tests: they use a throw-away database named `hrone_test_<random>` which is dropped at
the end, so no existing data is touched. If no MongoDB is reachable, the tests are skipped (not faked).

## Endpoints
`GET /health` · `POST/GET /employees` · `POST /attendance/punch-in` · `POST /attendance/punch-out` · `GET /attendance` ·
`PATCH /attendance/{emp_code}/{date}` · `GET /analytics/employees/{emp_code}/monthly` · `GET /analytics/departments/summary` ·
`GET /analytics/leaderboard/late` · `GET /analytics/departments/{department}/trend` · `GET /admin/explain/{endpoint}`

## Status
Every endpoint of the supplied API contract is implemented. Verified: all 88 automated tests pass on MongoDB 7.0 and on
MongoDB 6.0.28; on both, explain plans show `IXSCAN` and no `COLLSCAN` for all five explain targets on a generated
100,000-record collection (slowest endpoint, the department summary, about 134 ms there).

## Interpretation choices (where the contract is silent)
- Trend range limit: the contract says 422 when "the range exceeds 92 days" and does not say whether that is the date difference or the number of rows. I count rows (from..to inclusive): 92 rows is accepted, 93 rows returns 422. Counting the difference instead would accept 93 rows; the two readings differ only at exactly 93 rows.
- `moving_avg_7d` averages the *published* (4-decimal rounded) `attendance_rate` values.
- Punch-out checks the "already punched out" (409) state before the time-window rule (422).
- A correction "changes nothing" (422) when status, `punch_in` and `punch_out` all keep their values.
- `working_days` for the monthly endpoint is calendar arithmetic in Python; the counts, sums, rankings, window
  functions, gap filling and half-up rounding are all done inside the MongoDB pipelines.
- Concurrent corrections use an optimistic guard; the loser gets 409 and can retry.
- A page number beyond the data (even an absurd one) returns an empty page with 200; a trend range ending on 9999-12-31 is rejected with 422.

## Known limitations
No authentication (none in the contract). The department trend first reads the department's `emp_code`/`joined_on`
values and passes them into the pipeline (`$in` list and headcount filter), which is fine for thousands of employees but
not for millions.
