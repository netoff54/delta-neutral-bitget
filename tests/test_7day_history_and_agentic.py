import pytest
import tempfile
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from core.historical_store import HistoricalStore
from core.models import TakerSnapshot, Opportunity, FeeBreakdown, HistoricalFundingStats
from analytics.funding_history_analyzer import FundingHistoryAnalyzer
from core.gemini_brain import GeminiBrain

@pytest.fixture
def temp_store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = HistoricalStore(db_path=db_path)
    yield store
    try:
        os.remove(db_path)
    except Exception:
        pass

def test_historical_store_crud_and_pruning(temp_store):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    history_data = [
        {"fundingRate": 0.0005, "fundingTimestamp": now_ms - (3600 * 8 * 1000 * 2)}, # 16h lalu
        {"fundingRate": 0.0006, "fundingTimestamp": now_ms - (3600 * 8 * 1000 * 1)}, # 8h lalu
        {"fundingRate": 0.0004, "fundingTimestamp": now_ms},                          # sekarang
        {"fundingRate": 0.0002, "fundingTimestamp": now_ms - (86400 * 10 * 1000)},    # 10 HARI LALU (kedaluwarsa)
    ]

    inserted = temp_store.record_funding_rates("BTC/USDT:USDT", history_data, interval_hours=8)
    assert inserted == 4

    # Idempotency check: insert lagi data yang sama
    inserted_again = temp_store.record_funding_rates("BTC/USDT:USDT", history_data, interval_hours=8)
    assert inserted_again == 4

    # Ambil data 7 hari: yang 10 hari lalu tidak boleh masuk
    res_7d = temp_store.get_funding_history("BTC/USDT:USDT", days=7)
    assert len(res_7d) == 3
    assert res_7d[-1]["funding_rate"] == 0.0004

    # Snapshot Taker
    snap = TakerSnapshot(
        base_asset="BTC",
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT:USDT",
        spot_price=65000.0,
        perp_price=65050.0,
        basis_spread_percent=0.0769,
        current_funding_rate=0.0004,
        predicted_next_rate=0.00045,
        funding_interval_hours=8,
        spot_taker_fee_pct=0.10,
        perp_taker_fee_pct=0.06,
        round_trip_fee_pct=0.32,
        break_even_cycles=8,
        break_even_hours=64.0,
        composite_score=88.5,
        is_eligible=True,
        rejection_reason=None
    )
    temp_store.record_taker_snapshot(snap)

    # AGI Experiential Learning & Pair Reputation
    temp_store.record_agi_experience(
        event_type="HARVEST",
        base_asset="BTC",
        funding_rate=0.0005,
        harvest_usdt=0.025,
        net_pnl_usdt=0.012,
        holding_hours=24.0,
        was_bep_reached=True,
        ai_decision="HOLD_PANEN",
        lesson_learned="Yield konsisten positif",
        pair_reputation_score=0.1
    )

    rep = temp_store.get_pair_reputation("BTC")
    assert rep["base_asset"] == "BTC"
    assert rep["successful_harvests"] == 1
    assert rep["reputation_score"] >= 0.6
    assert rep["total_harvest_usdt"] == 0.025

    # Uji Prune Data > 7 hari
    prune_res = temp_store.prune_older_than_days(days=7)
    assert prune_res["pruned_funding_records"] == 1 # 1 record yang 10 hari lalu terhapus

def test_funding_history_analyzer_math():
    analyzer = FundingHistoryAnalyzer()

    # 21 siklus (7 hari @ 8h interval)
    rates = [0.0005] * 10 + [0.0007] * 11 # Total yield = 0.005 + 0.0077 = 0.0127 (1.27%)
    records = [{"funding_rate": r} for r in rates]

    stats = analyzer.analyze_7d_history(
        symbol="ETH/USDT:USDT",
        records=records,
        spot_taker_fee_pct=0.10,
        perp_taker_fee_pct=0.06,
        funding_interval_hours=8,
        basis_spread_percent=0.05
    )

    assert stats.seven_day_cumulative_yield_pct == 1.27
    assert stats.positive_consistency_pct == 100.0
    assert stats.negative_flip_count == 0
    assert stats.decay_trend == "ACCELERATING" # 0.0007 > 0.0005 * 1.15
    assert stats.taker_fee_recovery_cycles > 0
    assert stats.historical_quality_score > 60.0

def test_funding_history_analyzer_with_negative_flips():
    analyzer = FundingHistoryAnalyzer()

    # 21 siklus dengan beberapa flip negatif
    rates = [0.0005] * 15 + [-0.0002, -0.0001, 0.0001] * 2
    records = [{"funding_rate": r} for r in rates]

    stats = analyzer.analyze_7d_history(
        symbol="VOL/USDT:USDT",
        records=records,
        spot_taker_fee_pct=0.10,
        perp_taker_fee_pct=0.06,
        funding_interval_hours=8,
        basis_spread_percent=0.0
    )

    assert stats.negative_flip_count == 4
    assert stats.positive_consistency_pct < 100.0
    assert stats.decay_trend == "DECAYING"

@pytest.mark.asyncio
async def test_gemini_brain_taker_selection_with_7d_stats():
    brain = GeminiBrain(api_key=None)

    fee_bd = FeeBreakdown(
        spot_entry_fee=0.05,
        perp_entry_fee=0.03,
        spot_exit_fee=0.05,
        perp_exit_fee=0.03,
        slippage_buffer=0.02,
        total_round_trip_fee=0.18,
        total_fee_percent=0.36
    )

    stats_safe = HistoricalFundingStats(
        seven_day_cumulative_yield_pct=2.1,
        seven_day_apy_pct=109.5,
        positive_consistency_pct=100.0,
        negative_flip_count=0,
        rate_std_dev=0.01,
        decay_trend="ACCELERATING",
        taker_fee_recovery_cycles=4,
        taker_fee_recovery_hours=32.0,
        historical_quality_score=92.0,
        sample_count=21
    )

    opp1 = Opportunity(
        base_asset="BTC",
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT:USDT",
        spot_price=65000.0,
        perp_price=65030.0,
        basis_spread_percent=0.046,
        current_funding_rate=0.0005,
        funding_interval_hours=8,
        spot_volume_24h=10000000.0,
        perp_volume_24h=20000000.0,
        fee_breakdown=fee_bd,
        gross_cycle_yield_percent=0.05,
        gross_apy_percent=54.75,
        net_apy_percent=45.0,
        predicted_next_funding_rate=0.0005,
        historical_mean_rate=0.0005,
        historical_std_rate=0.0001,
        consistency_score_percent=100.0,
        composite_performance_score=90.0,
        break_even_cycles=8,
        break_even_hours=64.0,
        is_eligible=True,
        historical_7d_stats=stats_safe
    )

    opp2 = Opportunity(
        base_asset="TRAP",
        spot_symbol="TRAP/USDT",
        perp_symbol="TRAP/USDT:USDT",
        spot_price=10.0,
        perp_price=10.01,
        basis_spread_percent=0.1,
        current_funding_rate=0.0010,
        funding_interval_hours=8,
        spot_volume_24h=5000000.0,
        perp_volume_24h=10000000.0,
        fee_breakdown=fee_bd,
        gross_cycle_yield_percent=0.10,
        gross_apy_percent=109.5,
        net_apy_percent=95.0,
        predicted_next_funding_rate=0.0010,
        historical_mean_rate=0.0002,
        historical_std_rate=0.0008,
        consistency_score_percent=60.0,
        composite_performance_score=50.0,
        break_even_cycles=4,
        break_even_hours=32.0,
        is_eligible=True,
        historical_7d_stats=None
    )

    # Mock call_gemini untuk memilih koin yang aman
    with patch.object(brain, "call_gemini", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "PILIH: BTC karena rekam jejak 7 hari zero flip dan bebas risiko pembalikan rate."
        chosen = await brain.select_optimal_taker_agi([opp1, opp2], capital_usdt=50.0)
        assert chosen.base_asset == "BTC"
