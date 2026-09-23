import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from core.models import DeltaNeutralPosition, PositionLeg
from core.database import UnifiedDatabase
from execution.position_manager import PositionManager

@pytest.mark.asyncio
async def test_sync_active_positions_new_discovery(tmp_path):
    temp_json = tmp_path / "positions.json"
    temp_db = UnifiedDatabase(sqlite_path=str(tmp_path / "test.db"))
    pos_mgr = PositionManager(data_file=str(temp_json), db_instance=temp_db)

    # Mock Bitget client
    mock_client = MagicMock()
    mock_ccxt = AsyncMock()

    # Futures return 1 active ARX short
    mock_ccxt.fetch_positions.return_value = [
        {
            "symbol": "ARX/USDT:USDT",
            "contracts": 142.0,
            "entryPrice": 0.22016,
            "markPrice": 0.21707,
            "side": "short",
            "leverage": 1.0,
        }
    ]

    # Spot balance returns 141.77 ARX
    mock_ccxt.fetch_balance.return_value = {
        "total": {"ARX": 141.77823, "USDT": 0.70}
    }

    mock_ccxt.fetch_funding_rate.return_value = {
        "interval": "4h",
        "fundingRate": 0.000133
    }

    mock_client.client = mock_ccxt
    mock_client.update_position_live_pnl = AsyncMock(side_effect=lambda p: p)

    # Execute sync
    active = await pos_mgr.sync_active_positions_from_exchange(mock_client)

    assert len(active) == 1
    pos = active[0]
    assert pos.base_asset == "ARX"
    assert pos.spot_leg.amount == 141.77823
    assert pos.perp_leg.amount == 142.0
    assert pos.perp_leg.side == "short"
    assert pos.leverage == 1
    assert pos.funding_interval_hours == 4
    assert pos.net_delta == pytest.approx(-0.22177, 0.001)
    assert pos.status == "OPEN"

@pytest.mark.asyncio
async def test_sync_active_positions_closes_vanished_positions(tmp_path):
    temp_json = tmp_path / "positions.json"
    temp_db = UnifiedDatabase(sqlite_path=str(tmp_path / "test2.db"))
    pos_mgr = PositionManager(data_file=str(temp_json), db_instance=temp_db)

    # Pre-add an old position
    old_pos = DeltaNeutralPosition(
        position_id="dn-old-123",
        base_asset="OLD",
        spot_leg=PositionLeg(market_type="spot", symbol="OLD/USDT", side="buy", amount=10, entry_price=1, current_price=1, nominal_usdt=10),
        perp_leg=PositionLeg(market_type="perp", symbol="OLD/USDT:USDT", side="short", amount=10, entry_price=1, current_price=1, nominal_usdt=10),
        leverage=1,
        net_delta=0.0,
        status="OPEN"
    )
    pos_mgr.add_position(old_pos)
    assert len(pos_mgr.get_active_positions()) == 1

    # Mock Bitget client with 0 active positions
    mock_client = MagicMock()
    mock_ccxt = AsyncMock()
    mock_ccxt.fetch_positions.return_value = []
    mock_ccxt.fetch_balance.return_value = {"total": {}}
    mock_client.client = mock_ccxt
    mock_client.update_position_live_pnl = AsyncMock(side_effect=lambda p: p)

    active = await pos_mgr.sync_active_positions_from_exchange(mock_client)
    assert len(active) == 0
    assert pos_mgr.positions["dn-old-123"].status == "CLOSED"
