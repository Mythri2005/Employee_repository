# Design decisions

1. **Indexes.** Unique indexes protect employee codes and `(emp_code, date)` (also supporting employee/date lookups). I also indexed department/join date, join date alone for headcount, date/employee for lists, status/date for filtered lists, date/late for the leaderboard, and employee/punch-in for punch-out. I skipped `history` because no query filters it.
2. **Punch-in race.** Both requests may pass a preliminary read. The unique attendance index lets one insert succeed; MongoDB rejects the other's duplicate, which the API maps to 409. Only one request returns 201.
3. **Ties.** The leaderboard uses MongoDB's competition rank on total late minutes alone, then filters `rank <= limit`. Everyone tied at the cutoff is returned; employee code only makes their display order deterministic.
4. **Headcount.** Department summary starts with eligible employees, not logs, and looks up each employee's month of logs. Preserving employees with no matching log means they still contribute one to headcount; joiners after month-end are filtered out.
5. **100x scale.** I would materialize daily employee/department aggregates and refresh them as attendance changes, keeping interactive analytics away from repeatedly joining the full log history.
