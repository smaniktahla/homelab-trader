def test_schema_smoke(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT slug, status FROM theses ORDER BY slug")
        rows = cur.fetchall()
    slugs = {r[0]: r[1] for r in rows}
    assert slugs["mean_reversion"] == "active"
    assert slugs["congress_shreve_hern"] == "backtesting_only"
