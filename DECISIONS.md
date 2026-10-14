# DECISIONS.md

1. **Indexes.** Unique `emp_code` and unique `(emp_code, date)` make duplicates impossible. `(date desc, emp_code)` and `(status, date desc, emp_code)` serve the sorted attendance list and the monthly leaderboard. `(emp_code, punch_in desc)` finds the record to punch out. `(department, joined_on)` serves headcount. I rejected an index on `late_minutes`: the date range already narrows the scan, and it would slow every punch.

2. **Punch-in race.** Both requests compute the same attendance date and call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed. The other gets `DuplicateKeyError`, which becomes 409. There is no check-then-insert.

3. **Ties.** Employees are ranked with `$rank` (1, 2, 2, 4) *before* `limit` is applied, so both tied employees are returned even if that means more than `limit` rows, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and looks up each person's logs. Someone with no logs gets an empty array and still counts as one.

5. **At 100x the data.** Keep per-employee daily and monthly rollups, updated on every punch or correction, so analytics read small summaries instead of raw logs; use keyset pagination instead of `skip`.
