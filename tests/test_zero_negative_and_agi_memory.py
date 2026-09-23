import pytest
import asyncio
from datetime import datetime, timedelta
from core.models import DeltaNeutralPosition, PositionLeg, Opportunity, FeeBreakdown
from core.bitget_client import BitgetClient
from core.gemini_brain import GeminiBrain
from config.settings import settings

@pytest.mark.asyncio
async def test_zero_tolerance_threshold_setting():
    """Memastikan ambang batas emergency exit funding rate adalah 0.0 (Zero tolerance)."""
    assert settings.EMERGENCY_EXIT_FUNDING_RATE == 0.0

@pytest.mark.asyncio
async def test_dynamic_funding_projection_math():
    """Memastikan proyeksi dividen funding fee dihitung akurat untuk interval dinamis 4h dan 8h."""
    brain = GeminiBrain(api_key=None, memory_file="scratch/test_ai_memory.json")
    
    spot_leg = PositionLeg(market_type="spot", symbol="BTW/USDT", side="buy", amount=10.0, entry_price=1.0, current_price=1.0, nominal_usdt=10.0, fee_paid=0.01)
    perp_leg = PositionLeg(market_type="perp", symbol="BTW/USDT:USDT", side="sell", amount=10.0, entry_price=1.0, current_price=1.0, nominal_usdt=10.0, fee_paid=0.006)
    pos = DeltaNeutralPosition(position_id="test_proj", base_asset="BTW", spot_leg=spot_leg, perp_leg=perp_leg, leverage=2, net_delta=0.0, total_fees_paid=0.016)

    # 1. Test interval 4h dengan rate 0.05% (0.0005)
    proj_4h = brain.generate_funding_projection(pos, current_rate=0.0005, interval_hours=4)
    assert proj_4h["current_rate_percent"] == 0.05
    assert proj_4h["projected_next_payout_usdt"] == 0.005 # $10 * 0.0005
    assert proj_4h["projected_daily_usdt"] == 0.03 # 0.005 * 6 siklus
    assert proj_4h["is_rate_healthy"] is True

    # 2. Test interval 8h dengan rate 0.01% (0.0001)
    proj_8h = brain.generate_funding_projection(pos, current_rate=0.0001, interval_hours=8)
    assert proj_8h["current_rate_percent"] == 0.01
    assert proj_8h["projected_next_payout_usdt"] == 0.001
    assert proj_8h["projected_daily_usdt"] == 0.003

@pytest.mark.asyncio
async def test_agi_knowledge_context_accumulation():
    """Memastikan akumulasi memori masa lalu disajikan sebagai Few-Shot context untuk AGI."""
    brain = GeminiBrain(api_key=None, memory_file="scratch/test_ai_knowledge.json")
    brain.memory.clear()
    brain.save_memory()

    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "event_type": "HARVEST_LEARNING",
        "base_asset": "BTW",
        "current_funding_rate": 0.0002,
        "harvest_profit_usdt": 0.002,
        "net_pnl_usdt": 0.005,
        "lesson_learned": "Koin BTW konsisten menghasilkan yield positif."
    }
    brain.memory.append(entry)
    brain.save_memory()

    context = brain.get_accumulated_knowledge_context()
    assert "BTW" in context
    assert "Koin BTW konsisten menghasilkan yield positif." in context

    brain.memory.clear()
    brain.save_memory()
