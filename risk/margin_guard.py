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
        """Memeriksa kesehatan margin untuk seluruh posisi aktif dengan proteksi exception penuh."""
        try:
            active_positions = self.pos_mgr.get_active_positions()
            if not active_positions:
                return

            swap_tickers = await self.client.fetch_tickers_by_type("swap")
            spot_tickers = await self.client.fetch_tickers_by_type("spot")

            if not swap_tickers and not spot_tickers:
                log.warning("[MarginGuard] Tidak dapat mengambil data tickers saat ini. Menunda pemeriksaan margin.")
                return

            # Ambil data posisi riil langsung dari Bitget Futures (MMR & Liq Price resmi Bitget)
            live_futures = []
            try:
                live_futures = await self.client.fetch_positions()
            except Exception as fpe:
                log.debug(f"[MarginGuard] Fetch futures positions notice: {fpe}")

            for pos in active_positions:
                try:
                    perp_sym = pos.perp_leg.symbol
                    spot_sym = pos.spot_leg.symbol

                    perp_t = swap_tickers.get(perp_sym, {})
                    spot_t = spot_tickers.get(spot_sym, {})

                    if not perp_t and not spot_t:
                        continue

                    current_perp_p = float(perp_t.get("last") or perp_t.get("close") or pos.perp_leg.entry_price or 1.0)
                    current_spot_p = float(spot_t.get("last") or spot_t.get("close") or pos.spot_leg.entry_price or 1.0)

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

                    # 1. Tarik Maintenance Margin Ratio (MMR) & Harga Likuidasi Riil dari Akun Bitget
                    matched_bitget_pos = next(
                        (p for p in live_futures if p.get("symbol") == perp_sym or p.get("symbol", "").startswith(pos.base_asset)),
                        None
                    )
                    if matched_bitget_pos:
                        raw_mmr = float(
                            matched_bitget_pos.get("marginRatio") or
                            matched_bitget_pos.get("info", {}).get("marginRatio") or
                            0.0
                        )
                        raw_liq = float(
                            matched_bitget_pos.get("liquidationPrice") or
                            matched_bitget_pos.get("info", {}).get("liquidationPrice") or
                            0.0
                        )
                        if raw_mmr > 0.0:
                            pos.current_margin_ratio = round(raw_mmr, 4)
                        if raw_liq > 0.0:
                            pos.liquidation_price = round(raw_liq, 4)
                    else:
                        # Fallback estimasi jika API data posisi sementara delay
                        entry_p = pos.perp_leg.entry_price
                        price_surge_pct = (current_perp_p - entry_p) / entry_p if entry_p > 0 else 0.0
                        lev = max(1, getattr(pos, "leverage", 1) or 1)
                        initial_margin_rate = 1.0 / lev
                        maint_margin_rate = 0.025  # Standar Bitget keepMarginRate ~2.5%
                        margin_ratio = (price_surge_pct + maint_margin_rate) / initial_margin_rate
                        pos.current_margin_ratio = round(max(0.0, margin_ratio), 4)
                        est_liq_price = entry_p * (1.0 + (initial_margin_rate - maint_margin_rate))
                        pos.liquidation_price = round(est_liq_price, 4)

                    self.pos_mgr.update_position(pos)

                    log.info(
                        f"[RiskGuard] {pos.base_asset} -> Spot: ${current_spot_p:,.4f} | Perp: ${current_perp_p:,.4f} | "
                        f"Net uPnL: ${net_unrealized:+.4f} | MMR Bitget: {pos.current_margin_ratio:.1%} | "
                        f"Liq Price: ${pos.liquidation_price:,.4f}"
                    )

                    # 2. Proteksi Darurat Likuidasi: Auto-close jika MMR Bitget mencapai >= 90%
                    auto_close_threshold = getattr(settings, "AUTO_CLOSE_MARGIN_RATIO", 0.90)
                    if pos.current_margin_ratio >= auto_close_threshold:
                        log.critical(
                            f"🚨 [EMERGENCY DELEVERAGE - MMR >= 90%] Maintenance Margin Ratio (MMR) {pos.base_asset} "
                            f"di Bitget mencapai {pos.current_margin_ratio:.1%} (>= {auto_close_threshold:.0%}). "
                            f"Menutup posisi secara instan untuk melindungi modal dari likuidasi!"
                        )
                        await self.executor.close_delta_neutral_position(
                            position_id=pos.position_id,
                            reason=f"MMR Kritis Bitget ({pos.current_margin_ratio:.1%} >= {auto_close_threshold:.0%})"
                        )
                        continue

                    # Peringatan dini jika margin ratio mendekati ambang batas (>= 80%)
                    warn_threshold = getattr(settings, "MARGIN_CALL_THRESHOLD", 0.80)
                    if pos.current_margin_ratio >= warn_threshold:
                        warn_msg = (
                            f"⚠️ *[MARGIN WARNING]*\n"
                            f"Posisi: `{pos.base_asset}`\n"
                            f"MMR Bitget saat ini: `{pos.current_margin_ratio:.1%}` (Peringatan: {warn_threshold:.0%})\n"
                            f"Harga Saat Ini: `${current_perp_p:,.4f}`\n"
                            f"Harga Likuidasi: `${pos.liquidation_price:,.4f}`\n"
                            f"Auto-close darurat otomatis aktif jika menyentuh 90%!"
                        )
                        log.warning(warn_msg.replace("*", "").replace("`", ""))
                        await notifier.send_message(warn_msg)

                    # 3. HARD TAKE PROFIT (TP) & HARD STOP LOSS (SL) OTOMATIS (TANPA MENUNGGU AI)
                    # Memberikan proteksi seketika saat terjadi volatilitas tinggi di Spot / Futures
                    pos_capital = getattr(pos.spot_leg, "nominal_usdt", 0.0) + getattr(pos.perp_leg, "nominal_usdt", 0.0)
                    if pos_capital <= 0.0:
                        pos_capital = (pos.spot_leg.amount * current_spot_p) + (pos.perp_leg.amount * current_perp_p)
                    pos_half_cap = max(10.0, pos_capital / 2.0)

                    tp_pct = getattr(settings, "HARD_TAKE_PROFIT_PERCENT", 0.05)
                    sl_pct = getattr(settings, "HARD_STOP_LOSS_PERCENT", 0.05)

                    # Basis spread saat exit (harga jual spot vs harga beli futures)
                    spot_bid = float(spot_t.get("bid") or spot_t.get("last") or current_spot_p)
                    perp_ask = float(perp_t.get("ask") or perp_t.get("last") or current_perp_p)
                    exit_spread_pct = ((spot_bid - perp_ask) / perp_ask) * 100.0 if perp_ask > 0 else 0.0

                    # A. Hard Take Profit (TP):
                    # Terpicu jika BEP tercapai dan profit bersih >= 5% modal posisi
                    target_tp_usdt = pos_half_cap * tp_pct
                    if pos.is_bep_reached and pos.net_pnl_usdt >= target_tp_usdt:
                        # Jamin spread keluar positif (Spot Bid >= Perp Ask) agar profit tidak tergerus
                        if getattr(settings, "REQUIRE_POSITIVE_SPREAD", True) and exit_spread_pct < -0.15:
                            log.info(
                                f"🎯 [TP Standby] Target profit +${pos.net_pnl_usdt:.4f} USDT terpenuhi, namun spread "
                                f"saat ini {exit_spread_pct:+.2f}%. Menunggu konvergensi spread positif sebelum mengunci profit."
                            )
                        else:
                            log.info(
                                f"🎯 [HARD TAKE PROFIT DIPICU] Posisi {pos.base_asset} mencapai target TP: "
                                f"Net PnL +${pos.net_pnl_usdt:.4f} USDT (>= {tp_pct:.0%}). Spread keluar: {exit_spread_pct:+.2f}%. "
                                f"Mengunci profit secara otomatis!"
                            )
                            await self.executor.close_delta_neutral_position(
                                position_id=pos.position_id,
                                reason=f"HARD_TAKE_PROFIT (+${pos.net_pnl_usdt:.4f} USDT >= {tp_pct:.0%})"
                            )
                            continue

                    # B. Hard Stop Loss (SL) Volatilitas Ekstrem:
                    # Terpicu seketika jika anomali divergensi ekstrem membuat Net PnL rugi melebihi 5% modal
                    max_sl_usdt = -(pos_half_cap * sl_pct)
                    if pos.net_pnl_usdt <= max_sl_usdt:
                        log.critical(
                            f"🛑 [HARD STOP LOSS DIPICU] Posisi {pos.base_asset} menyentuh batas kerugian maksimal: "
                            f"Net PnL ${pos.net_pnl_usdt:.4f} USDT (<= -{sl_pct:.0%}). "
                            f"Menutup kedua kaki secara instan untuk melindungi modal dari volatilitas liar!"
                        )
                        await self.executor.close_delta_neutral_position(
                            position_id=pos.position_id,
                            reason=f"HARD_STOP_LOSS (Net PnL ${pos.net_pnl_usdt:.4f} <= -{sl_pct:.0%})"
                        )
                        continue
                except Exception as pos_err:
                    log.error(f"[MarginGuard] Kendala periksa posisi {getattr(pos, 'base_asset', 'UNKNOWN')}: {pos_err}")
        except Exception as e:
            log.error(f"[MarginGuard] Gagal memeriksa kesehatan margin: {e}")

margin_guard = MarginGuard()
