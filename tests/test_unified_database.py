import pytest
from datetime import datetime, timezone
from pathlib import Path

from core.database import UnifiedDatabase
from core.models import DeltaNeutralPosition, PositionLeg, TakerSnapshot
from core.historical_store import HistoricalStore

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_unified.db"
    database = UnifiedDatabase(sqlite_path=str(db_file))
    return database

def test_db_schema_initialization(temp_db):
    """Memverifikasi seluruh tabel terbuat sempurna."""
    with temp_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row["name"] if isinstance(row, dict) else row[0] for row in cursor.fetchall()]

    assert "positions" in tables
    assert "order_executions" in tables
    assert "funding_harvests" in tables
    assert "funding_history" in tables
    assert "potential_taker_snapshots" in tables
    assert "agi_experience_memory" in tables
    assert "portfolio_snapshots" in tables

def test_position_lifecycle_in_db(temp_db):
    """Menguji siklus hidup pencatatan posisi di database."""
    spot_leg = PositionLeg(
        market_type="spot", symbol="ARX/USDT", side="buy",
        amount=140.0, entry_price=0.22, current_price=0.22,
        nominal_usdt=30.8, fee_paid=0.03
    )
    perp_leg = PositionLeg(
        market_type="perp", symbol="ARX/USDT:USDT", side="short",
        amount=140.0, entry_price=0.22, current_price=0.22,
        nominal_usdt=30.8, fee_paid=0.02
    )
    pos = DeltaNeutralPosition(
        position_id="dn-arx-test-1",
        base_asset="ARX",
        spot_leg=spot_leg,
        perp_leg=perp_leg,
        leverage=1,
        funding_interval_hours=8,
        net_delta=0.0,
        status="OPEN"
    )

    # 1. Simpan posisi baru
    temp_db.record_position(pos)
    active = temp_db.get_active_positions_from_db()
    assert len(active) == 1
    assert active[0]["base_asset"] == "ARX"
    assert active[0]["status"] == "OPEN"
    assert active[0]["leverage"] == 1

    # 2. Update posisi (misal PnL bertambah)
    pos.cumulative_funding_received = 0.45
    pos.net_pnl_usdt = 0.40
    pos.is_bep_reached = True
    temp_db.record_position(pos)

    updated_active = temp_db.get_active_positions_from_db()
    assert updated_active[0]["cumulative_funding_usdt"] == pytest.approx(0.45)
    assert updated_active[0]["is_bep_reached"] == 1

    # 3. Tutup posisi
    temp_db.close_position_in_db("dn-arx-test-1", exit_reason="TARGET_PROFIT", net_pnl=0.45)
    assert len(temp_db.get_active_positions_from_db()) == 0

    history = temp_db.get_positions_history(limit=10)
    assert len(history) == 1
    assert history[0]["status"] == "CLOSED"
    assert history[0]["exit_reason"] == "TARGET_PROFIT"

def test_order_execution_recording(temp_db):
    """Menguji pencatatan riwayat eksekusi order."""
    temp_db.record_order_execution(
        order_id="ord-spot-001",
        position_id="dn-arx-test-1",
        market_type="spot",
        symbol="ARX/USDT",
        side="buy",
        requested_amount=140.0,
        filled_amount=140.0,
        price=0.22,
        fee_paid=0.03,
        status="FILLED"
    )

    orders = temp_db.get_orders_history(position_id="dn-arx-test-1")
    assert len(orders) == 1
    assert orders[0]["order_id"] == "ord-spot-001"
    assert orders[0]["filled_amount"] == pytest.approx(140.0)
    assert orders[0]["price"] == pytest.approx(0.22)

def test_funding_harvest_and_compounding_ledger(temp_db):
    """Menguji pencatatan buku kas panen dividen funding fee."""
    temp_db.record_funding_harvest(
        position_id="dn-arx-test-1",
        base_asset="ARX",
        perp_symbol="ARX/USDT:USDT",
        funding_rate=0.0012,
        interval_hours=8,
        payment_usdt=0.0369,
        compounded_amount_usdt=0.0369
    )
    temp_db.record_funding_harvest(
        position_id="dn-arx-test-1",
        base_asset="ARX",
        perp_symbol="ARX/USDT:USDT",
        funding_rate=0.0015,
        interval_hours=8,
        payment_usdt=0.0462,
        compounded_amount_usdt=0.0462
    )

    harvests = temp_db.get_harvest_history(base_asset="ARX")
    assert len(harvests) == 2

    total = temp_db.get_total_harvested_profit()
    assert total == pytest.approx(0.0369 + 0.0462)

def test_agentic_memory_and_evolution(temp_db):
    """Menguji memori pembelajaran kontinu dan evolusi reputasi koin Gemini AI."""
    temp_db.record_agi_experience(
        event_type="HARVEST",
        base_asset="ARX",
        funding_rate=0.0012,
        harvest_usdt=0.05,
        net_pnl_usdt=0.04,
        holding_hours=8.0,
        was_bep_reached=True,
        ai_decision="HOLD_PANEN",
        lesson_learned="ARX mempertahankan funding rate positif konsisten.",
        pair_reputation_score=0.7
    )

    mem = temp_db.get_agentic_memory(limit=10)
    assert len(mem) == 1
    assert mem[0]["base_asset"] == "ARX"
    assert mem[0]["event_type"] == "HARVEST"
    assert "ARX mempertahankan" in mem[0]["lesson_learned"]

    rep = temp_db.get_pair_reputation("ARX")
    assert rep["base_asset"] == "ARX"
    assert rep["successful_harvests"] == 1
    assert rep["reputation_score"] >= 0.6

def test_portfolio_snapshot_and_equity_curve(temp_db):
    """Menguji snapshot saldo portofolio dan kurva ekuitas."""
    temp_db.record_portfolio_snapshot(
        total_liquid_usdt=62.50,
        spot_free_usdt=0.71,
        swap_free_usdt=0.97,
        otc_free_usdt=0.0,
        active_positions_count=1,
        compounded_capital_usdt=62.50,
        total_profit_harvested_usdt=0.45
    )

    history = temp_db.get_portfolio_history(limit=5)
    assert len(history) == 1
    assert history[0]["total_liquid_usdt"] == pytest.approx(62.50)
    assert history[0]["active_positions_count"] == 1

def test_historical_store_backward_compatibility(temp_db):
    """Memastikan wrapper HistoricalStore bekerja 100% mulus dengan UnifiedDatabase."""
    hs = HistoricalStore(db_instance=temp_db)
    
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Simpan funding rates
    dummy_rates = [
        {"fundingRate": 0.0001, "fundingTimestamp": now_ms - (3600 * 8 * 1000)},
        {"fundingRate": 0.0002, "fundingTimestamp": now_ms}
    ]
    inserted = hs.record_funding_rates("ETH/USDT:USDT", dummy_rates, interval_hours=8)
    assert inserted == 2

    # Query
    rates = hs.get_funding_history("ETH/USDT:USDT", days=7)
    assert len(rates) == 2
    assert rates[0]["funding_rate"] == pytest.approx(0.0001)
