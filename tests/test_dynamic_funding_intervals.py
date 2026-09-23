import pytest
import math
from datetime import datetime, timezone, timedelta
from utils.interval_helper import (
    parse_interval_hours,
    detect_empirical_interval,
    cycles_per_day,
    calculate_apy
)
from analytics.fee_calculator import FeeCalculator
from analytics.funding_history_analyzer import FundingHistoryAnalyzer
from analytics.performance_scorer import PerformanceScorer

def test_parse_interval_hours():
    # Integer tests
    assert parse_interval_hours(1) == 1
    assert parse_interval_hours(4) == 4
    assert parse_interval_hours(8) == 8
    assert parse_interval_hours(2) == 2

    # String tests
    assert parse_interval_hours("1") == 1
    assert parse_interval_hours("4") == 4
    assert parse_interval_hours("8") == 8
    assert parse_interval_hours("1h") == 1
    assert parse_interval_hours("4h") == 4
    assert parse_interval_hours("8h") == 8
    assert parse_interval_hours("1 hour") == 1
    assert parse_interval_hours("4 hours") == 4
    assert parse_interval_hours("8 jam") == 8

    # Edge cases & fallbacks
    assert parse_interval_hours(None, default=8) == 8
    assert parse_interval_hours("", default=8) == 8
    assert parse_interval_hours("invalid", default=8) == 8

def test_detect_empirical_interval():
    base_ts = 1710000000000

    # 1h series (1 hour = 3600 * 1000 ms)
    ts_1h = [base_ts + (i * 3600 * 1000) for i in range(12)]
    assert detect_empirical_interval(ts_1h) == 1

    # 4h series (4 hours = 4 * 3600 * 1000 ms)
    ts_4h = [base_ts + (i * 4 * 3600 * 1000) for i in range(12)]
    assert detect_empirical_interval(ts_4h) == 4

    # 8h series (8 hours = 8 * 3600 * 1000 ms)
    ts_8h = [base_ts + (i * 8 * 3600 * 1000) for i in range(12)]
    assert detect_empirical_interval(ts_8h) == 8

def test_fee_calculator_dynamic_intervals():
    calc = FeeCalculator(
        spot_taker_fee=0.001,  # 0.10%
        spot_maker_fee=0.0008,
        perp_taker_fee=0.0006, # 0.06%
        perp_maker_fee=0.0002,
        slippage_buffer=0.0005 # 0.05%
    )
    # Total round trip taker fee = (0.001 + 0.0006) * 2 + 0.001 = 0.0042 (0.42%)

    rate = 0.0004 # 0.04% per funding settlement

    # 1. Evaluate with 1h interval (24 settlements/day)
    eval_1h = calc.evaluate_opportunity(
        funding_rate=rate,
        nominal_value_usdt=100.0,
        funding_interval_hours=1
    )
    # 0.04% * 24 * 365 = 350.4% Gross APY
    assert eval_1h["gross_apy_percent"] == 350.4
    # break_even_cycles = ceil(0.42% / 0.04%) = 11 cycles
    assert eval_1h["break_even_cycles"] == 11
    # break_even_hours = 11 * 1h = 11.0h
    assert eval_1h["break_even_hours"] == 11.0
    assert eval_1h["is_eligible"] is True

    # 2. Evaluate with 4h interval (6 settlements/day)
    eval_4h = calc.evaluate_opportunity(
        funding_rate=rate,
        nominal_value_usdt=100.0,
        funding_interval_hours=4
    )
    # 0.04% * 6 * 365 = 87.6% Gross APY
    assert eval_4h["gross_apy_percent"] == 87.6
    assert eval_4h["break_even_cycles"] == 11
    # break_even_hours = 11 * 4h = 44.0h
    assert eval_4h["break_even_hours"] == 44.0
    assert eval_4h["is_eligible"] is True

    # 3. Evaluate with 8h interval (3 settlements/day)
    eval_8h = calc.evaluate_opportunity(
        funding_rate=rate,
        nominal_value_usdt=100.0,
        funding_interval_hours=8
    )
    # 0.04% * 3 * 365 = 43.8% Gross APY
    assert eval_8h["gross_apy_percent"] == 43.8
    assert eval_8h["break_even_cycles"] == 11
    # break_even_hours = 11 * 8h = 88.0h (> 72h max limit -> rejected)
    assert eval_8h["break_even_hours"] == 88.0
    assert eval_8h["is_eligible"] is False
    assert "melebihi batas toleransi" in eval_8h["rejection_reason"]

def test_funding_history_analyzer_dynamic_intervals():
    analyzer = FundingHistoryAnalyzer()

    # 24 records @ 0.05% for 1h token = 1 full day of data
    records_1h = [{"funding_rate": 0.0005} for _ in range(24)]
    stats_1h = analyzer.analyze_history_deep(
        symbol="MEME1/USDT:USDT",
        records=records_1h,
        funding_interval_hours=1
    )
    # 24 settlements * 0.05% = 1.2% daily yield -> 1.2% * 365 = 438% APY
    assert stats_1h.thirty_day_cumulative_yield_pct == 1.2
    assert stats_1h.thirty_day_apy_pct == 438.0
    assert stats_1h.thirty_day_flip_count == 0

    # 6 records @ 0.05% for 4h token = 1 full day of data
    records_4h = [{"funding_rate": 0.0005} for _ in range(6)]
    stats_4h = analyzer.analyze_history_deep(
        symbol="MEME4/USDT:USDT",
        records=records_4h,
        funding_interval_hours=4
    )
    # 6 settlements * 0.05% = 0.3% daily yield -> 0.3% * 365 = 109.5% APY
    assert stats_4h.thirty_day_cumulative_yield_pct == 0.3
    assert stats_4h.thirty_day_apy_pct == 109.5

def test_performance_scorer_dynamic_intervals():
    scorer = PerformanceScorer()

    # Score for 1h vs 8h with same rate per settlement
    score_1h = scorer.calculate_composite_score(
        predicted_next_rate=0.0005,
        historical_mean=0.0005,
        consistency_pct=100.0,
        std_rate=0.0001,
        net_apy_percent=100.0,
        break_even_hours=12.0,
        funding_interval_hours=1
    )

    score_8h = scorer.calculate_composite_score(
        predicted_next_rate=0.0005,
        historical_mean=0.0005,
        consistency_pct=100.0,
        std_rate=0.0001,
        net_apy_percent=20.0,
        break_even_hours=48.0,
        funding_interval_hours=8
    )

    # 1h produces significantly higher annualized yield and faster break-even
    assert score_1h > score_8h
