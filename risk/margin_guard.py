import asyncio
from typing import Dict, Any

from config.settings import settings
from utils.logger import log
from utils.notifier import notifier
from core.bitget_client import BitgetClient, bitget_client
from execution.position_manager import PositionManager, position_manager
from execution.order_executor import OrderExecutor, order_executor

class MarginGuard:
    """
    Penjaga Margin & Pencegah Likuidasi:
    1. Memantau pergerakan harga pada kaki Short Futures.
    2. Menghitung Margin Ratio dan jarak terhadap harga likuidasi.
    3. Memicu peringatan dini atau auto-close darurat jika harga meroket tajam
       guna melindungi akun dari forced liquidation fee exchange.
    """

    def __init__(
        self,
        client: BitgetClient = bitget_client,
        pos_mgr: PositionManager = position_manager,
        executor: OrderExecutor = order_executor
    ):
        self.client = client
        self.pos_mgr = pos_mgr
        self.executor = executor

    async def check_positions_health(self):
        """Memeriksa kesehatan margin untuk seluruh posisi aktif."""
        active_positions = self.pos_mgr.get_active_positions()
        if not active_positions:
            return

        swap_tickers = await self.client.fetch_tickers_by_type("swap")
        spot_tickers = await self.client.fetch_tickers_by_type("spot")

        for pos in active_positions:
            perp_sym = pos.perp_leg.symbol
            spot_sym = pos.spot_leg.symbol

            perp_t = swap_tickers.get(perp_sym)
            spot_t = spot_tickers.get(spot_sym)

            if not perp_t or not spot_t:
                continue

            current_perp_p = float(perp_t.get("last") or pos.perp_leg.entry_price)
            current_spot_p = float(spot_t.get("last") or pos.spot_leg.entry_price)

            # Update harga terkini dan unrealized PnL
            pos.perp_leg.current_price = current_perp_p
            pos.spot_leg.current_price = current_spot_p

            # Kaki spot: Long (profit saat harga naik)
            pos.spot_leg.unrealized_pnl = round(
                (current_spot_p - pos.spot_leg.entry_price) * pos.spot_leg.amount, 4
            )
            # Kaki perp: Short (profit saat harga turun, rugi saat harga naik)
            pos.perp_leg.unrealized_pnl = round(
                (pos.perp_leg.entry_price - current_perp_p) * pos.perp_leg.amount, 4
            )

            # Net unrealized PnL (karena delta neutral, harusnya saling mengimbangi)
            net_unrealized = pos.spot_leg.unrealized_pnl + pos.perp_leg.unrealized_pnl

            # Estimasi Margin Ratio untuk Kaki Short
            # Pada leverage L, margin awal = 1 / L. Jika harga naik delta_p %, margin tergerus.
            entry_p = pos.perp_leg.entry_price
            price_surge_pct = (current_perp_p - entry_p) / entry_p if entry_p > 0 else 0.0

            # Estimasi margin ratio (0.0 aman, mendekati 1.0 = likuidasi)
            # Rumus perkiraan untuk short: (Kerugian belum terealisasi + Maintenance Margin) / Margin Awal
            initial_margin_rate = 1.0 / pos.leverage
            maint_margin_rate = 0.05  # Standar Bitget ~0.5% - 5%
            margin_ratio = (price_surge_pct + maint_margin_rate) / initial_margin_rate
            pos.current_margin_ratio = round(max(0.0, margin_ratio), 4)

            # Estimasi harga likuidasi (saat margin terkuras)
            est_liq_price = entry_p * (1.0 + (initial_margin_rate - maint_margin_rate))
            pos.liquidation_price = round(est_liq_price, 4)

            self.pos_mgr.update_position(pos)

            log.info(
                f"[MarginGuard] {pos.base_asset} -> Harga: ${current_spot_p:,.4f} | "
                f"Net uPnL: ${net_unrealized:+.4f} | Margin Ratio: {pos.current_margin_ratio:.1%} | "
                f"Est. Liq Price: ${pos.liquidation_price:,.4f}"
            )

            # 1. Peringatan jika margin ratio mendekati ambang batas peringatan (e.g. 85%)
            warn_threshold = getattr(settings, "MARGIN_CALL_THRESHOLD", 0.85)
            if pos.current_margin_ratio >= warn_threshold:
                warn_msg = (
                    f"⚠️ *[MARGIN WARNING]*\n"
                    f"Posisi: `{pos.base_asset}`\n"
                    f"Margin Ratio saat ini: `{pos.current_margin_ratio:.1%}` (Peringatan: {warn_threshold:.0%})\n"
                    f"Harga Saat Ini: `${current_perp_p:,.4f}`\n"
                    f"Harga Likuidasi: `${pos.liquidation_price:,.4f}`\n"
                    f"Auto-close darurat aktif jika menyentuh 95%!"
                )
                log.warning(warn_msg.replace("*", "").replace("`", ""))
                await notifier.send_message(warn_msg)

            # 2. Proteksi Ekstrem: Auto-close jika margin ratio melebihi 95% (mencegah penalty likuidasi exchange)
            auto_close_threshold = getattr(settings, "AUTO_CLOSE_MARGIN_RATIO", 0.95)
            if pos.current_margin_ratio >= auto_close_threshold:
                log.critical(
                    f"🚨 [EMERGENCY DELEVERAGE] Margin ratio untuk {pos.base_asset} mencapai "
                    f"{pos.current_margin_ratio:.1%} (>= {auto_close_threshold:.0%}). "
                    f"Menutup posisi secara otomatis untuk melindungi modal!"
                )
                await self.executor.close_delta_neutral_position(
                    position_id=pos.position_id,
                    reason=f"Margin Ratio Kritis ({pos.current_margin_ratio:.1%} >= {auto_close_threshold:.0%})"
                )

margin_guard = MarginGuard()
