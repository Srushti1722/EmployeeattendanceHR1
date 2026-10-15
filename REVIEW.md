# Starter-code review

## Evidence scope

The assignment brief says the supplied starter had more than ten defects, and identifies the routes supplied versus the
routes to be built. The current project contains the completed implementation. Its Git history starts with that
implementation; it does not contain the original starter. Therefore, no historical starter defect can be independently
verified as a before/after finding from source. Historical reports are labeled unverified below. Current behavior and
the fixes made during this audit are grounded in the implementation and listed tests.

## Verified defects in the original starter

None can be verified from the files or Git history currently available. The assignment's statement that the starter had
more than ten defects is not, by itself, evidence of each defect's location or behavior.

## Reported but unverified starter findings

These issues were reported in the previous review, but the original code is unavailable for confirmation. The “How it
was fixed” column describes the current implementation, not a verified diff from the starter.

| # | Where (function / area) | What is wrong | How you'd notice it (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | Startup / `lifespan` | Reported: missing unique natural-key indexes would allow duplicate employee codes or attendance days. | Start the app twice and inspect index definitions; concurrently insert one `emp_code` or (`emp_code`, `date`). | Current startup declares unique `employees.emp_code` and `attendance_logs(emp_code, date)` indexes. A fake-based test verifies declarations on repeated lifespan entry; Atlas index state is not verified here. |
| 2 | `EmployeeIn`, `create_employee`, `list_employees` | Reported: input validation, duplicate races, or pagination/serialization differed from the contract. Exact starter behavior cannot be checked. | Invalid calendar date or equal shift times; same-code concurrent creates; filtered page 2 and response-field checks. | Current model validates dates and shifts; duplicate-key errors return 409; listing uses filtered counts, stable sort, database pagination, excludes `_id`, and converts `created_at` to epoch milliseconds. |
| 3 | `punch_in`, time helpers | Reported: missing employee/status/timestamp validation, incorrect IST/overnight attendance day, or incorrect late calculations. | Unknown employee; seconds instead of epoch milliseconds; overnight punch before shift end; 09:40:00 versus 09:40:01 for a 09:30 shift. | Current models validate status and timestamp; route returns 404 for an unknown employee; helpers apply IST/overnight date assignment, whole-second truncation, and the R2 grace/floor calculation. |
| 4 | `punch_out`, calculation helpers | Reported: missing endpoint or incorrect overtime, work-hour rounding, half-day, overnight, or concurrency behavior. | Exercise 29:59/30:00 overtime, a half-up work-hour tie, overnight shift end, repeated or parallel punches. | Implemented a conditional atomic update and Decimal half-up duration calculation. Fake-based tests cover races and boundaries; a real MongoDB race has not been run here. |
| 5 | `list_attendance` | Reported: incorrect filtering, date ordering, pagination, totals, validation, or BSON response serialization. | Reversed date range or a filtered second-page request; inspect returned schema and ordering. | Current route validates inclusive date bounds, sorts by date descending and employee code ascending, paginates and counts with the same filters, and shapes records through `_attendance_response`. |
| 6 | `regularize_attendance` | Reported: missing correction validation, stale derived values, overwritten history, or lost concurrent edits. | Correct an overnight timestamp; verify only changed fields appear in one appended audit entry; race two corrections. | Current implementation validates and recomputes derived fields, appends history, and uses a compare-and-set update. Tests use fakes, not a real database race. |

## Assignment features to build (not starter defects)

The brief marks punch-out, regularization, four analytics routes, and the explain route as candidate implementation work.

| # | Where (function / area) | What is wrong | How you'd notice it (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | `POST /attendance/punch-out` | This was a required missing feature, not a starter-code defect. | Inspect route registration; test unknown employee, missing/open/closed record, overnight shift, and competing requests. | Implemented with a conditional MongoDB update. Unit tests use fakes; real concurrency remains unverified. |
| 2 | `PATCH /attendance/{emp_code}/{date}` | This was a required missing feature, not a starter-code defect. | Test invalid/no-op updates, unknown resources, history preservation, and concurrent corrections. | Implemented with validation, derived-field recomputation, append-only history, and atomic compare-and-set. |
| 3 | `GET /analytics/employees/{emp_code}/monthly` | This was a required missing feature, not a starter-code defect. | Test month bounds, join date, weekdays/weekends, half days, empty months, and unknown employee. | Implemented by `_employee_monthly_pipeline`; calculations run in MongoDB. Unit tests inspect pipeline structure and fake results. |
| 4 | `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late` | These were required missing features, not starter-code defects. | Check no-log headcount, record-level average, ranking ties and the cutoff limit. | Implemented as MongoDB aggregations. The leaderboard uses `$rank` before filtering by rank; tests inspect pipeline structure and fake results. |
| 5 | `GET /analytics/departments/{department}/trend` | This was a required missing feature, not a starter-code defect. | Check every date in range, join-date headcount, weekends, missing logs, null rates, and the in-range seven-row moving average. | Implemented in MongoDB. The pipeline now looks up each employee's bounded date range and groups daily rows afterward, avoiding one department-wide employee array. Execution/performance on Atlas remains unverified. |
| 6 | `GET /admin/explain/{endpoint}` | This was a required missing feature, not a starter-code defect. | Call each supported path with its required parameters and inspect execution statistics and winning stages. | Implemented with `executionStats` and the route's query/pipeline builders. The verifier now fails a plan unless it sees `IXSCAN` and no `COLLSCAN`; no live plan has been captured in this environment. |

## Defects found in the completed implementation and corrected

| # | Where (function / area) | What is wrong | How you'd notice it (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | `create_employee` response | PyMongo can attach `_id: ObjectId` to the inserted document; returning it directly causes FastAPI JSON serialization to fail and violates the response schema. | Simulate an insert that adds an `ObjectId`; JSON-encode the response and verify only contract fields remain. | Current route removes `_id` and returns the employee fields with epoch-millisecond `created_at`. `test_employee_creation_returns_epoch_milliseconds` covers the case. |
| 2 | Mongo client / `health` | The default server-selection timeout could exceed the assignment's 20-second readiness window when MongoDB is unavailable. | Inspect the configured PyMongo option; simulate a failed ping for the 503 contract. | Client selection now times out after 5 seconds. `test_mongo_selection_timeout_is_below_readiness_deadline` asserts the configured timeout; health status tests mock ping success/failure. |
| 3 | `_department_trend_pipeline` | Grouping all department employees into one BSON array risks exceeding MongoDB's 16 MB document limit as the department grows. | Inspect the pipeline for department-wide `$push`; scale a department until that grouped document exceeds the BSON limit. | The pipeline now performs a bounded per-employee lookup (at most one natural-key attendance record per employee/date), expands the at-most-92-day request range, and groups daily totals afterward. Structural tests confirm the global employee array is gone. Live load/performance is unverified. |
| 4 | `verify_mongodb.report_explain` | The verifier previously marked any HTTP 200 explain response as PASS even if the winning plan had `COLLSCAN` or no `IXSCAN`. | Feed an explain response with `COLLSCAN` or `EOF` to the verifier. | It now marks a plan PASS only when an `IXSCAN` is present and `COLLSCAN` is absent. Three focused tests cover passing and failing plan cases. |

## Reviewed and retained

- `emp_code` is the employee identifier; `_id` is internal. Employee creation/listing and attendance responses do not
  expose `_id`.
- Analytics read stored derived attendance values as required by `DATA_MODEL.md`; they do not fetch attendance
  collections into Python for metric calculations.
- Department summary begins from employees eligible by month end and then looks up logs, retaining employees with no
  logs. The leaderboard ranks before applying its cutoff. The trend generates the inclusive calendar range and moving
  average in MongoDB.
- Startup declares five application indexes: two unique natural-key indexes, attendance date/code sorting, employee
  join-date, and department/join-date. The test checks declarations twice; it does not prove real index use.
- `openapi.yaml` identifies all required endpoint paths, response fields, errors, and R1-R10; the test suite checks
  representative behavior and route/pipeline shapes, not every case on the hidden dataset.

## Verification and remaining limitations

- `python -m unittest discover -s tests -v` in the project virtual environment: **57 tests passed** after this audit's
  changes. Test modules use mocks/fakes; these results do not prove MongoDB aggregation execution or database-backed
  concurrency.
- At the time of verification, `MONGO_URI` and `MONGO_DB` were absent from the process environment. Consequently no
  live aggregation, startup against Atlas, or explain plan was run. Actual `IXSCAN`/`COLLSCAN` results are unknown.
- The trend rewrite avoids the large employee array, but its per-employee/date expansion and overall latency have not
  been benchmarked at 100,000 records or 100x volume.
- The original starter source is unavailable in this repository and no pre-implementation Git commit exists. The
  brief's “more than ten defects” statement cannot be substantiated item by item without that source.
