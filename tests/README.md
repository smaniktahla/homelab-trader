# Running the tests

These tests need a real (disposable) Postgres — the fixture-equivalence
test in particular runs actual SQL through actual `shared/signals.py`
code, which is the whole point of it. There's no mocked-DB layer.

```bash
docker run -d --name invest_test_pg \
  -e POSTGRES_USER=invest_test -e POSTGRES_PASSWORD=not_a_real_credential -e POSTGRES_DB=invest_test \
  -p 15432:5432 postgres:16-alpine

pip install -r tests/requirements.txt
TEST_DATABASE_URL="postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test" \
  python3 -m pytest tests/ -q
```

Each test function gets the per-cycle tables (`price_history`, `signals`,
`trade_proposals`, `signal_outcomes`, `symbol_features`, `watchlist`, ...)
truncated before it runs; config/reference tables (`signal_params`,
`theses`, `app_settings`) are left as `schema.sql` seeded them once per
session. See `tests/conftest.py` for exactly what's reset.

`test_fixture_equivalence.py` additionally shells out to `git show` to
load the pre-PR-#1 version of the scoring module for comparison. It tries
`main` first, then falls back to `origin/main` — a plain `git clone`
only ever gets a local branch for whatever ref was checked out, so `main`
itself is usually just a remote-tracking ref, not a local branch, and
`git show main:...` fails outright on a fresh clone. Either way it needs
the pre-PR-#1 commit reachable (true today; would need updating once this
PR merges and history moves on far enough that `main`'s tip no longer
predates it — at that point these two tests should be retired in favor of
whatever the next behavior-preservation baseline is).
