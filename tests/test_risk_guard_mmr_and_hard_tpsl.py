import pytest
from unittest.mock import AsyncMock, MagicMock
from core.models import DeltaNeutralPosition, PositionLeg
from risk.margin_guard import MarginGuard
from config.settings import settings

@pytest.fixture
def mock_setup():
    mock_client = MagicMock()
    mock_pos_mgr = MagicMock()
    mock_executor = MagicMock()
    mock_executor.close_delta_neutral_position = AsyncMock(return_value=True)

    pos = DeltaNeutralPosition(
        position_id="test_pos_arx",
        base_asset="ARX",
        spot_leg=PositionLeg(
            market_type="spot",
            symbol="ARX/USDT",
            side="buy",
            amount=100.0,
            entry_price=0.20,
            current_price=0.20,
            nominal_usdt=20.0,
            fee_paid=0.02
        ),
        perp_leg=PositionLeg(
            market_type="perp",
            symbol="ARX/USDT:USDT",
            side="sell",
            amount=100.0,
            entry_price=0.20,
            current_price=0.20,
            nominal_usdt=20.0,
            fee_paid=0.02
        ),
        leverage=1,
        net_delta=0.0,
        status="OPEN"
    )

    mock_pos_mgr.get_active_positions.return_value = [pos]
    return mock_client, mock_pos_mgr, mock_executor, pos

@pytest.mark.asyncio
async def test_mmr_exceeds_90_pct_triggers_emergency_close(mock_setup):
    mock_client, mock_pos_mgr, mock_executor, pos = mock_setup

    mock_client.fetch_tickers_by_type = AsyncMock(side_effect=lambda mtype: (
        {"ARX/USDT:USDT": {"last": 0.25, "ask": 0.25}} if mtype == "swap"
        else {"ARX/USDT": {"last": 0.25, "bid": 0.25}}
    ))

    # Bitget returns real position with MMR 0.92 (92%)
    mock_client.fetch_positions = AsyncMock(return_value=[{
        "symbol": "ARX/USDT:USDT",
        "marginRatio": 0.92,
        "liquidationPrice": 0.26
    }])

    guard = MarginGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=mock_executor)
    await guard.check_positions_health()

    assert pos.current_margin_ratio == 0.92
    mock_executor.close_delta_neutral_position.assert_awaited_once_with(
        position_id="test_pos_arx",
        reason="MMR Kritis Bitget (92.0% >= 90%)"
    )

@pytest.mark.asyncio
async def test_hard_take_profit_triggered_with_positive_spread(mock_setup):
    mock_client, mock_pos_mgr, mock_executor, pos = mock_setup

    # Positive spread: spot bid $0.21 >= perp ask $0.20
    mock_client.fetch_tickers_by_type = AsyncMock(side_effect=lambda mtype: (
        {"ARX/USDT:USDT": {"last": 0.20, "ask": 0.20}} if mtype == "swap"
        else {"ARX/USDT": {"last": 0.21, "bid": 0.21}}
    ))

    mock_client.fetch_positions = AsyncMock(return_value=[{
        "symbol": "ARX/USDT:USDT",
        "marginRatio": 0.05,
        "liquidationPrice": 0.40
    }])

    # Capital = 20 USDT, 5% TP = +1.0 USDT
    pos.is_bep_reached = True
    pos.net_pnl_usdt = 1.50 # +1.50 USDT (> 5%)

    guard = MarginGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=mock_executor)
    await guard.check_positions_health()

    mock_executor.close_delta_neutral_position.assert_awaited_once()
    args, kwargs = mock_executor.close_delta_neutral_position.call_args
    assert "HARD_TAKE_PROFIT" in kwargs.get("reason", "")

@pytest.mark.asyncio
async def test_hard_stop_loss_triggered_on_extreme_loss(mock_setup):
    mock_client, mock_pos_mgr, mock_executor, pos = mock_setup

    mock_client.fetch_tickers_by_type = AsyncMock(side_effect=lambda mtype: (
        {"ARX/USDT:USDT": {"last": 0.20, "ask": 0.20}} if mtype == "swap"
        else {"ARX/USDT": {"last": 0.20, "bid": 0.20}}
    ))

    mock_client.fetch_positions = AsyncMock(return_value=[{
        "symbol": "ARX/USDT:USDT",
        "marginRatio": 0.10,
        "liquidationPrice": 0.40
    }])

    # Extreme net loss: -1.50 USDT (<= -5% of 20 USDT = -1.0)
    pos.net_pnl_usdt = -1.50

    guard = MarginGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=mock_executor)
    await guard.check_positions_health()

    mock_executor.close_delta_neutral_position.assert_awaited_once()
    args, kwargs = mock_executor.close_delta_neutral_position.call_args
    assert "HARD_STOP_LOSS" in kwargs.get("reason", "")
