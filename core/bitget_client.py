import asyncio
from typing import Dict, Any, List, Optional
import ccxt.async_support as ccxt

from config.settings import settings
from utils.logger import log
from utils.dns_resolver import patch_dns
from core.models import OrderExecutionResult

class BitgetClient:
    """
    Client wrapper untuk Bitget API (Spot & USDT-M Perp) menggunakan CCXT Async.
    """

    def __init__(self):
        # Terapkan patch DNS untuk mengatasi pemblokiran ISP
        patch_dns()

        config = {
            "apiKey": settings.BITGET_API_KEY,
            "secret": settings.BITGET_API_SECRET,
            "password": settings.BITGET_API_PASSPHRASE,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # Default futures swap
                "adjustForTimeDifference": True,
            },
            "timeout": 15000,
        }

        self.client = ccxt.bitget(config)
        self.markets_loaded = False

    async def initialize(self):
        """Memuat daftar market Bitget (Spot & Futures)."""
        if not self.markets_loaded:
            log.info("Memuat pasar Bitget (Spot & USDT-M Futures)...")
            await self.client.load_markets()
            self.markets_loaded = True
            log.info(f"Berhasil memuat {len(self.client.markets)} pasar dari Bitget.")

    async def close(self):
        """Menutup koneksi client."""
        await self.client.close()

    async def fetch_all_funding_rates(self) -> Dict[str, Any]:
        """Mengambil data funding rate untuk seluruh kontrak futures."""
        return await self.client.fetch_funding_rates()

    async def fetch_fund_rates_and_intervals(self) -> Dict[str, Dict[str, Any]]:
        """
        Mengambil funding rate terkini beserta interval penyelesaian dinamis (misal 4h, 8h, 1h)
        dan timestamp pembayaran berikutnya langsung dari endpoint Bitget V2.
        """
        try:
            res = await self.client.publicMixGetV2MixMarketCurrentFundRate({"productType": "USDT-FUTURES"})
            data = res.get("data", [])
            result = {}
            for item in data:
                raw_sym = item.get("symbol", "")
                interval_str = item.get("fundingRateInterval", "8")
                interval_hours = int(interval_str) if interval_str and interval_str.isdigit() else 8
                rate = float(item.get("fundingRate", 0.0))
                next_ts = int(item.get("nextUpdate", 0)) if item.get("nextUpdate") else None

                # Konversi nama symbol Bitget (e.g. BTCUSDT) ke format standar CCXT (BTC/USDT:USDT)
                if raw_sym.endswith("USDT"):
                    base = raw_sym[:-4]
                    ccxt_sym = f"{base}/USDT:USDT"
                    result[ccxt_sym] = {
                        "symbol": ccxt_sym,
                        "base_asset": base,
                        "fundingRate": rate,
                        "fundingInterval": interval_hours,
                        "nextUpdate": next_ts
                    }
            return result
        except Exception as e:
            log.warning(f"Gagal mengambil current-fund-rate Bitget V2: {e}. Fallback ke fetch_funding_rates()")
            fallback_rates = await self.fetch_all_funding_rates()
            return {
                sym: {
                    "symbol": sym,
                    "fundingRate": float(r.get("fundingRate", 0.0)),
                    "fundingInterval": 8,
                    "nextUpdate": r.get("fundingTimestamp")
                }
                for sym, r in fallback_rates.items()
            }

    async def fetch_funding_rate_history(self, symbol: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Mengambil riwayat funding rate untuk pasangan tertentu."""
        try:
            return await self.client.fetch_funding_rate_history(symbol, limit=limit)
        except Exception as e:
            log.warning(f"Gagal mengambil riwayat funding rate untuk {symbol}: {e}")
            return []

    async def fetch_tickers(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """Mengambil ticker harga terkini."""
        return await self.client.fetch_tickers(symbols)

    async def fetch_tickers_by_type(self, market_type: str = "swap") -> Dict[str, Any]:
        """Mengambil ticker berdasarkan tipe pasar ('spot' atau 'swap')."""
        return await self.client.fetch_tickers(params={"type": market_type})

    async def fetch_otc_balance(self) -> float:
        """
        Mengambil saldo USDT di dompet OTC / P2P / Funding Bitget.
        SAMA SEKALI TIDAK MENGAKSES ATAU MENYENTUH FITUR EARN.
        """
        if settings.DRY_RUN:
            return float(settings.SIMULATED_OTC_BALANCE_USDT if settings.INCLUDE_OTC_BALANCE else 0.0)

        try:
            # 1. Coba endpoint V2 funding-assets (classic/P2P)
            res = await self.client.privateSpotGetV2AccountFundingAssets()
            data = res.get("data", [])
            for item in data:
                if item.get("coin", "").upper() == "USDT":
                    return float(item.get("available") or item.get("balance") or 0.0)
        except Exception as e:
            log.debug(f"privateSpotGetV2AccountFundingAssets check: {e}")

        try:
            # 2. Coba endpoint UTA V3 funding assets
            res = await self.client.privateUtaGetV3AccountFundingAssets()
            data = res.get("data", [])
            for item in data:
                if item.get("coin", "").upper() == "USDT":
                    return float(item.get("available") or item.get("balance") or 0.0)
        except Exception as e:
            log.debug(f"privateUtaGetV3AccountFundingAssets check: {e}")

        try:
            # 3. Coba endpoint V3 P2P balance
            res = await self.client.privateUtaGetV3P2pBalance({"token": "USDT"})
            data = res.get("data", {})
            if isinstance(data, dict):
                return float(data.get("available") or data.get("balance") or 0.0)
        except Exception as e:
            log.debug(f"privateUtaGetV3P2pBalance check: {e}")

        return 0.0

    async def fetch_liquid_trading_balance(self) -> Dict[str, float]:
        """
        Mengambil saldo USDT cair yang bebas (liquid free balance) dari dompet Spot, Futures, dan OTC/P2P.
        SAMA SEKALI TIDAK MENGAKSES, MEREDEEM, ATAU MENYENTUH DANA DI FITUR EARN/SAVINGS BITGET.
        """
        if settings.DRY_RUN:
            sim = settings.SIMULATED_BALANCE_USDT
            otc = float(settings.SIMULATED_OTC_BALANCE_USDT if settings.INCLUDE_OTC_BALANCE else 0.0)
            return {
                "spot_free": round(sim / 2.0, 4),
                "swap_free": round(sim / 2.0, 4),
                "otc_free": round(otc, 4),
                "total_liquid_usdt": round(sim + otc, 4)
            }
        try:
            # Ambil saldo spot bebas
            spot_bal = await self.client.fetch_balance({"type": "spot"})
            spot_free = float(spot_bal.get("free", {}).get("USDT", 0.0) or spot_bal.get("USDT", {}).get("free", 0.0) or 0.0)

            # Ambil saldo futures swap bebas
            swap_bal = await self.client.fetch_balance({"type": "swap"})
            swap_free = float(swap_bal.get("free", {}).get("USDT", 0.0) or swap_bal.get("USDT", {}).get("free", 0.0) or 0.0)

            # Ambil saldo OTC / P2P bebas jika diaktifkan
            otc_free = 0.0
            if settings.INCLUDE_OTC_BALANCE:
                otc_free = await self.fetch_otc_balance()

            total_liquid = spot_free + swap_free + otc_free
            return {
                "spot_free": round(spot_free, 4),
                "swap_free": round(swap_free, 4),
                "otc_free": round(otc_free, 4),
                "total_liquid_usdt": round(total_liquid, 4)
            }
        except Exception as e:
            log.error(f"Gagal mengambil saldo cair trading dari Bitget: {e}")
            return {"spot_free": 0.0, "swap_free": 0.0, "otc_free": 0.0, "total_liquid_usdt": 0.0}

    async def fetch_balance(self) -> Dict[str, Any]:
        """Mengambil informasi saldo akun trading."""
        liquid = await self.fetch_liquid_trading_balance()
        tot = liquid["total_liquid_usdt"]
        return {
            "USDT": {"free": tot, "total": tot},
            "total": {"USDT": tot},
            "free": {"USDT": tot},
            "spot_free": liquid["spot_free"],
            "swap_free": liquid["swap_free"],
            "otc_free": liquid.get("otc_free", 0.0)
        }

    async def transfer_internal(self, from_wallet: str, to_wallet: str, amount: float) -> bool:
        """
        Transfer internal USDT dua arah antar-dompet pribadi Anda di Bitget:
        - 'otc' / 'funding' : Akun OTC / Pendanaan (di API internal Bitget menggunakan kode 'p2p')
        - 'spot'            : Akun Spot
        - 'futures' / 'swap': Akun USDT-M Futures

        CATATAN PENTING:
        Ini 100% adalah TRANSFER INTERNAL antar dompet Anda sendiri di Bitget (bebas biaya/Rp 0 fee, instan).
        SAMA SEKALI BUKAN transaksi jual-beli P2P dengan pihak ketiga/merchant!
        SAMA SEKALI TIDAK PERNAH MENGAKSES/MENYENTUH PRODUK BITGET EARN.
        """
        if amount <= 0:
            return True

        alias_map = {
            "otc": "p2p",
            "p2p": "p2p",
            "funding": "p2p",
            "spot": "spot",
            "futures": "swap",
            "swap": "swap"
        }

        from_acc = alias_map.get(from_wallet.lower(), from_wallet)
        to_acc = alias_map.get(to_wallet.lower(), to_wallet)

        if from_acc == to_acc:
            return True

        wallet_labels = {
            "p2p": "Akun OTC/Pendanaan",
            "spot": "Akun Spot",
            "swap": "Akun Futures"
        }

        from_label = wallet_labels.get(from_acc, from_acc)
        to_label = wallet_labels.get(to_acc, to_acc)

        log.info(f"[Transfer Internal] Memindahkan ${amount:.2f} USDT dari {from_label} ke {to_label}...")

        if settings.DRY_RUN:
            log.info(f"[Transfer Internal - Simulasi] Berhasil mentransfer ${amount:.2f} USDT dari {from_label} ke {to_label}.")
            return True

        try:
            await self.client.transfer(
                code="USDT",
                amount=amount,
                fromAccount=from_acc,
                toAccount=to_acc
            )
            log.info(f"[Transfer Internal - Sukses] ${amount:.2f} USDT berhasil dipindahkan dari {from_label} ke {to_label}.")
            await asyncio.sleep(1)
            return True
        except Exception as e:
            log.error(f"[Transfer Internal - Gagal] Gagal transfer ${amount:.2f} USDT dari {from_label} ke {to_label}: {e}")
            return False

    async def balance_wallets_for_position(self, required_spot: float, required_swap: float) -> bool:
        """
        Saling menyeimbangkan alokasi USDT antar 3 akun trading Anda (Spot, Futures, dan OTC)
        secara otomatis melalui Transfer Internal agar kedua kaki order (Spot Long & Futures Short)
        dapat dieksekusi dengan lancar.
        HANYA mentransfer antar dompet internal Anda sendiri ('p2p', 'spot', 'swap').
        SAMA SEKALI TIDAK PERNAH MENYENTUH DANA DI FITUR BITGET EARN.
        """
        if settings.DRY_RUN:
            return True

        try:
            liquid = await self.fetch_liquid_trading_balance()
            spot_free = liquid["spot_free"]
            swap_free = liquid["swap_free"]
            otc_free = liquid.get("otc_free", 0.0)

            # 1. Kebutuhan Spot
            if spot_free < required_spot:
                deficit = required_spot - spot_free + 0.5  # buffer 0.5 USDT
                # Prioritaskan transfer internal dari Akun OTC jika tersedia saldo
                if otc_free > 0:
                    transfer_from_otc = min(otc_free, deficit)
                    success = await self.transfer_internal("otc", "spot", transfer_from_otc)
                    if success:
                        otc_free -= transfer_from_otc
                        deficit -= transfer_from_otc
                        spot_free += transfer_from_otc

                # Jika masih kurang, transfer internal dari surplus Akun Futures
                if deficit > 0 and swap_free > required_swap:
                    transfer_from_swap = min(swap_free - required_swap, deficit)
                    if transfer_from_swap > 0:
                        success = await self.transfer_internal("futures", "spot", transfer_from_swap)
                        if success:
                            swap_free -= transfer_from_swap
                            spot_free += transfer_from_swap

            # 2. Kebutuhan Futures (Margin Short)
            if swap_free < required_swap:
                deficit = required_swap - swap_free + 0.5
                # Prioritaskan transfer internal dari Akun OTC jika tersedia saldo
                if otc_free > 0:
                    transfer_from_otc = min(otc_free, deficit)
                    success = await self.transfer_internal("otc", "futures", transfer_from_otc)
                    if success:
                        otc_free -= transfer_from_otc
                        deficit -= transfer_from_otc
                        swap_free += transfer_from_otc

                # Jika masih kurang, transfer internal dari surplus Akun Spot
                if deficit > 0 and spot_free > required_spot:
                    transfer_from_spot = min(spot_free - required_spot, deficit)
                    if transfer_from_spot > 0:
                        success = await self.transfer_internal("spot", "futures", transfer_from_spot)
                        if success:
                            spot_free -= transfer_from_spot
                            swap_free += transfer_from_spot

            return True
        except Exception as e:
            log.warning(f"Gagal melakukan transfer internal trading (OTC/Spot/Futures): {e}")
            return False

    async def return_funds_to_otc(self, amount: Optional[float] = None, from_wallet: str = "spot") -> bool:
        """
        Mentransfer kembali USDT dari dompet trading (Spot / Futures) ke dompet OTC/Pendanaan.
        Dua arah (mutual transfer) antar akun pribadi Anda.
        """
        liquid = await self.fetch_liquid_trading_balance()
        wallet_key = "spot_free" if from_wallet == "spot" else "swap_free"
        avail = liquid.get(wallet_key, 0.0)

        transfer_amt = amount if (amount and amount <= avail) else max(0.0, avail - 5.0)
        if transfer_amt > 0:
            return await self.transfer_internal(from_wallet=from_wallet, to_wallet="otc", amount=transfer_amt)
        return False

    async def fetch_positions(self) -> List[Dict[str, Any]]:
        """Mengambil posisi futures terbuka saat ini."""
        if settings.DRY_RUN:
            return []
        try:
            return await self.client.fetch_positions()
        except Exception as e:
            log.error(f"Gagal mengambil posisi futures terbuka: {e}")
            return []

    def get_market(self, symbol: str) -> Dict[str, Any]:
        """Mendapatkan metadata pasar (presisi lot, tick size, minimal cost)."""
        if not self.client.markets or symbol not in self.client.markets:
            return {}
        return self.client.market(symbol)

    def align_quantity(self, spot_symbol: str, perp_symbol: str, target_nominal_usdt: float, price: float) -> float:
        """
        Menghitung kuantitas token yang terkalibrasi secara presisi antara Spot dan Perp
        agar Net Delta benar-benar 0 (tidak ada sisa desimal yang tidak ter-hedge).
        """
        raw_qty = target_nominal_usdt / price

        perp_market = self.get_market(perp_symbol)
        spot_market = self.get_market(spot_symbol)

        # Ambil precision amount dan minimal order
        perp_precision = perp_market.get("precision", {}).get("amount", 1e-4) if perp_market else 1e-4
        perp_min = (perp_market.get("limits", {}).get("amount", {}).get("min", 0.0) if perp_market else 0.0) or 0.0
        spot_min = (spot_market.get("limits", {}).get("amount", {}).get("min", 0.0) if spot_market else 0.0) or 0.0

        step = perp_precision if isinstance(perp_precision, float) else 10 ** (-perp_precision)
        aligned_qty = int(raw_qty / step) * step

        if aligned_qty < perp_min or aligned_qty < spot_min:
            return 0.0

        if perp_market and perp_symbol in self.client.markets:
            formatted_str = self.client.amount_to_precision(perp_symbol, aligned_qty)
            return float(formatted_str)
        return round(aligned_qty, 6)

    async def set_leverage_for_symbol(self, perp_symbol: str, leverage: int = settings.LEVERAGE):
        """Mengatur leverage untuk posisi futures."""
        if settings.DRY_RUN:
            return
        try:
            await self.client.set_leverage(leverage, perp_symbol, params={"marginMode": "cross"})
        except Exception as e:
            log.warning(f"Gagal mengatur leverage {leverage}x untuk {perp_symbol}: {e}")

    async def execute_spot_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        order_type: str = "market",
        price: Optional[float] = None
    ) -> OrderExecutionResult:
        """Eksekusi order di pasar Spot."""
        if settings.DRY_RUN:
            simulated_price = price or 100.0
            fee = (amount * simulated_price) * settings.SPOT_TAKER_FEE
            return OrderExecutionResult(
                success=True,
                market_type="spot",
                symbol=symbol,
                side=side,
                requested_amount=amount,
                filled_amount=amount,
                avg_price=simulated_price,
                fee_amount=fee,
                order_id=f"SIM_SPOT_{side.upper()}_{int(asyncio.get_event_loop().time())}"
            )

        try:
            params = {"createMarketBuyOrderRequiresPrice": False}
            order = await self.client.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=params
            )
            filled = float(order.get("filled", amount))
            avg_p = float(order.get("average", price or 0.0))
            fee = float(order.get("fee", {}).get("cost", 0.0) if order.get("fee") else (filled * avg_p * settings.SPOT_TAKER_FEE))

            return OrderExecutionResult(
                success=True,
                market_type="spot",
                symbol=symbol,
                side=side,
                requested_amount=amount,
                filled_amount=filled,
                avg_price=avg_p,
                fee_amount=fee,
                order_id=str(order.get("id"))
            )
        except Exception as e:
            log.error(f"[Order Spot] Gagal {side} {amount} {symbol}: {e}")
            return OrderExecutionResult(
                success=False,
                market_type="spot",
                symbol=symbol,
                side=side,
                requested_amount=amount,
                error_message=str(e)
            )

    async def execute_perp_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        order_type: str = "market",
        price: Optional[float] = None
    ) -> OrderExecutionResult:
        """Eksekusi order di pasar USDT-M Futures (Perpetual)."""
        if settings.DRY_RUN:
            simulated_price = price or 100.0
            fee = (amount * simulated_price) * settings.PERP_TAKER_FEE
            return OrderExecutionResult(
                success=True,
                market_type="perp",
                symbol=symbol,
                side=side,
                requested_amount=amount,
                filled_amount=amount,
                avg_price=simulated_price,
                fee_amount=fee,
                order_id=f"SIM_PERP_{side.upper()}_{int(asyncio.get_event_loop().time())}"
            )

        try:
            order = await self.client.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                params={"marginMode": "cross"}
            )
            filled = float(order.get("filled", amount))
            avg_p = float(order.get("average", price or 0.0))
            fee = float(order.get("fee", {}).get("cost", 0.0) if order.get("fee") else (filled * avg_p * settings.PERP_TAKER_FEE))

            return OrderExecutionResult(
                success=True,
                market_type="perp",
                symbol=symbol,
                side=side,
                requested_amount=amount,
                filled_amount=filled,
                avg_price=avg_p,
                fee_amount=fee,
                order_id=str(order.get("id"))
            )
        except Exception as e:
            log.error(f"[Order Perp] Gagal {side} {amount} {symbol}: {e}")
            return OrderExecutionResult(
                success=False,
                market_type="perp",
                symbol=symbol,
                side=side,
                requested_amount=amount,
                error_message=str(e)
            )

bitget_client = BitgetClient()
