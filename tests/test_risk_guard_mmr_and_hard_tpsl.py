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
async def test_mmr_exceeds_90_pct_triggers_cancellation(mock_setup):
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
    mock_executor.close_delta_neutral_position.assert_awaited_once()
    args, kwargs = mock_executor.close_delta_neutral_position.call_args
    assert "BATALKAN_DELTA_NEUTRAL" in kwargs.get("reason", "")

@pytest.mark.asyncio
async def test_futures_roe_minus_90_pct_triggers_cancellation(mock_setup):
    mock_client, mock_pos_mgr, mock_executor, pos = mock_setup

    mock_client.fetch_tickers_by_type = AsyncMock(side_effect=lambda mtype: (
        {"ARX/USDT:USDT": {"last": 0.38, "ask": 0.38}} if mtype == "swap"
        else {"ARX/USDT": {"last": 0.38, "bid": 0.38}}
    ))

    # Bitget returns real position with ROE -92.5%
    mock_client.fetch_positions = AsyncMock(return_value=[{
        "symbol": "ARX/USDT:USDT",
        "marginRatio": 0.15,
        "liquidationPrice": 0.45,
        "percentage": -92.5
    }])

    guard = MarginGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=mock_executor)
    await guard.check_positions_health()

    assert pos.current_roe_percent == -92.5
    mock_executor.close_delta_neutral_position.assert_awaited_once()
    args, kwargs = mock_executor.close_delta_neutral_position.call_args
    assert "BATALKAN_DELTA_NEUTRAL" in kwargs.get("reason", "")
    assert "90%" in kwargs.get("reason", "")

@pytest.mark.asyncio
async def test_no_artificial_tp_sl_delta_neutral_continues_holding(mock_setup):
    mock_client, mock_pos_mgr, mock_executor, pos = mock_setup

    # Positive spread: spot bid $0.20, perp ask $0.20
    mock_client.fetch_tickers_by_type = AsyncMock(side_effect=lambda mtype: (
        {"ARX/USDT:USDT": {"last": 0.20, "ask": 0.20}} if mtype == "swap"
        else {"ARX/USDT": {"last": 0.20, "bid": 0.20}}
    ))

    mock_client.fetch_positions = AsyncMock(return_value=[{
        "symbol": "ARX/USDT:USDT",
        "marginRatio": 0.05,
        "liquidationPrice": 0.40
    }])

    # Case 1: Net PnL is +1.50 USDT (> 5%). Posisi TIDAK boleh ditutup prematurely
    # karena delta neutral harus terus memanen funding tanpa interupsi artificial TP
    pos.is_bep_reached = True
    pos.net_pnl_usdt = 1.50
    pos.perp_leg.unrealized_pnl = -0.50

    guard = MarginGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=mock_executor)
    await guard.check_positions_health()

    mock_executor.close_delta_neutral_position.assert_not_called()

    # Case 2: Net PnL fluctuates down to -1.20 USDT (<= -5% of 20 USDT margin).
    # Namun kaki futures belum minus 90% (hanya -0.50). Posisi TETAP DIPERTAHANKAN (no artificial SL)!
    pos.net_pnl_usdt = -1.20
    await guard.check_positions_health()

    mock_executor.close_delta_neutral_position.assert_not_called()
