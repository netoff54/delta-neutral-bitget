import pytest
from datetime import datetime, timezone
from config.settings import settings
from core.models import DeltaNeutralPosition, PositionLeg, Opportunity
from core.gemini_brain import GeminiBrain

@pytest.mark.asyncio
async def test_quota_budget_configuration():
    """Memastikan kuota harian dialokasikan 720 panggilan (~48-50% dari limit 1.500 RPD) dan interval 120s."""
    assert settings.SCAN_INTERVAL_SECONDS == 120
    assert settings.AI_MAX_DAILY_CALLS == 720
    assert settings.AI_REALTIME_EVALUATION is True

@pytest.mark.asyncio
async def test_quota_tracking_and_cap():
    """Memastikan GeminiBrain menghitung kuota panggilan harian dan menahan jika batas 720 tercapai."""
    brain = GeminiBrain(api_key=None, memory_file="scratch/test_quota_mem.json")
    brain.daily_calls_count = 0
    brain.max_daily_calls = 720

    status = brain.get_quota_status()
    assert status["calls_today"] == 0
    assert status["max_daily_calls"] == 720
    assert status["official_rpd"] == 1500

    # Simulasikan panggilan tercapai
    brain.daily_calls_count = 720
    status_full = brain.get_quota_status()
    assert status_full["calls_today"] == 720
    assert status_full["budget_percent"] == 48.0

@pytest.mark.asyncio
async def test_ground_truth_evaluation_fallback_structure():
    """Memastikan evaluate_realtime_ground_truth menghasilkan struktur keputusan valid meskipun offline."""
    brain = GeminiBrain(api_key=None, memory_file="scratch/test_gt_mem.json")

    spot = PositionLeg(market_type="spot", symbol="BTW/USDT", side="buy", amount=10.0, entry_price=1.0, current_price=1.02, nominal_usdt=10.0, fee_paid=0.01)
    perp = PositionLeg(market_type="perp", symbol="BTW/USDT:USDT", side="sell", amount=10.0, entry_price=1.0, current_price=1.01, nominal_usdt=10.0, fee_paid=0.006)
    pos = DeltaNeutralPosition(position_id="test_pos", base_asset="BTW", spot_leg=spot, perp_leg=perp, leverage=2, net_delta=0.0, total_fees_paid=0.016)

    decision = await brain.evaluate_realtime_ground_truth(
        active_positions=[pos],
        total_balance_usdt=20.0,
        spot_free_usdt=5.0,
        swap_free_usdt=5.0,
        top_opportunities=[],
        market_countdown_minutes=12.5
    )

    assert "action" in decision
    assert "tactical_guidance" in decision
    assert "market_condition" in decision
    assert decision["action"] in ["HOLD_AND_HARVEST", "TIGHTEN_EXIT", "PROACTIVE_REBALANCE", "ROTATE", "EMERGENCY_UNWIND"]
