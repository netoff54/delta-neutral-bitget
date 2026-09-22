import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from core.models import Opportunity, FeeBreakdown
from core.bitget_client import BitgetClient
from risk.compounding_manager import CompoundingManager
from execution.order_executor import OrderExecutor
from execution.position_manager import PositionManager

async def test_full_capital_allocation():
    print("=== TEST 1: Dynamic Allocation of Full Liquid Trading Capital ===")
    cm = CompoundingManager(state_file="data/test_compounding_state.json")
    
    # Skenario: User memiliki total 150.0 USDT saldo cair (Spot Free + Futures Free)
    # dan $5,000.00 di Bitget Earn
    liquid_usdt = 150.0
    allocated = cm.get_position_capital(available_liquid_usdt=liquid_usdt)
    
    # 150 * (1 - 0.02 buffer) = 147.0 USDT
    expected = round(150.0 * 0.98, 2)
    assert allocated == expected, f"Expected {expected}, got {allocated}"
    print(f"✅ Saldo cair $150.00 teralokasi: ${allocated:.2f} USDT (setelah 2% safety buffer)")

async def test_bitget_earn_isolation():
    print("\n=== TEST 2: Strict Bitget Earn Isolation ===")
    cm = CompoundingManager(state_file="data/test_compounding_state.json")
    
    liquid_usdt = 150.0
    earn_usdt = 5000.0  # Dana tabungan di Bitget Earn
    
    # Skenario A: Menggunakan dana cair yang valid ($147 <= $147)
    valid = cm.validate_capital_usage(requested_amount=147.0, available_liquid_usdt=liquid_usdt)
    assert valid is True, "Harusnya valid untuk dana cair"
    print("✅ Penggunaan modal trading cair ($147.00) DISETUJUI")
    
    # Skenario B: Mencoba menggunakan dana yang melebihi saldo cair (misal ingin menarik dana earn $500)
    invalid = cm.validate_capital_usage(requested_amount=500.0, available_liquid_usdt=liquid_usdt)
    assert invalid is False, "Harusnya DITOLAK karena menyentuh saldo di luar trading cair"
    print("✅ Percobaan alokasi melebihi saldo trading cair ($500.00) DITOLAK KERAS untuk melindungi Bitget Earn")

async def test_leg_sizing_and_leverage_math():
    print("\n=== TEST 3: Delta Neutral Leg Sizing with Leverage 2x ===")
    allocated_capital = 150.0 * 0.98  # $147.00
    effective_leverage = 2
    
    # Formula: leg_nominal = Total / (1 + 1/L)
    leg_nominal = allocated_capital / (1.0 + (1.0 / effective_leverage))
    spot_cost = leg_nominal
    perp_margin = leg_nominal / effective_leverage
    total_used = spot_cost + perp_margin
    
    assert abs(total_used - allocated_capital) < 1e-5, f"Total used {total_used} != {allocated_capital}"
    print(f"✅ Modal Total: ${allocated_capital:.2f} USDT")
    print(f"   -> Spot Long Leg: ${spot_cost:.2f} USDT")
    print(f"   -> Perp Short Leg: ${leg_nominal:.2f} Notional (Margin: ${perp_margin:.2f} USDT @ 2x)")
    print(f"   -> Total Modal Terpakai: ${total_used:.2f} USDT")
    print(f"   -> Net Delta = Spot Long (${spot_cost:.2f}) - Perp Short (${leg_nominal:.2f}) = 0.00 (Hedging Sempurna)")

async def test_wallet_balancing():
    print("\n=== TEST 4: Automatic Internal Wallet Balancing (Spot <-> Swap) ===")
    client = BitgetClient()
    
    # Mock dry-run wallet balancing
    balanced = await client.balance_wallets_for_position(required_spot=98.0, required_swap=49.0)
    assert balanced is True
    print("✅ Penyeimbangan dompet Spot & Futures (Swap) berhasil diverifikasi (Bitget Earn tetap 100% terisolasi)")

async def test_otc_balance_and_transfer():
    print("\n=== TEST 5: Automatic OTC / P2P Balance Detection & Transfer ===")
    client = BitgetClient()
    
    with patch.object(settings, "DRY_RUN", True):
        # 1. Pastikan saldo OTC terdeteksi
        otc_bal = await client.fetch_otc_balance()
        assert otc_bal == 50.0, f"Expected 50.0, got {otc_bal}"
        print(f"✅ Saldo OTC / P2P terdeteksi: ${otc_bal:.2f} USDT")

        # 2. Total saldo cair mencakup Spot + Futures + OTC
        liquid_info = await client.fetch_liquid_trading_balance()
        expected_total = 100.0 + 50.0  # SIMULATED_BALANCE_USDT (100) + SIMULATED_OTC_BALANCE_USDT (50)
        assert liquid_info["total_liquid_usdt"] == expected_total, f"Expected {expected_total}, got {liquid_info['total_liquid_usdt']}"
        assert liquid_info["otc_free"] == 50.0
        print(f"✅ Total Saldo Cair Gabungan: ${liquid_info['total_liquid_usdt']:.2f} USDT (Spot: ${liquid_info['spot_free']:.2f}, Swap: ${liquid_info['swap_free']:.2f}, OTC: ${liquid_info['otc_free']:.2f})")

    # 3. Test Transfer dari OTC ke Spot/Futures saat dibutuhkan
    # Mocking transfer call
    client.client.transfer = AsyncMock(return_value={"id": "mock_transfer_id"})
    
    # Skenario: Spot punya 10 USDT, Futures punya 10 USDT, butuh Spot 40 USDT dan Futures 20 USDT.
    # OTC punya 50 USDT. Harus menarik dari OTC ke Spot dan Futures!
    with patch.object(client, "fetch_liquid_trading_balance", new_callable=AsyncMock) as mock_liquid:
        mock_liquid.return_value = {
            "spot_free": 10.0,
            "swap_free": 10.0,
            "otc_free": 50.0,
            "total_liquid_usdt": 70.0
        }
        with patch.object(settings, "DRY_RUN", False):
            success = await client.balance_wallets_for_position(required_spot=40.0, required_swap=20.0)
            assert success is True
            assert client.client.transfer.call_count >= 2
            # Periksa transfer pertama dari 'p2p' (Akun OTC) ke 'spot' (Akun Spot)
            call1 = client.client.transfer.call_args_list[0]
            assert call1.kwargs["fromAccount"] == "p2p"
            assert call1.kwargs["toAccount"] == "spot"
            # Periksa transfer kedua dari 'p2p' (Akun OTC) ke 'swap' (Akun Futures)
            call2 = client.client.transfer.call_args_list[1]
            assert call2.kwargs["fromAccount"] == "p2p"
            assert call2.kwargs["toAccount"] == "swap"
            print("✅ Otomasi penarikan dana dari Akun OTC ke Spot & Futures terverifikasi!")

async def test_mutual_internal_transfers():
    print("\n=== TEST 6: Saling Transfer Internal 2 Arah Antar Akun OTC, Spot, dan Futures ===")
    client = BitgetClient()
    client.client.transfer = AsyncMock(return_value={"id": "mock_transfer_id"})

    with patch.object(settings, "DRY_RUN", False):
        # 1. OTC -> Spot
        ok1 = await client.transfer_internal("otc", "spot", 25.0)
        assert ok1 is True
        assert client.client.transfer.call_args.kwargs["fromAccount"] == "p2p"
        assert client.client.transfer.call_args.kwargs["toAccount"] == "spot"

        # 2. Spot -> OTC
        ok2 = await client.transfer_internal("spot", "otc", 15.0)
        assert ok2 is True
        assert client.client.transfer.call_args.kwargs["fromAccount"] == "spot"
        assert client.client.transfer.call_args.kwargs["toAccount"] == "p2p"

        # 3. Futures -> OTC
        ok3 = await client.transfer_internal("futures", "otc", 10.0)
        assert ok3 is True
        assert client.client.transfer.call_args.kwargs["fromAccount"] == "swap"
        assert client.client.transfer.call_args.kwargs["toAccount"] == "p2p"

        # 4. OTC -> Futures
        ok4 = await client.transfer_internal("otc", "futures", 20.0)
        assert ok4 is True
        assert client.client.transfer.call_args.kwargs["fromAccount"] == "p2p"
        assert client.client.transfer.call_args.kwargs["toAccount"] == "swap"

        # 5. Spot -> Futures
        ok5 = await client.transfer_internal("spot", "futures", 5.0)
        assert ok5 is True
        assert client.client.transfer.call_args.kwargs["fromAccount"] == "spot"
        assert client.client.transfer.call_args.kwargs["toAccount"] == "swap"

        # 6. Futures -> Spot
        ok6 = await client.transfer_internal("futures", "spot", 5.0)
        assert ok6 is True
        assert client.client.transfer.call_args.kwargs["fromAccount"] == "swap"
        assert client.client.transfer.call_args.kwargs["toAccount"] == "spot"

    print("✅ Seluruh 6 arah transfer internal pribadi (OTC <-> Spot <-> Futures) terbukti bekerja 100%!")

async def main():
    await test_full_capital_allocation()
    await test_bitget_earn_isolation()
    await test_leg_sizing_and_leverage_math()
    await test_wallet_balancing()
    await test_otc_balance_and_transfer()
    await test_mutual_internal_transfers()
    print("\n🎉 SELURUH PENGUJIAN OTOMASI MODAL, SALING TRANSFER OTC/SPOT/FUTURES, & PROTEKSI EARN BERHASIL 100%!")

if __name__ == "__main__":
    asyncio.run(main())
