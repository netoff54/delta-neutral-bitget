import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional
import ccxt.async_support as ccxt

from config.settings import settings
from utils.logger import log
from utils.dns_resolver import patch_dns
from utils.interval_helper import parse_interval_hours, detect_empirical_interval
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
        Mengambil funding rate terkini beserta interval penyelesaian dinamis (1h, 4h, 8h)
        dan timestamp pembayaran berikutnya langsung dari endpoint Bitget V2.
        """
        try:
            res = await self.client.publicMixGetV2MixMarketCurrentFundRate({"productType": "USDT-FUTURES"})
            data = res.get("data", [])
            result = {}
            for item in data:
                raw_sym = item.get("symbol", "")
                interval_raw = (
                    item.get("fundingRateInterval") or
                    item.get("ratePeriod") or
                    item.get("fundingInterval") or
                    item.get("fundInterval") or
                    item.get("settleInterval")
                )
                interval_hours = parse_interval_hours(interval_raw, default=8)
                rate = float(item.get("fundingRate", 0.0) or 0.0)
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
                    "fundingRate": float(r.get("fundingRate", 0.0) or 0.0),
                    "fundingInterval": parse_interval_hours(
                        r.get("info", {}).get("fundingRateInterval") or
                        r.get("info", {}).get("ratePeriod") or
                        r.get("interval"),
                        default=8
                    ),
                    "nextUpdate": r.get("fundingTimestamp")
                }
                for sym, r in fallback_rates.items()
            }

    async def fetch_funding_rate_history(
        self,
        symbol: str,
        limit: int = 100,
        since: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Mengambil riwayat funding rate untuk pasangan tertentu (hingga 7 hari ke belakang)."""
        try:
            return await self.client.fetch_funding_rate_history(symbol, since=since, limit=limit)
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
        SECARA KETAT MENGECEK:
        1. Minimal Token Lot Size (limits.amount.min)
        2. Minimal Nilai Nominal USDT (limits.cost.min, misal $5 USDT di Futures Bitget)
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
            aligned_qty = float(formatted_str)
        else:
            aligned_qty = round(aligned_qty, 6)

        # Cek Batasan Minimal Nominal USDT (limits.cost.min):
        # Di Bitget Futures, order bernilai di bawah $5.0 USDT akan ditolak exchange (Error 45110).
        perp_cost_min = float(perp_market.get("limits", {}).get("cost", {}).get("min", 5.0) or 5.0) if perp_market else 5.0
        spot_cost_min = float(spot_market.get("limits", {}).get("cost", {}).get("min", 1.0) or 1.0) if spot_market else 1.0
        perp_cost_min = max(5.0, perp_cost_min)
        spot_cost_min = max(1.0, spot_cost_min)

        notional = aligned_qty * price
        if notional < perp_cost_min or notional < spot_cost_min:
            log.debug(
                f"[Market Limit] Nominal {aligned_qty} token (${notional:.2f} USDT) di bawah batas minimal "
                f"(Futures Min: ${perp_cost_min:.2f}, Spot Min: ${spot_cost_min:.2f})"
            )
            return 0.0

        return aligned_qty

    def get_min_order_cost(self, spot_symbol: str, perp_symbol: str) -> Dict[str, float]:
        """Mendapatkan batas minimal order notional (dalam USDT) untuk Spot dan Futures."""
        perp_market = self.get_market(perp_symbol)
        spot_market = self.get_market(spot_symbol)

        perp_cost_min = float(perp_market.get("limits", {}).get("cost", {}).get("min", 5.0) or 5.0) if perp_market else 5.0
        spot_cost_min = float(spot_market.get("limits", {}).get("cost", {}).get("min", 1.0) or 1.0) if spot_market else 1.0
        perp_cost_min = max(5.0, perp_cost_min)
        spot_cost_min = max(1.0, spot_cost_min)

        # Required nominal per kaki dan total modal minimal yang dibutuhkan sesuai leverage
        required_leg = max(spot_cost_min, perp_cost_min) + 0.10
        required_capital = required_leg * (1.0 + (1.0 / max(1, settings.LEVERAGE)))

        return {
            "spot_cost_min": spot_cost_min,
            "perp_cost_min": perp_cost_min,
            "required_leg_usdt": required_leg,
            "required_min_capital_usdt": round(required_capital, 2)
        }

    async def get_funding_settlement_countdown(self, perp_symbol: str) -> Dict[str, Any]:
        """
        Mengambil waktu settlement funding terdekat dan menghitung countdown serta
        rate terkini yang diproyeksikan akan dibayarkan exchange dalam 5 menit ke depan.
        Mendukung interval dinamis (1h, 4h, 8h).
        """
        clean_sym = perp_symbol.split('/')[0].upper() + "USDT"
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        default_res = {
            "symbol": clean_sym,
            "current_projected_rate": 0.0,
            "funding_interval_hours": 8,
            "next_settlement_timestamp_ms": now_ms + (8 * 3600 * 1000),
            "seconds_until_settlement": 8 * 3600,
            "minutes_until_settlement": 480.0,
            "is_near_settlement": False
        }
        if settings.DRY_RUN:
            return default_res

        try:
            res = await self.client.publicMixGetV2MixMarketCurrentFundRate({
                "productType": "USDT-FUTURES",
                "symbol": clean_sym
            })
            data_list = res.get("data", [])
            if data_list:
                item = data_list[0]
                rate = float(item.get("fundingRate", 0.0) or 0.0)
                interval_raw = (
                    item.get("fundingRateInterval") or
                    item.get("ratePeriod") or
                    item.get("fundingInterval") or
                    item.get("settleInterval")
                )
                interval_hours = parse_interval_hours(interval_raw, default=8)
                next_update_ms = int(item.get("nextUpdate", now_ms + (interval_hours * 3600 * 1000)))
                diff_ms = max(0, next_update_ms - now_ms)
                seconds_left = diff_ms / 1000.0
                window_seconds = settings.PRE_SETTLEMENT_CHECK_MINUTES * 60

                return {
                    "symbol": clean_sym,
                    "current_projected_rate": rate,
                    "funding_interval_hours": interval_hours,
                    "next_settlement_timestamp_ms": next_update_ms,
                    "seconds_until_settlement": seconds_left,
                    "minutes_until_settlement": round(seconds_left / 60.0, 1),
                    "is_near_settlement": (seconds_left <= window_seconds)
                }
        except Exception as e:
            log.debug(f"Gagal mengambil countdown settlement untuk {clean_sym}: {e}")

        return default_res

    async def fetch_realized_funding_fee(self, base_asset: str, since_timestamp_ms: Optional[int] = None) -> float:
        """
        Mengambil total pemasukan riil funding fee yang dibayarkan oleh exchange ke akun
        berdasarkan riwayat tagihan ledger Bitget (/api/v2/mix/account/bill).
        """
        if settings.DRY_RUN:
            return 0.0
        try:
            params = {
                "productType": "USDT-FUTURES",
                "symbol": f"{base_asset.upper()}USDT",
                "pageSize": "50"
            }
            if since_timestamp_ms:
                params["startTime"] = str(since_timestamp_ms)

            res = await self.client.privateMixGetV2MixAccountBill(params)
            bills = res.get("data", {}).get("bills", [])
            total_funding = 0.0
            for b in bills:
                b_type = str(b.get("businessType", "")).lower()
                if "contract_settle_fee" in b_type or "funding" in b_type:
                    amt = float(b.get("amount", 0.0) or 0.0)
                    total_funding += amt
            return round(total_funding, 6)
        except Exception as e:
            log.debug(f"Gagal mengambil funding fee ledger untuk {base_asset}: {e}")
            return 0.0

    async def update_position_live_pnl(self, pos: Any) -> Any:
        """
        Memperbarui status PnL riil (Spot Unrealized, Futures Unrealized, Realized Funding, Net PnL, dan status BEP).
        """
        if not pos or pos.status != "OPEN":
            return pos

        try:
            # Ambil harga terkini Spot dan Perp
            spot_ticker = await self.client.fetch_ticker(pos.spot_leg.symbol)
            perp_ticker = await self.client.fetch_ticker(pos.perp_leg.symbol)

            spot_curr_p = float(spot_ticker.get("bid") or spot_ticker.get("last") or pos.spot_leg.entry_price)
            perp_curr_p = float(perp_ticker.get("ask") or perp_ticker.get("last") or pos.perp_leg.entry_price)

            pos.spot_leg.current_price = spot_curr_p
            pos.perp_leg.current_price = perp_curr_p

            # Hitung Unrealized PnL Kaki Spot: Long = (now - entry) * qty
            spot_pnl = (spot_curr_p - pos.spot_leg.entry_price) * pos.spot_leg.amount
            pos.spot_leg.unrealized_pnl = round(spot_pnl, 4)

            # Hitung Unrealized PnL Kaki Perp: Short = (entry - now) * qty
            perp_pnl = (pos.perp_leg.entry_price - perp_curr_p) * pos.perp_leg.amount
            pos.perp_leg.unrealized_pnl = round(perp_pnl, 4)

            total_unrealized = spot_pnl + perp_pnl
            pos.unrealized_pnl_usdt = round(total_unrealized, 4)

            # Ambil Realized Funding Fee dari Ledger jika live
            entry_ts_ms = int(pos.entry_time.timestamp() * 1000)
            real_funding = await self.fetch_realized_funding_fee(pos.base_asset, since_timestamp_ms=entry_ts_ms)
            if real_funding != 0.0:
                pos.realized_funding_usdt = real_funding
                pos.cumulative_funding_received = real_funding

            # Estimasi biaya penutupan (exit taker fees: Spot 0.1%, Futures 0.06%)
            est_exit_fees = (pos.spot_leg.amount * spot_curr_p * 0.0010) + (pos.perp_leg.amount * perp_curr_p * 0.0006)
            total_roundtrip_fees = pos.total_fees_paid + est_exit_fees

            # Net PnL Bersih = Unrealized PnL + Realized Funding Fee - Total Biaya Transaksi Round-Trip
            net_pnl = pos.unrealized_pnl_usdt + pos.cumulative_funding_received - total_roundtrip_fees
            pos.net_pnl_usdt = round(net_pnl, 4)

            # Status BEP tercapai jika Net PnL > 0 (Funding fee riil sudah melampaui seluruh biaya round-trip)
            pos.is_bep_reached = (pos.net_pnl_usdt > 0.0)

        except Exception as e:
            log.debug(f"Gagal update live PnL posisi {pos.base_asset}: {e}")

        return pos

    async def set_leverage_for_symbol(self, perp_symbol: str, leverage: int = settings.LEVERAGE):
        """Mengatur leverage untuk posisi futures dan memastikan position mode satu arah (one_way_mode)."""
        if settings.DRY_RUN:
            return
        try:
            try:
                await self.client.set_position_mode(False, perp_symbol)
            except Exception as pe:
                log.debug(f"Position mode sync notice: {pe}")
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
            params = {}
            if order_type == "market" and side == "buy" and (price is None or price <= 0):
                try:
                    ticker = await self.client.fetch_ticker(symbol)
                    price = float(ticker.get("ask") or ticker.get("last") or 0.0)
                except Exception as te:
                    log.warning(f"Gagal mengambil live ticker untuk market buy {symbol}: {te}")

            order = await self.client.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=params
            )
            filled_val = order.get("filled")
            filled = float(filled_val) if filled_val is not None else float(amount)
            avg_val = order.get("average")
            avg_p = float(avg_val) if avg_val is not None else float(price or 0.0)
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
        price: Optional[float] = None,
        reduce_only: bool = False
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
            params = {"marginMode": "cross"}
            if reduce_only:
                params["reduceOnly"] = True

            order = await self.client.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=params
            )
            filled_val = order.get("filled")
            filled = float(filled_val) if filled_val is not None else float(amount)
            avg_val = order.get("average")
            avg_p = float(avg_val) if avg_val is not None else float(price or 0.0)
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
