import pytest
import asyncio
from datetime import datetime, timedelta
from core.models import DeltaNeutralPosition, PositionLeg, Opportunity, FeeBreakdown
from core.bitget_client import BitgetClient
from core.gemini_brain import GeminiBrain

@pytest.mark.asyncio
async def test_gemini_brain_memory_persistence():
    """Memastikan GeminiBrain dapat menyimpan dan memuat riwayat pembelajaran ke disk."""
    brain = GeminiBrain(
        api_key="mock_key",
        model="gemini-3.6-flash",
        memory_file="scratch/test_ai_memory.json"
    )
    brain.memory.clear()
    brain.save_memory()

    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "event_type": "TEST",
        "insight": "Testing memory persistence"
    }
    brain.memory.append(entry)
    brain.save_memory()

    # Load kembali dari disk
    brain2 = GeminiBrain(
        api_key="mock_key",
        model="gemini-3.6-flash",
        memory_file="scratch/test_ai_memory.json"
    )
    assert len(brain2.memory) == 1
    assert brain2.memory[0]["insight"] == "Testing memory persistence"

    # Cleanup
    brain2.memory.clear()
    brain2.save_memory()

@pytest.mark.asyncio
async def test_presettlement_countdown_structure():
    """Memastikan countdown pra-settlement mengembalikan struktur data yang benar."""
    client = BitgetClient()
    res = await client.get_funding_settlement_countdown("BTC/USDT:USDT")
    assert "symbol" in res
    assert "current_projected_rate" in res
    assert "seconds_until_settlement" in res
    assert "minutes_until_settlement" in res
    assert "is_near_settlement" in res
    assert isinstance(res["is_near_settlement"], bool)

@pytest.mark.asyncio
async def test_presettlement_risk_evaluation():
    """Memastikan evaluate_pre_settlement_risk mendeteksi kondisi aman vs kritis."""
    brain = GeminiBrain(api_key=None, memory_file="scratch/test_ai_memory.json")
    
    spot_leg = PositionLeg(market_type="spot", symbol="BTC/USDT", side="buy", amount=0.01, entry_price=60000, current_price=60000, nominal_usdt=600, fee_paid=0.6)
    perp_leg = PositionLeg(market_type="perp", symbol="BTC/USDT:USDT", side="sell", amount=0.01, entry_price=60000, current_price=60000, nominal_usdt=600, fee_paid=0.36)
    pos = DeltaNeutralPosition(position_id="test", base_asset="BTC", spot_leg=spot_leg, perp_leg=perp_leg, leverage=2, net_delta=0.0, total_fees_paid=0.96)

    # 1. Rate positif aman
    eval_safe = await brain.evaluate_pre_settlement_risk(pos, projected_rate=0.0005, seconds_until_settlement=200)
    assert eval_safe["action"] == "SAFE"

    # 2. Rate negatif melampaui ambang batas darurat
    eval_risky = await brain.evaluate_pre_settlement_risk(pos, projected_rate=-0.0002, seconds_until_settlement=200)
    assert eval_risky["action"] == "EMERGENCY_EXIT"
