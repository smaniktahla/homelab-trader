from datetime import datetime, timezone

import fundamentals as f


def _insert_fact(conn, symbol, metric, value, period_end, accepted_at, fiscal_period="Q1-2026"):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fundamental_facts
                (symbol, metric, value, unit, fiscal_period, period_start, period_end,
                 filed_at, accepted_at, form_type, accession_number, source)
            VALUES (%s, %s, %s, 'USD', %s, %s, %s, %s, %s, '10-Q', %s, 'sec_edgar')
        """, (symbol, metric, value, fiscal_period, period_end, period_end,
              accepted_at, accepted_at, f"acc-{symbol}-{metric}-{period_end}"))
    conn.commit()


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_no_data_returns_none(conn):
    assert f.compute_fundamental_score(conn, "AAPL", _dt(2026, 6, 1)) is None


def test_revenue_only_scores_without_growth_or_margins(conn):
    _insert_fact(conn, "AAPL", "Revenues", 1000, _dt(2026, 3, 31), _dt(2026, 4, 15))
    score = f.compute_fundamental_score(conn, "AAPL", _dt(2026, 6, 1))
    # No prior-year revenue and no margin data -> nothing to score at all
    # (revenue alone isn't a scoreable input by itself in this formula).
    assert score is None


def test_point_in_time_excludes_facts_filed_after_as_of(conn):
    _insert_fact(conn, "AAPL", "Revenues", 1000, _dt(2026, 3, 31), _dt(2026, 4, 15))
    _insert_fact(conn, "AAPL", "GrossProfit", 400, _dt(2026, 3, 31), _dt(2026, 4, 15))
    # A later, better-looking fact that wasn't public yet as of as_of.
    _insert_fact(conn, "AAPL", "GrossProfit", 900, _dt(2026, 3, 31), _dt(2026, 8, 1),
                 fiscal_period="Q1-2026-restated")

    score_before_restatement = f.compute_fundamental_score(conn, "AAPL", _dt(2026, 6, 1))
    score_after_restatement = f.compute_fundamental_score(conn, "AAPL", _dt(2026, 9, 1))

    assert score_before_restatement is not None
    assert score_after_restatement is not None
    # The restated (higher) gross margin must only affect scores computed
    # as of a time when that filing had actually been accepted -- using it
    # for the earlier as_of would be look-ahead bias.
    assert score_after_restatement > score_before_restatement


def test_revenue_growth_uses_prior_year_comparable_period(conn):
    _insert_fact(conn, "AAPL", "Revenues", 1000, _dt(2025, 3, 31), _dt(2025, 4, 15))
    _insert_fact(conn, "AAPL", "Revenues", 1200, _dt(2026, 3, 31), _dt(2026, 4, 15))
    _insert_fact(conn, "AAPL", "GrossProfit", 500, _dt(2026, 3, 31), _dt(2026, 4, 15))

    score = f.compute_fundamental_score(conn, "AAPL", _dt(2026, 6, 1))
    assert score is not None

    # 20% growth should score meaningfully higher than flat/negative growth.
    _insert_fact(conn, "MSFT", "Revenues", 1000, _dt(2025, 3, 31), _dt(2025, 4, 15))
    _insert_fact(conn, "MSFT", "Revenues", 950, _dt(2026, 3, 31), _dt(2026, 4, 15))
    _insert_fact(conn, "MSFT", "GrossProfit", 500, _dt(2026, 3, 31), _dt(2026, 4, 15))
    declining_score = f.compute_fundamental_score(conn, "MSFT", _dt(2026, 6, 1))

    assert score > declining_score


def test_missing_prior_year_revenue_excludes_growth_component_not_zero(conn):
    # Only current-year revenue + gross profit -- no prior-year comparable,
    # so growth can't be computed. Must not be treated as 0% growth.
    _insert_fact(conn, "AAPL", "Revenues", 1000, _dt(2026, 3, 31), _dt(2026, 4, 15))
    _insert_fact(conn, "AAPL", "GrossProfit", 500, _dt(2026, 3, 31), _dt(2026, 4, 15))
    score_no_growth_data = f.compute_fundamental_score(conn, "AAPL", _dt(2026, 6, 1))

    _insert_fact(conn, "MSFT", "Revenues", 1000, _dt(2025, 3, 31), _dt(2025, 4, 15))
    _insert_fact(conn, "MSFT", "Revenues", 500, _dt(2026, 3, 31), _dt(2026, 4, 15))  # -50% growth, bad
    _insert_fact(conn, "MSFT", "GrossProfit", 500, _dt(2026, 3, 31), _dt(2026, 4, 15))  # same margin
    score_with_bad_growth = f.compute_fundamental_score(conn, "MSFT", _dt(2026, 6, 1))

    # Missing growth data (excluded from the average) must score higher
    # than a genuinely bad growth figure (included and dragging it down) --
    # if missing data were silently treated as 0%/worst-case, these two
    # would come out equal or inverted.
    assert score_no_growth_data > score_with_bad_growth


def test_zero_prior_year_revenue_excluded_not_divide_by_zero(conn):
    _insert_fact(conn, "AAPL", "Revenues", 0, _dt(2025, 3, 31), _dt(2025, 4, 15))
    _insert_fact(conn, "AAPL", "Revenues", 1000, _dt(2026, 3, 31), _dt(2026, 4, 15))
    _insert_fact(conn, "AAPL", "GrossProfit", 500, _dt(2026, 3, 31), _dt(2026, 4, 15))
    # Must not raise ZeroDivisionError -- growth component silently
    # excluded, gross margin still contributes.
    score = f.compute_fundamental_score(conn, "AAPL", _dt(2026, 6, 1))
    assert score is not None
