import pytest
from unittest.mock import AsyncMock, MagicMock
from core.models import DeltaNeutralPosition, PositionLeg
from risk.funding_guard import FundingGuard
from risk.margin_guard import MarginGuard
from execution.rebalancer import AutoRebalancer

@pytest.mark.asyncio
async def test_funding_guard_emergency_close_position_exists():
    mock_client = MagicMock()
    mock_pos_mgr = MagicMock()
    mock_executor = MagicMock()
    mock_executor.close_delta_neutral_position = AsyncMock(return_value=True)

    fg = FundingGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=mock_executor)
    
    # Must have emergency_close_position
    assert hasattr(fg, "emergency_close_position")
    
    pos = MagicMock()
    pos.position_id = "test-dn-123"
    res = await fg.emergency_close_position(pos, reason="Test Reason")
    assert res is True
    mock_executor.close_delta_neutral_position.assert_awaited_once_with(position_id="test-dn-123", reason="Test Reason")

@pytest.mark.asyncio
async def test_margin_guard_survives_api_failure():
    mock_client = MagicMock()
    mock_pos_mgr = MagicMock()
    # Simulate API failure returning empty tickers or raising exception
    mock_client.fetch_tickers_by_type = AsyncMock(side_effect=Exception("Connection reset by peer"))
    
    pos = MagicMock()
    mock_pos_mgr.get_active_positions.return_value = [pos]

    mg = MarginGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=MagicMock())
    # Should catch exception internally and not raise
    await mg.check_positions_health()

@pytest.mark.asyncio
async def test_funding_guard_survives_api_failure():
    mock_client = MagicMock()
    mock_pos_mgr = MagicMock()
    mock_client.fetch_all_funding_rates = AsyncMock(side_effect=Exception("502 Bad Gateway Bitget"))

    pos = MagicMock()
    mock_pos_mgr.get_active_positions.return_value = [pos]

    fg = FundingGuard(client=mock_client, pos_mgr=mock_pos_mgr, executor=MagicMock())
    # Should catch exception internally and not raise
    await fg.check_positions_funding()

@pytest.mark.asyncio
async def test_auto_rebalancer_survives_failure():
    mock_client = MagicMock()
    mock_pos_mgr = MagicMock()
    pos = MagicMock()
    pos.spot_leg = MagicMock(amount=10.0, current_price=1.0)
    pos.perp_leg = MagicMock(amount=5.0)
    mock_pos_mgr.get_active_positions.return_value = [pos]
    mock_client.execute_perp_order = AsyncMock(side_effect=Exception("Network timeout"))

    rebalancer = AutoRebalancer(client=mock_client, pos_mgr=mock_pos_mgr, executor=MagicMock())
    # Should catch exception and not raise
    await rebalancer.rebalance_delta_drift()
