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
                        # Tarik Return On Equity (ROE) riil langsung dari Bitget Futures (persentase)
                        raw_roe = matched_bitget_pos.get("percentage")
                        if raw_roe is None:
                            raw_roe = matched_bitget_pos.get("info", {}).get("unrealizedPLR")

                        if raw_mmr > 0.0:
                            pos.current_margin_ratio = round(raw_mmr, 4)
                        if raw_liq > 0.0:
                            pos.liquidation_price = round(raw_liq, 4)
                        if raw_roe is not None:
                            pos.current_roe_percent = round(float(raw_roe), 2)
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
                        perp_margin_est = (pos.perp_leg.amount * entry_p) / lev if lev > 0 else 1.0
                        pos.current_roe_percent = round((pos.perp_leg.unrealized_pnl / max(0.01, perp_margin_est)) * 100.0, 2)

                    self.pos_mgr.update_position(pos)

                    log.info(
                        f"[RiskGuard] {pos.base_asset} -> Spot: ${current_spot_p:,.4f} | Perp: ${current_perp_p:,.4f} | "
                        f"Net uPnL: ${net_unrealized:+.4f} | ROE Futures: {pos.current_roe_percent:+.2f}% | "
                        f"MMR Bitget: {pos.current_margin_ratio:.1%} | Liq Price: ${pos.liquidation_price:,.4f}"
                    )

                    # 2. Proteksi Darurat: BATALKAN DELTA NEUTRAL JIKA ROE FUTURES MINUS >= 85% (ATAU MMR >= 85%)
                    auto_close_threshold = getattr(settings, "AUTO_CLOSE_MARGIN_RATIO", 0.85)
                    max_roe_loss_pct = getattr(settings, "FUTURES_MAX_ROE_LOSS_PERCENT", 85.0)
                    
                    perp_margin = (getattr(pos.perp_leg, "nominal_usdt", 0.0) or (pos.perp_leg.amount * pos.perp_leg.entry_price)) / max(1, getattr(pos, "leverage", 1))
                    perp_unrealized_pnl = getattr(pos.perp_leg, "unrealized_pnl", 0.0)
                    calc_roe = (perp_unrealized_pnl / perp_margin * 100.0) if perp_margin > 0 else 0.0
                    effective_roe = pos.current_roe_percent if pos.current_roe_percent != 0.0 else calc_roe

                    is_mmr_critical = pos.current_margin_ratio >= auto_close_threshold
                    is_roe_critical = effective_roe <= -max_roe_loss_pct or perp_unrealized_pnl <= (-(max_roe_loss_pct / 100.0) * perp_margin)

                    if is_mmr_critical or is_roe_critical:
                        log.critical(
                            f"🚨 [BATALKAN DELTA NEUTRAL - ROE/MMR MELEWATI 85%] Kaki Futures {pos.base_asset} "
                            f"melebihi batas toleransi 85%! ROE Bitget: {effective_roe:+.2f}% (Batas: -{max_roe_loss_pct:.0f}%), "
                            f"MMR Bitget: {pos.current_margin_ratio:.1%} (Batas: {auto_close_threshold:.0%}), "
                            f"Perp uPnL: ${perp_unrealized_pnl:+.4f} / Margin ${perp_margin:.2f}. "
                            f"Membatalkan delta neutral dengan Limit Order Maker & spread positif untuk menyelamatkan modal!"
                        )
                        cancel_msg = (
                            f"🚨 *[BATALKAN DELTA NEUTRAL - ROE/MMR MELEWATI 85%]*\n"
                            f"Posisi: `{pos.base_asset}`\n"
                            f"ROE Futures Bitget: `{effective_roe:+.2f}%` (Batas Maksimal Minus: -{max_roe_loss_pct:.0f}%)\n"
                            f"MMR Bitget: `{pos.current_margin_ratio:.1%}` (Batas: {auto_close_threshold:.0%})\n"
                            f"Futures uPnL: `${perp_unrealized_pnl:+.4f} USDT` / Margin `${perp_margin:.2f} USDT`\n"
                            f"Tindakan: Membatalkan delta neutral dan menutup posisi via Limit Order Maker dengan spread positif!"
                        )
                        await notifier.send_message(cancel_msg)
                        await self.executor.close_delta_neutral_position(
                            position_id=pos.position_id,
                            reason=f"BATALKAN_DELTA_NEUTRAL_ROE_MMR_85% (ROE: {effective_roe:+.1f}%, MMR: {pos.current_margin_ratio:.1%})"
                        )
                        continue

                    # Peringatan dini jika margin ratio atau ROE mendekati ambang batas (>= 75%)
                    warn_threshold = getattr(settings, "MARGIN_CALL_THRESHOLD", 0.75)
                    if pos.current_margin_ratio >= warn_threshold or effective_roe <= -(warn_threshold * 100.0):
                        warn_msg = (
                            f"⚠️ *[MARGIN & ROE WARNING]*\n"
                            f"Posisi: `{pos.base_asset}`\n"
                            f"MMR Bitget saat ini: `{pos.current_margin_ratio:.1%}` (Peringatan: {warn_threshold:.0%})\n"
                            f"ROE Futures saat ini: `{effective_roe:+.2f}%`\n"
                            f"Harga Saat Ini: `${current_perp_p:,.4f}`\n"
                            f"Harga Likuidasi: `${pos.liquidation_price:,.4f}`\n"
                            f"Jika menyentuh 85%, posisi delta neutral akan dibatalkan otomatis via Maker Limit Order!"
                        )
                        log.warning(warn_msg.replace("*", "").replace("`", ""))
                        await notifier.send_message(warn_msg)

                    # 3. MONITOR & PASTIKAN BASIS SPREAD TETAP POSITIF
                    # Menjamin harga perp tidak berada di bawah spot (perp >= spot)
                    spot_bid = float(spot_t.get("bid") or spot_t.get("last") or current_spot_p)
                    perp_ask = float(perp_t.get("ask") or perp_t.get("last") or current_perp_p)
                    basis_spread_pct = ((perp_ask - spot_bid) / spot_bid) * 100.0 if spot_bid > 0 else 0.0

                    spread_status = "POSITIF (AMAN)" if basis_spread_pct >= 0.0 else "NEGATIF (WASPADA)"
                    log.info(
                        f"[Spread Monitor] {pos.base_asset} -> Spot Bid: ${spot_bid:,.4f} | Perp Ask: ${perp_ask:,.4f} | "
                        f"Basis Spread: {basis_spread_pct:+.3f}% -> Status Spread: {spread_status}"
                    )

                    if basis_spread_pct < -0.50:
                        log.warning(
                            f"⚠️ [Spread Warning] Basis spread {pos.base_asset} tertekan negatif ({basis_spread_pct:+.2f}%). "
                            f"Memantau konvergensi harga agar spread kembali positif."
                        )
                except Exception as pos_err:
                    log.error(f"[MarginGuard] Kendala periksa posisi {getattr(pos, 'base_asset', 'UNKNOWN')}: {pos_err}")
        except Exception as e:
            log.error(f"[MarginGuard] Gagal memeriksa kesehatan margin: {e}")

margin_guard = MarginGuard()
