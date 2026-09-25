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
async def test_mmr_exceeds_85_pct_triggers_cancellation(mock_setup):
    mock_client, mock_pos_mgr, mock_executor, pos = mock_setup

    mock_client.fetch_tickers_by_type = AsyncMock(side_effect=lambda mtype: (
        {"ARX/USDT:USDT": {"last": 0.25, "ask": 0.25}} if mtype == "swap"
        else {"ARX/USDT": {"last": 0.25, "bid": 0.25}}
    ))

    # Bitget returns real position with MMR 0.86 (86% > 85%)
    mock_client.fetch_positions = AsyncMock(return_value=[{
        "symbol": "ARX/USDT:USDT",
        "marginRatio": 0.86,
        "liquidationPrice": 0.26
    }])

    guard = MarginGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=mock_executor)
    await guard.check_positions_health()

    assert pos.current_margin_ratio == 0.86
    mock_executor.close_delta_neutral_position.assert_awaited_once()
    args, kwargs = mock_executor.close_delta_neutral_position.call_args
    assert "BATALKAN_DELTA_NEUTRAL" in kwargs.get("reason", "")
    assert "85%" in kwargs.get("reason", "")

@pytest.mark.asyncio
async def test_futures_roe_minus_85_pct_triggers_cancellation(mock_setup):
    mock_client, mock_pos_mgr, mock_executor, pos = mock_setup

    mock_client.fetch_tickers_by_type = AsyncMock(side_effect=lambda mtype: (
        {"ARX/USDT:USDT": {"last": 0.38, "ask": 0.38}} if mtype == "swap"
        else {"ARX/USDT": {"last": 0.38, "bid": 0.38}}
    ))

    # Bitget returns real position with ROE -86.5% (< -85.0%)
    mock_client.fetch_positions = AsyncMock(return_value=[{
        "symbol": "ARX/USDT:USDT",
        "marginRatio": 0.15,
        "liquidationPrice": 0.45,
        "percentage": -86.5
    }])

    guard = MarginGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=mock_executor)
    await guard.check_positions_health()

    assert pos.current_roe_percent == -86.5
    mock_executor.close_delta_neutral_position.assert_awaited_once()
    args, kwargs = mock_executor.close_delta_neutral_position.call_args
    assert "BATALKAN_DELTA_NEUTRAL" in kwargs.get("reason", "")
    assert "85%" in kwargs.get("reason", "")

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
    # Namun kaki futures belum minus 85% (hanya -0.50). Posisi TETAP DIPERTAHANKAN (no artificial SL)!
    pos.net_pnl_usdt = -1.20
    await guard.check_positions_health()

    mock_executor.close_delta_neutral_position.assert_not_called()

@pytest.mark.asyncio
async def test_close_position_executes_maker_limit_orders_with_positive_spread(mock_setup):
    """
    Memverifikasi bahwa eksekusi close_delta_neutral_position:
    1. Menggunakan order_type='limit' dan post_only=True (Market Maker)
    2. Menjamin basis spread positif (Spot Sell Price >= Futures Buy Price)
    """
    mock_client, mock_pos_mgr, _, pos = mock_setup
    from execution.order_executor import OrderExecutor
    from core.models import OrderExecutionResult

    mock_pos_mgr.positions = {pos.position_id: pos}

    # Mock ticker live: Spot Bid 0.201, Spot Ask 0.202, Perp Bid 0.200, Perp Ask 0.201
    mock_client.client.fetch_ticker = AsyncMock(side_effect=lambda sym: (
        {"bid": 0.201, "ask": 0.202, "last": 0.2015} if "ARX/USDT" in sym and ":USDT" not in sym
        else {"bid": 0.200, "ask": 0.201, "last": 0.2005}
    ))
    mock_client.client.fetch_balance = AsyncMock(return_value={"free": {"ARX": 100.0}})

    mock_client.execute_perp_order = AsyncMock(return_value=OrderExecutionResult(
        success=True, market_type="perp", symbol="ARX/USDT:USDT", side="buy",
        requested_amount=100.0, filled_amount=100.0, avg_price=0.200, fee_amount=0.004, order_id="ord_p_1"
    ))
    mock_client.execute_spot_order = AsyncMock(return_value=OrderExecutionResult(
        success=True, market_type="spot", symbol="ARX/USDT", side="sell",
        requested_amount=100.0, filled_amount=100.0, avg_price=0.202, fee_amount=0.0, order_id="ord_s_1"
    ))

    executor = OrderExecutor(client=mock_client, pos_mgr=mock_pos_mgr)
    success = await executor.close_delta_neutral_position(pos.position_id, reason="TEST_MAKER_CLOSE")

    assert success is True
    # Verifikasi eksekusi futures order: Limit Maker (post_only=True)
    mock_client.execute_perp_order.assert_awaited_once()
    _, perp_kwargs = mock_client.execute_perp_order.call_args
    assert perp_kwargs["order_type"] == "limit"
    assert perp_kwargs["post_only"] is True
    assert perp_kwargs["reduce_only"] is True

    # Verifikasi eksekusi spot order: Limit Maker (post_only=True)
    mock_client.execute_spot_order.assert_awaited_once()
    _, spot_kwargs = mock_client.execute_spot_order.call_args
    assert spot_kwargs["order_type"] == "limit"
    assert spot_kwargs["post_only"] is True

    # Verifikasi jaminan spread positif: Spot Sell Price >= Perp Buy Price
    spot_sell_p = spot_kwargs["price"]
    perp_buy_p = perp_kwargs["price"]
    assert spot_sell_p >= perp_buy_p, f"Spot Sell {spot_sell_p} harus >= Perp Buy {perp_buy_p}!"

