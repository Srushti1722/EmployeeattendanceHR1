# Engineering decisions

1. **Indexes.** Unique `employees.emp_code` and `(emp_code, date)` indexes enforce natural keys. `(date desc, emp_code asc)` supports attendance sorting; `joined_on` serves the summary's unfiltered date match; `(department, joined_on)` supports department filters. I rejected standalone `status`: it has low selectivity and the list is date-sorted.

2. **Punch-in race.** Both requests may pass the initial lookup. The unique compound index allows one insert; MongoDB rejects the other with `DuplicateKeyError`. The insert handler maps it to 409. The lookup improves the usual response, but the index is the integrity guarantee.

3. **Ties.** MongoDB competition rank gives tied totals the same rank and skips the next rank. The limit is applied afterward, so every employee tied at or above the cutoff rank is returned.

4. **Headcount.** The summary starts with employees joined by month end, then looks up attendance. Employees with no matching logs remain and count toward headcount.

5. **100x scale.** I would test representative data and redesign trend aggregation to avoid large department arrays. `explain` helps inspect scans and indexes, but plans must be verified on the target dataset.
