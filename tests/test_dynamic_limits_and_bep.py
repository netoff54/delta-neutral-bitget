import pytest
import asyncio
from datetime import datetime, timedelta
from core.models import DeltaNeutralPosition, PositionLeg, Opportunity, FeeBreakdown
from core.bitget_client import BitgetClient
from execution.position_manager import PositionManager
from execution.rebalancer import AutoRebalancer

@pytest.mark.asyncio
async def test_min_order_cost_calculation():
    """Memastikan get_min_order_cost mendeteksi minimal 5 USDT futures dan 1 USDT spot."""
    client = BitgetClient()
    limits = client.get_min_order_cost("BTC/USDT", "BTC/USDT:USDT")
    assert limits["spot_cost_min"] >= 1.0
    assert limits["perp_cost_min"] >= 5.0
    # Minimal satu kaki harus >= max(spot_cost_min, perp_cost_min) = 5.0
    assert limits["required_leg_usdt"] >= 5.0
    # Total modal untuk 2x leverage = required_leg * 1.5 >= 7.5 USDT
    assert limits["required_min_capital_usdt"] >= 7.5

@pytest.mark.asyncio
async def test_bep_rotation_guard():
    """Memastikan posisi yang belum BEP (Net PnL <= 0) tidak dirotasi oleh rebalancer."""
    pos_mgr = PositionManager(data_file="scratch/test_positions.json")
    pos_mgr.positions.clear()
    pos_mgr.save_positions()

    entry_time = datetime.utcnow() - timedelta(hours=20) # Sudah 20 jam (> 16 jam threshold)
    spot_leg = PositionLeg(
        market_type="spot",
        symbol="BTW/USDT",
        side="buy",
        amount=50.0,
        entry_price=0.10,
        current_price=0.10,
        nominal_usdt=5.0,
        fee_paid=0.005
    )
    perp_leg = PositionLeg(
        market_type="perp",
        symbol="BTW/USDT:USDT",
        side="sell",
        amount=50.0,
        entry_price=0.10,
        current_price=0.10,
        nominal_usdt=5.0,
        fee_paid=0.003
    )
    pos = DeltaNeutralPosition(
        position_id="DN_BTW_test",
        base_asset="BTW",
        spot_leg=spot_leg,
        perp_leg=perp_leg,
        leverage=2,
        entry_time=entry_time,
        net_delta=0.0,
        total_fees_paid=0.008,
        realized_funding_usdt=0.001,
        cumulative_funding_received=0.001,
        unrealized_pnl_usdt=0.0,
        net_pnl_usdt=-0.015, # Defisit, belum BEP!
        is_bep_reached=False,
        status="OPEN"
    )
    pos_mgr.add_position(pos)

    fb = FeeBreakdown(
        spot_entry_fee=0.005,
        perp_entry_fee=0.003,
        spot_exit_fee=0.005,
        perp_exit_fee=0.003,
        slippage_buffer=0.002,
        total_round_trip_fee=0.018,
        total_fee_percent=0.22
    )

    new_opp = Opportunity(
        base_asset="ARX",
        spot_symbol="ARX/USDT",
        perp_symbol="ARX/USDT:USDT",
        spot_price=0.20,
        perp_price=0.20,
        basis_spread_percent=0.0,
        current_funding_rate=0.003, # 0.3%
        predicted_next_funding_rate=0.003,
        historical_funding_rates=[0.003, 0.003],
        funding_interval_hours=8,
        spot_volume_24h=1_000_000,
        perp_volume_24h=5_000_000,
        fee_breakdown=fb,
        gross_cycle_yield_percent=0.3,
        gross_apy_percent=328.5,
        net_apy_percent=150.0,
        break_even_cycles=1,
        break_even_hours=12.0,
        consistency_score_percent=95.0,
        funding_trend="STABLE",
        composite_performance_score=90.0,
        is_eligible=True,
        rejection_reason=None
    )

    rebalancer = AutoRebalancer(pos_mgr=pos_mgr)
    await rebalancer.evaluate_and_rotate([new_opp], capital_per_position=8.0)

    # Posisi BTW harus tetap OPEN karena belum BEP
    active = pos_mgr.get_active_positions()
    assert len(active) == 1
    assert active[0].base_asset == "BTW"
    assert active[0].status == "OPEN"

    pos_mgr.positions.clear()
    pos_mgr.save_positions()
