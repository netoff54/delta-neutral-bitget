import pytest
import tempfile
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from core.historical_store import HistoricalStore
from core.database import UnifiedDatabase
from core.models import TakerSnapshot, Opportunity, FeeBreakdown, HistoricalFundingStats
from analytics.funding_history_analyzer import FundingHistoryAnalyzer, funding_history_analyzer
from core.gemini_brain import GeminiBrain

@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db_inst = UnifiedDatabase(sqlite_path=db_path)
    store = HistoricalStore(db_instance=db_inst)
    yield db_inst, store
    try:
        os.remove(db_path)
    except Exception:
        pass

def test_60day_history_analyzer_math():
    analyzer = FundingHistoryAnalyzer()

    # 180 siklus (60 hari @ 8h interval)
    rates = [0.0004] * 90 + [0.0006] * 90  # Rata-rata 0.0005 per 8 jam (0.15% per hari)
    records = [{"funding_rate": r} for r in rates]

    stats = analyzer.analyze_history_deep(
        symbol="SOL/USDT:USDT",
        records=records,
        spot_taker_fee_pct=0.10,
        perp_taker_fee_pct=0.06,
        funding_interval_hours=8,
        basis_spread_percent=0.04
    )

    # 180 siklus * rata-rata: total yield = (90*0.0004 + 90*0.0006) = 0.09 = 9.0%
    assert round(stats.sixty_day_cumulative_yield_pct, 1) == 9.0
    assert stats.sixty_day_flip_count == 0
    assert stats.positive_consistency_pct == 100.0
    assert stats.decay_trend == "ACCELERATING"
    assert stats.thirty_day_cumulative_yield_pct > 0.0
    assert stats.seven_day_cumulative_yield_pct > 0.0
    assert stats.historical_quality_score > 70.0

def test_60day_negative_flip_detection():
    analyzer = FundingHistoryAnalyzer()

    # 180 siklus dengan 7 flip negatif
    rates = [0.0004] * 160 + [-0.0002] * 7 + [0.0003] * 13
    records = [{"funding_rate": r} for r in rates]

    stats = analyzer.analyze_history_deep(
        symbol="FLIP/USDT:USDT",
        records=records,
        funding_interval_hours=8
    )

    assert stats.sixty_day_flip_count == 7
    assert stats.negative_flip_count == 7
    assert stats.positive_consistency_pct < 100.0

def test_database_and_store_60day_lifecycle(temp_db):
    db_inst, store = temp_db
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Tambahkan data 60 hari ke belakang dan 70 hari ke belakang
    history_data = [
        {"fundingRate": 0.0005, "fundingTimestamp": now_ms - (86400 * 20 * 1000)},  # 20 hari lalu
        {"fundingRate": 0.0006, "fundingTimestamp": now_ms - (86400 * 45 * 1000)},  # 45 hari lalu
        {"fundingRate": 0.0007, "fundingTimestamp": now_ms - (86400 * 55 * 1000)},  # 55 hari lalu (dalam 60 hari)
        {"fundingRate": 0.0003, "fundingTimestamp": now_ms - (86400 * 75 * 1000)},  # 75 hari lalu (lebih dari 60 hari)
    ]

    inserted = store.record_funding_rates("ETH/USDT:USDT", history_data, interval_hours=8)
    assert inserted == 4

    # Ambil data 60 hari: hanya 3 yang lolos, yang 75 hari lalu tidak masuk
    res_60d = store.get_funding_history("ETH/USDT:USDT", days=60)
    assert len(res_60d) == 3

    # Prune yang lebih dari 60 hari
    prune_res = store.prune_older_than_days(days=60)
    assert prune_res["pruned_funding_records"] == 1

def test_database_multi_timeframe_includes_2m(temp_db):
    db_inst, _ = temp_db
    now = datetime.now(timezone.utc)

    # Simpan riwayat saldo gabungan
    balance_payload = {
        "timestamp_utc": now.isoformat(),
        "total_combined_equity_usdt": 100.0,
        "spot": {"equity_usdt": 50.0, "free_usdt": 50.0},
        "futures": {"equity_usdt": 50.0, "free_usdt": 50.0, "margin_used_usdt": 0.0, "unrealized_pnl_usdt": 0.0},
        "otc": {"equity_usdt": 0.0},
        "earn": {"equity_usdt": 0.0}
    }
    db_inst.record_combined_balance(balance_payload, active_positions_count=0)

    pnl_timeframes = db_inst.get_all_pnl_timeframes()
    assert "2M" in pnl_timeframes
    assert "1M" in pnl_timeframes
    assert "1W" in pnl_timeframes
    assert "1D" in pnl_timeframes
    assert "1Y" in pnl_timeframes
    assert pnl_timeframes["2M"]["timeframe_days"] == 60

@pytest.mark.asyncio
async def test_gemini_brain_uses_60day_stats():
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

    stats_60d = HistoricalFundingStats(
        sixty_day_cumulative_yield_pct=7.5,
        sixty_day_apy_pct=45.6,
        sixty_day_flip_count=0,
        thirty_day_cumulative_yield_pct=3.8,
        thirty_day_apy_pct=46.2,
        thirty_day_flip_count=0,
        seven_day_cumulative_yield_pct=0.9,
        seven_day_apy_pct=46.9,
        positive_consistency_pct=100.0,
        negative_flip_count=0,
        rate_std_dev=0.005,
        decay_trend="STABLE",
        taker_fee_recovery_cycles=3,
        taker_fee_recovery_hours=24.0,
        historical_quality_score=95.0,
        sample_count=180
    )

    opp = Opportunity(
        base_asset="SAFECOIN",
        spot_symbol="SAFECOIN/USDT",
        perp_symbol="SAFECOIN/USDT:USDT",
        spot_price=100.0,
        perp_price=100.05,
        basis_spread_percent=0.05,
        current_funding_rate=0.0004,
        funding_interval_hours=8,
        spot_volume_24h=5000000.0,
        perp_volume_24h=10000000.0,
        fee_breakdown=fee_bd,
        gross_cycle_yield_percent=0.04,
        gross_apy_percent=43.8,
        net_apy_percent=38.0,
        predicted_next_funding_rate=0.0004,
        historical_mean_rate=0.0004,
        historical_std_rate=0.00005,
        consistency_score_percent=100.0,
        composite_performance_score=92.0,
        break_even_cycles=6,
        break_even_hours=48.0,
        is_eligible=True,
        historical_60d_stats=stats_60d
    )

    with patch.object(brain, "call_gemini", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "PILIH: SAFECOIN karena performa 60 hari 0 flip dan konsistensi stabil."
        chosen = await brain.select_optimal_taker_agi([opp], capital_usdt=100.0)
        assert chosen.base_asset == "SAFECOIN"
