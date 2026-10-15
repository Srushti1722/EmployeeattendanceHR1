# Engineering decisions

1. **Indexes.** Startup declares five indexes: unique employee code and attendance `(emp_code, date)` for natural-key integrity; attendance `(date desc, emp_code asc)` for listing; employee `joined_on` and `(department, joined_on)` for summary matching. A fake test checks repeated startup declarations; real selection needs live explain output.

2. **Concurrent punch-ins.** Both requests may pass the lookup, but the unique `(emp_code, date)` index allows one insert. The loser gets `DuplicateKeyError` and returns 409. The pre-check improves the common response; the index is the integrity guarantee. Concurrency tests use fakes, not live MongoDB.

3. **Leaderboard ties.** MongoDB competition rank is applied before the limit. All people tied at the cutoff rank are returned, so the response can exceed `limit`.

4. **Employees without logs.** Summary starts from employees joined by month end and looks up logs. No-match employees remain in headcount and contribute zero presence.

5. **100x scale.** Trend avoids one department-wide employee array and bounds each employee lookup to 92 days, but still processes each eligible employee. I would benchmark representative data and inspect execution statistics before considering materialized daily summaries. No 100,000-record or 100x benchmark has run.
