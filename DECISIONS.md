# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` (one record per day, so it also decides punch-in races; it serves per-employee lookups and the monthly pipeline); `(date desc, emp_code)` (the global list sort and month-range scans); `(emp_code, punch_in desc)` (latest record at or before a punch-out time). `employees`: unique `emp_code`; `(department, emp_code)` (list by department, sorted); `joined_on` and `(department, joined_on)` (headcount match). No index on `status` was created: it has five values, so on its own it narrows little; `status` filters are applied while reading through the date or employee indexes.

2. **Punch-in race.** The code does not check first. Both requests call `insert_one`; the unique `(emp_code, date)` index lets MongoDB accept exactly one. The other fails with `DuplicateKeyError`, which the code turns into a 409.

3. **Ties.** The leaderboard ranks total late minutes with `$rank`, so tied employees share a rank and the next rank is skipped (1, 1, 3). `limit` is applied to the rank after ranking, so everyone tied at a rank within the limit is returned, even if that is more rows than `limit`, ordered by `emp_code`.

4. **Headcount.** The department summary starts from `employees` (joined on or before month end) and uses `$lookup` to attach that month's logs. An employee with no logs gets an empty array and is still counted.

5. **100x the data.** One option is a per-employee, per-day (or per-month) totals collection updated on punch and correction, so analytics read those instead of raw logs; the cost is an extra write path to keep consistent.
