# Running the tests

These tests need a real (disposable) Postgres — the fixture-equivalence
test in particular runs actual SQL through actual `shared/signals.py`
code, which is the whole point of it. There's no mocked-DB layer.

```bash
docker run -d --name invest_test_pg \
  -e POSTGRES_USER=invest -e POSTGRES_PASSWORD=investpass -e POSTGRES_DB=invest \
  -p 15432:5432 postgres:16-alpine

pip install -r tests/requirements.txt
TEST_DATABASE_URL="postgresql://invest:investpass@localhost:15432/invest" \
  python3 -m pytest tests/ -q
```

Each test function gets the per-cycle tables (`price_history`, `signals`,
`trade_proposals`, `signal_outcomes`, `symbol_features`, `watchlist`, ...)
truncated before it runs; config/reference tables (`signal_params`,
`theses`, `app_settings`) are left as `schema.sql` seeded them once per
session. See `tests/conftest.py` for exactly what's reset.

`test_fixture_equivalence.py` additionally shells out to
`git show main:shared/signals.py` to load the pre-PR-#1 version of the
scoring module for comparison — it must be run from a checkout where
`main` still has the pre-PR-#1 commit reachable (true for this branch;
would need updating once this PR merges and history moves on).
