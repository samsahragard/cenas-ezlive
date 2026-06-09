"""Cenas Floor Pulse V2 - data layer unit tests.

Covers the behaviors that the V2 update was specifically asked to fix:
- Today resets by real date (zeros + empty leaderboard).
- Week/Month/Last30 carry stats + a full peer leaderboard.
- Peer ranking is a composite of normalized tips/hr + tip%, ranked, "#N of M".
- Unknown cash tips stay neutral (the cash-unknown peer still ranks, no crash).
- No divide-by-zero anywhere.
"""
from app.services import floor_pulse as fp


def test_today_resets_to_zero():
    stats = fp.employee_stats("today")
    assert stats["take_home"] == 0.0
    assert stats["projected_take_home"] == 0.0
    assert stats["tips"] == 0.0
    assert stats["hours"] == 0.0
    assert stats["total_tables"] == 0
    assert stats["open_tickets"] == 0
    # And the day's peer ranking is empty -> "No ranked shift yet".
    assert fp.build_leaderboard("today") == []


def test_week_has_stats_and_full_leaderboard():
    stats = fp.employee_stats("week")
    assert stats["tips"] > 0
    assert stats["hours"] > 0
    assert stats["tip_per_hour"] > 0
    assert round(stats["tip_pct"], 1) == 18.0  # Kennya: 332.62 / 1848.13
    lb = fp.build_leaderboard("week")
    # 12 peers minus the one pure-cash server held out of ranking = 11.
    assert len(lb) == 11
    # Ranks are 1..N contiguous and carry the cohort size.
    assert [r["rank"] for r in lb] == list(range(1, 12))
    assert all(r["of"] == 11 for r in lb)


def test_my_rank_present_in_non_today_ranges():
    for rng in ("week", "month", "last30"):
        lb = fp.build_leaderboard(rng)
        me = fp.my_rank(lb)
        assert me is not None, rng
        assert me["is_me"] is True
        assert 1 <= me["rank"] <= me["of"]


def test_leaderboard_is_composite_of_tiphr_and_tippct():
    lb = fp.build_leaderboard("week")
    # Score is two normalized halves of 50 each, in [0, 100], sorted descending.
    scores = [r["score"] for r in lb]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= s <= 100.0001 for s in scores)
    # Verify the composite formula directly: score == tip/hr-norm*50 + tip%-norm*50.
    # (A balanced performer can outrank either single-axis leader -- that is the
    # whole point of a composite, so we check the math, not "owns a maximum".)
    max_tip_hr = max(r["tip_per_hour"] for r in lb)
    max_tip_pct = max(r["tip_pct"] for r in lb)
    for r in lb:
        expected = (r["tip_per_hour"] / max_tip_hr) * 50 + (r["tip_pct"] / max_tip_pct) * 50
        assert abs(r["score"] - expected) < 1e-6
    # The #1 row has the single highest composite score.
    assert lb[0]["score"] == max(scores)


def test_unknown_cash_tip_is_neutral_not_ranked_last():
    # Meher has 0 RECORDED (card) tips and a cash_unknown marker -- her only tip
    # signal is unknown cash. Ranking her on a recorded-tips composite would
    # score her 0 and push her to last place, i.e. punish the unknown cash. The
    # neutral behavior is to hold her OUT of the ranked cohort and surface her as
    # "cash tips pending" -- never ranked dead last.
    lb = fp.build_leaderboard("week")
    assert all(r["id"] != "meher-hayr" for r in lb), "pure-cash server must not be ranked"
    # ...and she appears in the held-out cash-pending bucket instead.
    pending = fp.cash_pending_peers("week")
    meher = next((r for r in pending if r["id"] == "meher-hayr"), None)
    assert meher is not None
    assert meher["cash_unknown"] == 1
    # A server WITH recorded card tips plus a cash_unknown marker (Melissa) is
    # NOT held out -- her ranking stands on her real recorded tips.
    assert any(r["id"] == "melissa-aguilera" for r in lb)
    # The held-out servers shrink the ranked cohort size (of N) honestly.
    assert lb[0]["of"] == len(lb)


def test_no_divide_by_zero_on_empty_inputs():
    # All aggregate ratios are safe on empty input.
    agg = fp.aggregate_rows([])
    assert agg["tip_per_hour"] == 0.0
    assert agg["tip_pct"] == 0.0
    assert agg["avg_check"] == 0.0
    # A peer with zero hours/sales does not blow up the leaderboard normalizers.
    assert fp.build_leaderboard("today") == []


def test_ranges_are_known_keys():
    assert set(fp.RANGE_KEYS) == {"today", "week", "month", "last30"}


def test_today_tickets_empty_yesterday_full():
    assert fp.today_tickets() == []
    yest = fp.yesterday_tickets()
    assert len(yest) == 7
    assert {t["table_id"] for t in yest} >= {"62B", "61B", "A1", "32"}


def test_filter_counts_and_filtering():
    tickets = fp.yesterday_tickets()
    counts = fp.filter_counts(tickets)
    assert counts["all"] == 7
    assert counts["mine"] == sum(1 for t in tickets if t["owner"])
    assert counts["attention"] == sum(1 for t in tickets if t["status"] == "attention")
    # "new" = opened within 20 min
    assert counts["new"] == sum(1 for t in tickets if t["opened_mins"] <= 20)
    mine = fp.filter_tickets(tickets, "mine")
    assert all(t["owner"] for t in mine)


def test_ticket_view_open_estimates_tip_closed_uses_actual():
    open_t = next(t for t in fp.yesterday_tickets() if t["status"] == "open")
    closed_t = next(t for t in fp.yesterday_tickets() if t["status"] == "closed" and t["tip"])
    ov = fp.ticket_view(open_t)
    cv = fp.ticket_view(closed_t)
    # Open check: estimate at the configured rate, no final tip%.
    assert abs(ov["est_tip"] - open_t["amount"] * fp.OPEN_TIP_ESTIMATE_RATE) < 1e-6
    assert ov["tip_pct"] is None
    # Closed check: real tip% present.
    assert cv["tip_pct"] is not None and cv["tip_pct"] > 0


def test_technical_rows_shape():
    rows = fp.technical_rows(fp.employee_stats("week"))
    labels = [r[0] for r in rows]
    assert labels[:3] == ["Tickets", "Avg drink", "Avg apps"]
    assert "Tip %" in labels
    assert "CC tabs" in labels and "CC tips" in labels
    # Every row is a (label, display-string) pair.
    assert all(isinstance(v, str) for _, v in rows)
