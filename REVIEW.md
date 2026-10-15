# Starter-code review

## Scope and evidence

The assignment brief identifies `app/main.py` as the starter and says it contains more than ten defects. This project
directory now contains the completed implementation, not a copy of that starter, and the newly initialized Git repository
has no earlier commit. The brief and the previous version of this review preserve the reported issue areas, but the
original code cannot be diffed to independently verify each starter-side defect. The table therefore separates those
reported starter findings from features the brief explicitly marked for implementation. The fixes below are checked
against the current code and contract; no unverified starter behavior is presented as a confirmed diff finding.

| # | Where (function / area) | What is wrong | How you'd notice it (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | Startup / `lifespan` | **Reported starter finding:** required natural-key indexes were absent. Without them concurrent duplicate requests can persist duplicate employees or attendance days. The original startup code is unavailable here to re-check. | Start twice and inspect indexes; race two inserts for the same `emp_code` or (`emp_code`, `date`). | Current lifespan calls `create_index` for unique `employees.emp_code` and unique `attendance_logs(emp_code, date)`, plus three query-supporting indexes. Repeated startup calls are covered by a fake-based test. |
| 2 | `EmployeeIn` | **Reported starter finding:** input validation did not fully enforce real dates and valid, differing shift times. | Submit `joined_on="2026-02-30"`, `shift_start="24:00"`, or identical start/end times; expect 422. | Current model validates the ISO calendar date, HH:MM ranges, and unequal shift times. |
| 3 | `create_employee` | **Reported starter finding:** a pre-insert duplicate lookup alone cannot prevent simultaneous duplicate creates. | Send parallel POSTs with the same employee code; exactly one must return 201 and the rest 409. | Current code retains a fast lookup and maps insert-time `DuplicateKeyError` to 409; the unique index is the integrity guarantee. |
| 4 | `create_employee` response | **Observed final-implementation defect, not attributed to the starter:** PyMongo adds `_id` to the inserted mapping; returning it caused FastAPI to fail serializing `ObjectId`. | Simulate an insert that adds an `ObjectId`; JSON-encode the response and check the contract fields. | Current code removes `_id`, converts `created_at` to epoch milliseconds, and returns only the employee response fields. `test_employee_creation_returns_epoch_milliseconds` covers this. |
| 5 | `list_employees` | **Reported starter finding:** filtering, stable ordering, pagination, filtered totals, or response serialization did not match the contract. The original code is unavailable to isolate which behavior was defective. | Filter by department and request page 2; check ascending `emp_code`, filtered `total`, page bounds, and epoch-millisecond `created_at`. | Current implementation counts with the same filter and uses database sort/skip/limit; `_id` is excluded and `created_at` is serialized. |
| 6 | `punch_in`, `_regularization_attendance_day`, `_late_minutes_for_attendance_day` | **Reported starter finding:** punch-in needed strict timestamp/status validation, 404 for unknown employees, IST/overnight date assignment, and race-safe duplicate handling. The original code is unavailable to re-check. | Try an invalid epoch-millisecond value or status, an unknown employee, an overnight punch before shift end, and parallel punches for one employee/date. | Current request models validate timestamp and status; the route returns 404 for an unknown employee, computes the IST attendance day and late minutes, truncates timestamps to seconds, and maps duplicate-key races to 409. |
| 7 | Calculation helpers / `punch_out` / regularization | **Reported starter finding:** time calculations can be wrong at grace, overtime, rounding, and overnight boundaries if they use local dates, float rounding, or the wrong shift end. The original helper code is unavailable to re-check. | For a 09:30 shift, test 09:40:00 versus 09:40:01; test 29:59 versus 30:00 overtime; test a half-up work-hour tie and an overnight shift. | Current helpers use IST-aware shift boundaries, truncate to whole seconds, floor minute totals, use `Decimal` with `ROUND_HALF_UP`, and calculate half-day from rounded work hours. Focused mocked tests cover these boundaries. |
| 8 | `list_attendance` | **Reported starter finding:** the endpoint needed inclusive date filters, range validation, stable ordering, pagination, filtered totals, and contract serialization. The original code is unavailable to re-check. | Request a reversed date range or a filtered second page; verify 422 or the correct page/count/order and that no `_id` or BSON datetime leaks. | Current route validates date order, applies filters to count and page queries, sorts by date descending then employee code ascending, and serializes each record with `_attendance_response`. |
| 9 | `POST /attendance/punch-out` | **Part B feature, not a starter defect:** the assignment marks this route “You” (not supplied as starter code). | Route inspection; punch-out an unknown employee, a missing/open record, a repeated record, and an overnight shift. | Implemented with a conditional atomic update so only one competing request can close an open record; response and error shapes follow the contract. Tests use fakes, not a live MongoDB race. |
| 10 | `PATCH /attendance/{emp_code}/{date}` | **Part B feature, not a starter defect:** the assignment marks this route “You”. | Correct a record and inspect derived values and history; race two regularizations and verify the loser receives 409 rather than overwriting history. | Implemented with validation, recomputed derived values, append-only history, and a compare-and-set update. The concurrency test is fake-based. |
| 11 | Analytics routes | **Part B features, not starter defects:** monthly employee analytics, department summary, late leaderboard, and department trend are marked “You”. | Inspect routes and test join dates, no-log headcount, half-days, ties at the rank cutoff, date gaps, weekends, and moving averages. | All four routes use MongoDB aggregation builders (`_employee_monthly_pipeline`, `_department_summary_pipeline`, `_late_leaderboard_pipeline`, `_department_trend_pipeline`); endpoint code does not fetch records for Python-side metric calculations. Unit tests inspect pipeline structure and fake results. |
| 12 | `GET /admin/explain/{endpoint}` | **Part C feature, not a starter defect:** the assignment requires this endpoint to return `executionStats` explain output for five named query paths. | Call each explain route with its required parameters and inspect the actual winning plan for `IXSCAN` and absence of `COLLSCAN`. | Current route builds explain commands with `verbosity="executionStats"` using the same query/pipeline builders, and returns JSON-safe Extended JSON. No post-fix live plan output is available in this review, so index use is not claimed. |

## Reviewed and retained

- `emp_code` is the business identifier; `_id` is internal and excluded from API responses. Attendance responses are
  shaped explicitly by `_attendance_response`.
- Analytics use stored `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` values, consistent with
  `DATA_MODEL.md`; they do not recompute them from punch timestamps.
- The department summary begins with eligible employees and then looks up logs, so employees without logs can count
  toward headcount. The leaderboard uses `$rank` before applying its rank cutoff, preserving ties.
- The department trend generates the inclusive date range and moving window inside MongoDB, rather than looping over
  fetched records in Python.
- The configured indexes are declared in startup and have stable key/options; the unit test invokes lifespan twice.
  This verifies the declarations, not Atlas index selection or execution performance.

## Verification and remaining concerns

- `python -m unittest discover -s tests -v`: **52 tests passed** in the existing project virtual environment. Test files:
  `test_analytics_routes.py`, `test_crud_contract.py`, `test_health.py`, `test_monthly_analytics.py`,
  `test_punch_out.py`, and `test_regularize.py`. These are unit tests using mocks/fakes; they are not proof of live
  MongoDB query behavior or database-backed concurrency.
- The aggregation calculations are expressed in MongoDB pipelines, but their execution against the assignment dataset
  was not established by this test run.
- No actual explain plan was inspected after the explain-response serialization fix. Whether each plan uses `IXSCAN`
  without `COLLSCAN` remains **unverified**; do not infer index use from index creation or pipeline tests.
- `_department_trend_pipeline` groups matching employees into one array before its attendance lookup. Its behavior is
  tested on small fake inputs, but its memory/performance behavior at the assignment scale and at 100x scale has not
  been benchmarked. `DECISIONS.md` identifies this as a design area to revisit.
- The unavailable starter source prevents a complete, independently verified list of the original starter's more than
  ten defects. Recover that source or a baseline commit if a line-by-line historical review is required.
