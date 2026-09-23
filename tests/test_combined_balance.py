import pytest
import os
import tempfile
import asyncio
from unittest.mock import AsyncMock, MagicMock
from core.database import UnifiedDatabase
from core.bitget_client import BitgetClient

@pytest.fixture
def temp_db():
    temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_file.close()
    database = UnifiedDatabase(sqlite_path=temp_file.name)
    yield database
    try:
        os.remove(temp_file.name)
    except OSError:
        pass

@pytest.mark.asyncio
async def test_fetch_all_combined_balances_mock():
    client = BitgetClient()
    client.client = MagicMock()

    # Mock dynamic balance for spot & swap
    async def dynamic_fetch_balance(params=None):
        if params and params.get("type") == "swap":
            return {
                "total": {"USDT": 50.0},
                "free": {"USDT": 20.0},
                "used": {"USDT": 30.0},
            }
        return {
            "total": {
                "USDT": 10.5,
                "ARX": 100.0,
            },
            "free": {
                "USDT": 10.5,
                "ARX": 100.0,
            }
        }

    client.client.fetch_balance = AsyncMock(side_effect=dynamic_fetch_balance)
    client.fetch_tickers_by_type = AsyncMock(return_value={
        "ARX/USDT": {"last": 0.25, "close": 0.25}
    })
    client.fetch_positions = AsyncMock(return_value=[])
    client.fetch_otc_balance = AsyncMock(return_value=5.0)

    # Mock privateEarnGetV2EarnAccountAssets (Earn read-only)
    client.client.privateEarnGetV2EarnAccountAssets = AsyncMock(return_value={
        "data": {
            "assetList": [
                {"coin": "USDT", "amount": "15.0"}
            ]
        }
    })

    res = await client.fetch_all_combined_balances()

    assert "total_combined_equity_usdt" in res
    assert "spot" in res
    assert "futures" in res
    assert "otc" in res
    assert "earn" in res

    # Spot: 10.5 USDT + 100 ARX * $0.25 = $35.5
    assert res["spot"]["equity_usdt"] == pytest.approx(35.5, rel=1e-2)
    # Futures: equity = $50.0
    assert res["futures"]["equity_usdt"] == pytest.approx(50.0, rel=1e-2)
    # OTC: $5.0
    assert res["otc"]["equity_usdt"] == pytest.approx(5.0, rel=1e-2)
    # Earn: $15.0
    assert res["earn"]["equity_usdt"] == pytest.approx(15.0, rel=1e-2)

    # Total combined: 35.5 + 50.0 + 5.0 + 15.0 = 105.5
    assert res["total_combined_equity_usdt"] == pytest.approx(105.5, rel=1e-2)

def test_database_combined_balance_lifecycle(temp_db):
    mock_data = {
        "total_combined_equity_usdt": 63.67,
        "spot": {
            "equity_usdt": 31.64,
            "free_usdt": 0.71,
            "holdings": [{"coin": "ARX", "amount": 141.78, "value_usdt": 30.93}]
        },
        "futures": {
            "equity_usdt": 32.03,
            "free_usdt": 0.92,
            "margin_used_usdt": 31.26,
            "unrealized_pnl_usdt": 0.05
        },
        "otc": {
            "equity_usdt": 0.0
        },
        "earn": {
            "equity_usdt": 0.0,
            "status": "100% PROTECTED & UNTOUCHED"
        }
    }

    # 1. Record snapshot
    row_id = temp_db.record_combined_balance(mock_data, active_positions_count=1)
    assert row_id is not None

    # 2. Retrieve latest
    latest = temp_db.get_latest_combined_balance()
    assert latest is not None
    assert latest["total_combined_equity_usdt"] == pytest.approx(63.67, rel=1e-2)
    assert latest["spot_equity_usdt"] == pytest.approx(31.64, rel=1e-2)
    assert latest["futures_equity_usdt"] == pytest.approx(32.03, rel=1e-2)
    assert latest["active_positions_count"] == 1
    assert "ARX" in latest["wallet_details_json"]

    # 3. Retrieve history
    history = temp_db.get_combined_balance_history(limit=10)
    assert len(history) == 1
    assert history[0]["id"] == row_id

    # 4. Growth stats
    stats = temp_db.get_equity_growth_stats(days=7)
    assert stats["sample_count"] == 1
    assert stats["current_equity_usdt"] == pytest.approx(63.67, rel=1e-2)
    assert stats["net_change_usdt"] == 0.0
    assert stats["growth_pct"] == 0.0
    assert stats["stability_rating"] == "HIGHLY_STABLE"

@pytest.mark.asyncio
async def test_earn_api_failure_handled_gracefully():
    client = BitgetClient()
    client.client = MagicMock()
    client.client.fetch_balance = AsyncMock(return_value={"total": {"USDT": 20.0}, "free": {"USDT": 20.0}})
    client.fetch_tickers_by_type = AsyncMock(return_value={})
    client.fetch_positions = AsyncMock(return_value=[])
    client.fetch_otc_balance = AsyncMock(return_value=0.0)

    # Earn raises exception
    client.client.privateEarnGetV2EarnAccountAssets = AsyncMock(side_effect=Exception("Permission denied"))

    res = await client.fetch_all_combined_balances()
    assert res["earn"]["equity_usdt"] == 0.0
    assert res["earn"]["status"] == "100% PROTECTED & UNTOUCHED"
    assert res["total_combined_equity_usdt"] >= 20.0
