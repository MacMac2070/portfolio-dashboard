"""track.py compounds the funding-aware daily series into everything the
Performance page prints — returns, drawdowns, streaks. All series here are
hand-crafted so every expected value is checkable on paper."""
import pytest

import track


def S(*pairs):
    """[("2026-01-05", 0.01), ...] -> the series shape track consumes."""
    return [{"date": d, "r": r, "pnl": round(r * 1000, 2)} for d, r in pairs]


# ---------------------------------------------------------------- _compound

def test_compound_empty_is_zero():
    assert track._compound([]) == pytest.approx(0.0)


def test_compound_multiplies_not_adds():
    rows = S(("2026-01-05", 0.10), ("2026-01-06", -0.05))
    assert track._compound(rows) == pytest.approx(1.10 * 0.95 - 1.0)


# ---------------------------------------------------------------- stats

def test_stats_needs_five_days():
    assert track.stats(S(("2026-01-05", 0.01))) is None


def test_stats_periods_compound_correctly():
    rows = S(
        ("2025-12-30", 0.02), ("2025-12-31", 0.01),
        ("2026-01-02", 0.01), ("2026-01-05", -0.02), ("2026-01-06", 0.03),
    )
    st = track.stats(rows)
    assert st["asof"] == "2026-01-06"
    si = 1.02 * 1.01 * 1.01 * 0.98 * 1.03 - 1.0
    ytd = 1.01 * 0.98 * 1.03 - 1.0            # 2026 rows only
    assert st["si"] == pytest.approx(si, abs=1e-5)
    assert st["ytd"] == pytest.approx(ytd, abs=1e-5)
    assert st["mtd"] == pytest.approx(ytd, abs=1e-5)   # January: MTD == YTD
    # No benchmark feed supplied: relative figures stand down honestly.
    assert st["bench"] is None
    assert st["ratios"] is None


def test_stats_ratio_gate_below_sixty_observations():
    rows = S(*[(f"2026-01-{d:02}", 0.001) for d in range(1, 21)])
    closes = [(f"2026-01-{d:02}", 100 + d) for d in range(1, 21)]
    st = track.stats(rows, index_history=lambda s: closes, benchmark_symbol="^X")
    assert st["bench"] is not None            # period returns align fine
    assert st["ratios"] is None               # but 19 aligned days is a coin toss


# ---------------------------------------------------------------- drawdown

def test_drawdown_flat_series_has_no_episodes():
    dd = track.drawdown(S(("2026-01-05", 0.01), ("2026-01-06", 0.02)))
    assert dd["episodes"] == []
    assert dd["current"] == 0.0


def test_drawdown_episode_with_recovery():
    rows = S(
        ("2026-01-05", 0.10),   # peak
        ("2026-01-06", -0.10),  # underwater
        ("2026-01-07", -0.05),  # trough
        ("2026-01-08", 0.20),   # recovers past the peak
    )
    dd = track.drawdown(rows)
    assert len(dd["episodes"]) == 1
    ep = dd["episodes"][0]
    assert ep["peak_date"] == "2026-01-05"
    assert ep["trough_date"] == "2026-01-07"
    assert ep["recovered"] == "2026-01-08"
    assert ep["depth"] == pytest.approx(0.90 * 0.95 - 1.0, abs=1e-4)
    assert dd["current"] == 0.0


def test_drawdown_still_underwater():
    dd = track.drawdown(S(("2026-01-05", 0.05), ("2026-01-06", -0.10)))
    assert len(dd["episodes"]) == 1
    assert dd["episodes"][0]["recovered"] is None
    assert dd["current"] == pytest.approx(-0.10, abs=1e-4)


# ---------------------------------------------------------------- day_stats

def test_day_stats_win_rate_and_streaks():
    rows = S(
        ("2026-01-05", 0.01), ("2026-01-06", 0.02),                     # +2
        ("2026-01-07", -0.01), ("2026-01-08", -0.02), ("2026-01-09", -0.01),  # -3
        ("2026-01-12", 0.03),
    )
    d = track.day_stats(rows)
    assert d["n"] == 6
    assert d["win_rate"] == pytest.approx(0.5)
    assert d["best_streak"] == 2
    assert d["worst_streak"] == -3
    assert d["best"][0]["r"] == pytest.approx(0.03)      # ranked, best first
    assert d["worst"][0]["r"] == pytest.approx(-0.02)


# ---------------------------------------------------------------- xirr & mwr

def test_xirr_recovers_a_known_annual_rate():
    rate = track._xirr([("2025-01-01", -1000.0), ("2026-01-01", 1100.0)])
    assert rate == pytest.approx(0.10, abs=2e-3)


def test_xirr_needs_both_directions():
    assert track._xirr([("2025-01-01", -1000.0), ("2026-01-01", -100.0)]) is None


def test_stats_mwr_equals_twr_when_nothing_flows():
    # With no external flows the money-weighted and time-weighted period
    # returns are the same number — the identity that keeps both honest.
    nav = 1000.0
    rows = []
    for d in ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"):
        pnl = round(nav * 0.01, 6)
        nav = nav + pnl
        rows.append({"date": d, "nav": nav, "flow": 0.0, "pnl": pnl, "r": 0.01})
    st = track.stats(rows)
    assert st["mwr"] is not None
    assert st["mwr"]["period"] == pytest.approx(st["si"], abs=1e-3)


def test_stats_without_nav_fields_skips_mwr():
    st = track.stats(S(*[(f"2026-01-{d:02}", 0.001) for d in range(1, 8)]))
    assert st["mwr"] is None                   # bare returns: no flows to weigh


# ---------------------------------------------------------------- risk & ath

def test_stats_risk_maxdd_sortino_calmar():
    rows = S(
        *[(f"2026-{1 + d // 28:02}-{1 + d % 28:02}", 0.005) for d in range(30)],
        ("2026-03-01", -0.05),
        *[(f"2026-{4 + d // 28:02}-{1 + d % 28:02}", 0.0) for d in range(29)],
    )
    st = track.stats(rows)
    assert st["risk"] is not None
    assert st["risk"]["max_dd"] == pytest.approx(-0.05, abs=1e-4)
    assert st["risk"]["sortino"] > 0
    assert st["risk"]["calmar"] > 0


def test_stats_risk_gate_below_sixty_days():
    st = track.stats(S(*[(f"2026-01-{d:02}", 0.001) for d in range(1, 8)]))
    assert st["risk"] is None


def test_drawdown_ath_fields():
    dd = track.drawdown(S(
        ("2026-01-05", 0.05), ("2026-01-06", -0.10), ("2026-01-16", 0.01),
    ))
    assert dd["ath_date"] == "2026-01-05"
    assert dd["days_underwater"] == 11


def test_drawdown_at_peak_is_not_underwater():
    dd = track.drawdown(S(("2026-01-05", 0.01), ("2026-01-06", 0.02)))
    assert dd["days_underwater"] == 0
