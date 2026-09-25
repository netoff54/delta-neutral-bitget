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
from config.settings import settings

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

def test_120day_history_analyzer_math():
    analyzer = FundingHistoryAnalyzer()

    # 360 siklus (120 hari / 4 bulan @ 8h interval)
    rates = [0.0004] * 180 + [0.0006] * 180  # Rata-rata 0.0005 per 8 jam (0.15% per hari)
    records = [{"funding_rate": r} for r in rates]

    stats = analyzer.analyze_history_deep(
        symbol="SOL/USDT:USDT",
        records=records,
        spot_taker_fee_pct=0.10,
        perp_taker_fee_pct=0.06,
        funding_interval_hours=8,
        basis_spread_percent=0.04
    )

    # 360 siklus * rata-rata: total yield = (180*0.0004 + 180*0.0006) = 0.18 = 18.0%
    assert round(stats.one_twenty_day_cumulative_yield_pct, 1) == 18.0
    assert stats.one_twenty_day_flip_count == 0
    assert stats.positive_consistency_pct == 100.0
    assert stats.decay_trend == "ACCELERATING"
    assert stats.sixty_day_cumulative_yield_pct > 0.0
    assert stats.thirty_day_cumulative_yield_pct > 0.0
    assert stats.seven_day_cumulative_yield_pct > 0.0
    assert stats.historical_quality_score > 70.0

def test_120day_negative_flip_detection():
    analyzer = FundingHistoryAnalyzer()

    # 360 siklus dengan 11 flip negatif
    rates = [0.0004] * 320 + [-0.0002] * 11 + [0.0003] * 29
    records = [{"funding_rate": r} for r in rates]

    stats = analyzer.analyze_history_deep(
        symbol="FLIP/USDT:USDT",
        records=records,
        funding_interval_hours=8
    )

    assert stats.one_twenty_day_flip_count == 11
    assert stats.negative_flip_count == 11
    assert stats.positive_consistency_pct < 100.0

def test_database_and_store_120day_lifecycle(temp_db):
    db_inst, store = temp_db
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Tambahkan data 120 hari ke belakang dan 130 hari ke belakang
    history_data = [
        {"fundingRate": 0.0005, "fundingTimestamp": now_ms - (86400 * 30 * 1000)},   # 30 hari lalu
        {"fundingRate": 0.0006, "fundingTimestamp": now_ms - (86400 * 90 * 1000)},   # 90 hari lalu
        {"fundingRate": 0.0007, "fundingTimestamp": now_ms - (86400 * 115 * 1000)},  # 115 hari lalu (dalam 120 hari)
        {"fundingRate": 0.0003, "fundingTimestamp": now_ms - (86400 * 135 * 1000)},  # 135 hari lalu (lebih dari 120 hari)
    ]

    inserted = store.record_funding_rates("ETH/USDT:USDT", history_data, interval_hours=8)
    assert inserted == 4

    # Ambil data 120 hari: hanya 3 yang lolos, yang 135 hari lalu tidak masuk
    res_120d = store.get_funding_history("ETH/USDT:USDT", days=120)
    assert len(res_120d) == 3

    # Prune yang lebih dari 120 hari
    prune_res = store.prune_older_than_days(days=120)
    assert prune_res["pruned_funding_records"] == 1

def test_database_multi_timeframe_includes_4m(temp_db):
    db_inst, _ = temp_db
    now = datetime.now(timezone.utc)

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
    assert "4M" in pnl_timeframes
    assert "2M" in pnl_timeframes
    assert "1M" in pnl_timeframes
    assert "1W" in pnl_timeframes
    assert "1D" in pnl_timeframes
    assert "1Y" in pnl_timeframes
    assert pnl_timeframes["4M"]["timeframe_days"] == 120

def test_volume_threshold_100k():
    assert settings.MIN_24H_VOLUME_USDT == 100000.0

@pytest.mark.asyncio
async def test_gemini_brain_uses_120day_stats():
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

    stats_120d = HistoricalFundingStats(
        one_twenty_day_cumulative_yield_pct=15.5,
        one_twenty_day_apy_pct=47.1,
        one_twenty_day_flip_count=0,
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
        historical_quality_score=96.0,
        sample_count=360
    )

    opp = Opportunity(
        base_asset="SOLIDCOIN",
        spot_symbol="SOLIDCOIN/USDT",
        perp_symbol="SOLIDCOIN/USDT:USDT",
        spot_price=100.0,
        perp_price=100.05,
        basis_spread_percent=0.05,
        current_funding_rate=0.0004,
        funding_interval_hours=8,
        spot_volume_24h=150000.0,  # $150k > $100k
        perp_volume_24h=200000.0,  # $200k > $100k
        fee_breakdown=fee_bd,
        gross_cycle_yield_percent=0.04,
        gross_apy_percent=43.8,
        net_apy_percent=38.0,
        predicted_next_funding_rate=0.0004,
        historical_mean_rate=0.0004,
        historical_std_rate=0.00005,
        consistency_score_percent=100.0,
        composite_performance_score=94.0,
        break_even_cycles=6,
        break_even_hours=48.0,
        is_eligible=True,
        historical_120d_stats=stats_120d
    )

    with patch.object(brain, "call_gemini", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "PILIH: SOLIDCOIN karena performa 120 hari (4 bulan) tanpa flip dan likuiditas di atas $100k."
        chosen = await brain.select_optimal_taker_agi([opp], capital_usdt=100.0)
        assert chosen.base_asset == "SOLIDCOIN"

def test_120day_consistency_exact_match():
    """Memastikan konsistensi ditarik dari seluruh horizon 120 hari / 4 bulan secara presisi (contoh: 2 flip dari 720 siklus = 99.7%)."""
    from analytics.funding_history_analyzer import funding_history_analyzer

    # 720 siklus (120 hari @ 4h interval): 2 siklus negatif, 718 siklus positif
    rates = [0.0005] * 350 + [-0.0001] + [0.0005] * 200 + [-0.0002] + [0.0005] * 168
    records = [{"funding_rate": r} for r in rates]

    stats = funding_history_analyzer.analyze_history_deep(
        symbol="PONKE/USDT:USDT",
        records=records,
        spot_taker_fee_pct=0.10,
        perp_taker_fee_pct=0.06,
        funding_interval_hours=4,
        basis_spread_percent=0.03
    )

    assert stats.negative_flip_count == 2
    assert stats.positive_consistency_pct == 99.7
    assert stats.one_twenty_day_flip_count == 2

def test_manipulative_vs_stable_funding_detection():
    """Memastikan sistem mampu membedakan koin dengan funding manipulatif (spike liar / banyak flip) vs stabil otentik."""
    from analytics.funding_history_analyzer import funding_history_analyzer

    # 1. Koin Stabil Otentik: 360 siklus konsisten ~0.04% per 8h tanpa spike dan 0 flip
    stable_rates = [0.0004] * 360
    stable_stats = funding_history_analyzer.analyze_history_deep(
        symbol="BTC/USDT:USDT",
        records=[{"funding_rate": r} for r in stable_rates],
        funding_interval_hours=8
    )
    assert stable_stats.stability_diagnosis == "STABLE_AUTHENTIC"
    assert stable_stats.manipulation_risk_score < 20.0
    assert stable_stats.spike_count == 0

    # 2. Koin Manipulatif: Banyak lonjakan liar (spikes 10x) lalu collapse dan beberapa kali flip negatif
    manip_rates = [0.0001] * 50 + [0.0035] + [0.0001] * 50 + [0.0040] + [-0.0005] * 3 + [0.0001] * 50 + [0.0050] + [-0.0002] * 2 + [0.0001] * 100
    manip_stats = funding_history_analyzer.analyze_history_deep(
        symbol="PUMPCOIN/USDT:USDT",
        records=[{"funding_rate": r} for r in manip_rates],
        funding_interval_hours=8
    )
    assert manip_stats.stability_diagnosis == "MANIPULATIVE_VOLATILE"
    assert manip_stats.manipulation_risk_score >= 45.0
    assert manip_stats.spike_count >= 2

@pytest.mark.asyncio
async def test_rotation_learning_persisted_to_database(temp_db):
    """Memastikan setiap rotasi delta-neutral menyimpan bunga riil dan analisis kausal AGI ke database."""
    db_inst, store = temp_db

    # Simpan pengalaman rotasi dengan bunga real dan alasan kausal
    store.record_agi_experience(
        event_type="ROTATION_LEARNING",
        base_asset="ARX",
        funding_rate=0.0005,
        harvest_usdt=2.45,
        net_pnl_usdt=1.85,
        holding_hours=96.0,
        was_bep_reached=True,
        ai_decision="ROTATE_ARX_TO_PONKE",
        lesson_learned="ARX menghasilkan yield 2.45 USDT dalam 96h karena funding rate positif stabil.",
        tactical_rule="Prioritaskan koin 1h/4h interval STABLE_AUTHENTIC untuk mempercepat compounding.",
        pair_reputation_score=0.25,
        context_snapshot={"rotated_from": "ARX", "rotated_to": "PONKE", "effective_realized_apy": 45.2}
    )

    # Verifikasi data tersimpan di agi_experience_memory
    memories = db_inst.get_agentic_memory(limit=10)
    assert len(memories) >= 1
    rot_mem = memories[0]
    assert rot_mem["event_type"] == "ROTATION_LEARNING"
    assert rot_mem["base_asset"] == "ARX"
    assert rot_mem["harvest_usdt"] == 2.45
    assert "ARX menghasilkan yield" in rot_mem["lesson_learned"]
    assert "Prioritaskan koin" in rot_mem["tactical_rule"]


