import asyncio
from datetime import datetime
from typing import List, Optional

from config.settings import settings
from utils.logger import log
from utils.notifier import notifier
from core.models import Opportunity, DeltaNeutralPosition
from utils.interval_helper import safe_hours_passed
from core.bitget_client import BitgetClient, bitget_client
from execution.position_manager import PositionManager, position_manager
from execution.order_executor import OrderExecutor, order_executor

class AutoRebalancer:
    """
    Modul Auto-Rebalancing & Dynamic Opportunity Rotation:
    1. Rebalancing Delta: Memastikan delta Spot Long vs Futures Short tetap mendekati 0.
    2. Dynamic Opportunity Rotation ('Mencari Taker Lain yang Memiliki Potensial'):
       Secara berkala membandingkan performa posisi aktif dengan peluang terbaru di pasar.
       PENTING: Rotasi HANYA terjadi jika ada keuntungan nyata di atas BEP (bukan hanya BEP=0),
       agar modal benar-benar tumbuh dan tidak hanya berputar tanpa pertumbuhan.
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

    async def rebalance_delta_drift(self):
        """
        Memeriksa dan memperbaiki penyimpangan delta (Delta Drift) pada seluruh posisi aktif.
        """
        if not settings.AUTO_REBALANCE_DELTA:
            return

        active_positions = self.pos_mgr.get_active_positions()
        for pos in active_positions:
            drift = pos.spot_leg.amount - pos.perp_leg.amount
            pos.net_delta = round(drift, 8)

            # Jika drift melebihi batas toleransi nilai (misal > $1 USD ekuivalen)
            drift_usdt = abs(drift * pos.spot_leg.current_price)
            if drift_usdt >= 2.0:
                log.warning(
                    f"⚖️ [Delta Drift Terdeteksi] {pos.base_asset}: Drift = {drift:+.4f} token (~${drift_usdt:.2f}). "
                    f"Melakukan kalibrasi rebalancing..."
                )
                if drift > 0:
                    # Spot lebih banyak dari short -> Tambah Short Perp
                    log.info(f"-> Menambah Short Perp {pos.perp_leg.symbol} sejumlah {drift:.4f}...")
                    res = await self.client.execute_perp_order(
                        symbol=pos.perp_leg.symbol,
                        side="sell",
                        amount=abs(drift),
                        order_type="market"
                    )
                    if res.success:
                        pos.perp_leg.amount += res.filled_amount
                else:
                    # Short lebih banyak dari spot -> Tambah Beli Spot
                    log.info(f"-> Menambah Beli Spot {pos.spot_leg.symbol} sejumlah {abs(drift):.4f}...")
                    res = await self.client.execute_spot_order(
                        symbol=pos.spot_leg.symbol,
                        side="buy",
                        amount=abs(drift),
                        order_type="market"
                    )
                    if res.success:
                        pos.spot_leg.amount += res.filled_amount

                pos.net_delta = round(pos.spot_leg.amount - pos.perp_leg.amount, 8)
                self.pos_mgr.update_position(pos)
                log.info(f"✅ Rebalancing selesai untuk {pos.base_asset}. Delta baru: {pos.net_delta}")

    async def evaluate_and_rotate(
        self,
        opportunities: List[Opportunity],
        capital_per_position: Optional[float] = None
    ):
        """
        Evaluasi rotasi peluang dengan logika pertumbuhan modal yang benar:

        FILOSOFI ROTASI YANG MENGHASILKAN PERTUMBUHAN NYATA:
        =====================================================
        Masalah umum: Setelah BEP (Net PnL = 0), langsung rotasi → bayar fee lagi → 
        uang hanya berputar tanpa berkembang.

        Solusi: Rotasi hanya dilakukan jika:
        1. BEP sudah tercapai DAN
        2. Ada profit NYATA di atas BEP (minimal MIN_PROFIT_BEFORE_ROTATION_USDT) DAN
        3. Pasar baru harus jauh lebih baik (MIN_ROTATION_APY_DIFF) DAN
        4. Funding rate koin baru harus STABIL (konsistensi > 75%) DAN
        5. Estimasi waktu BEP di koin baru < waktu yang sudah diraih sekarang
        
        Dengan cara ini modal berkembang: Profit dari koin lama dijaga, 
        dan koin baru menghasilkan profit lebih tinggi.
        """
        if not settings.AUTO_ROTATE_OPPORTUNITIES:
            return

        from risk.compounding_manager import compounding_manager
        if capital_per_position is None or capital_per_position <= 0:
            capital_per_position = compounding_manager.get_position_capital()

        active_positions = self.pos_mgr.get_active_positions()
        if not active_positions:
            return

        eligible_opps = [o for o in opportunities if o.is_eligible and o.current_funding_rate > 0.0]
        if not eligible_opps:
            return

        from core.gemini_brain import gemini_brain
        best_new_opp = await gemini_brain.select_optimal_taker_agi(eligible_opps, capital_per_position) or eligible_opps[0]

        # Jangan rotasi ke koin yang sama
        for pos in active_positions:
            if pos.base_asset == best_new_opp.base_asset:
                log.info(f"[Rotasi] Koin terbaik saat ini sudah sama dengan posisi aktif ({pos.base_asset}). Skip rotasi.")
                continue

            holding_hours = safe_hours_passed(pos.entry_time)

            # Sinkronisasi status PnL riil dan funding fee dari ledger
            await self.client.update_position_live_pnl(pos)

            # ================================================================
            # SYARAT ROTASI KETAT - Mencegah Fee Churn & Memastikan Pertumbuhan
            # ================================================================

            # Syarat 1: Minimum holding time
            if holding_hours < settings.MIN_HOLDING_HOURS_BEFORE_ROTATION:
                log.info(
                    f"⏳ [Rotasi Ditunda] {pos.base_asset}: Baru {holding_hours:.1f} jam "
                    f"(Minimum: {settings.MIN_HOLDING_HOURS_BEFORE_ROTATION}h)"
                )
                continue

            # Syarat 2: HARUS sudah BEP (Net PnL > 0)
            if not pos.is_bep_reached or pos.net_pnl_usdt <= 0.0:
                log.info(
                    f"⏳ [Rotasi Ditunda] {pos.base_asset} belum BEP "
                    f"(Net PnL: ${pos.net_pnl_usdt:+.4f} | Real Funding: +${pos.realized_funding_usdt:.4f}). "
                    f"Menunggu funding fee menutupi seluruh biaya round-trip sebelum rotasi."
                )
                continue

            # Syarat 3: HARUS ada profit surplus di atas BEP (KUNCI PERTUMBUHAN)
            # Ini memastikan setelah rotasi, modal bersih tetap lebih besar dari modal awal
            min_profit_required = getattr(settings, "MIN_PROFIT_BEFORE_ROTATION_USDT", 0.5)
            if pos.net_pnl_usdt < min_profit_required:
                log.info(
                    f"⏳ [Rotasi Ditunda] {pos.base_asset}: Profit belum mencukupi untuk rotasi. "
                    f"(Net PnL: ${pos.net_pnl_usdt:+.4f} | Minimum profit rotasi: ${min_profit_required:.4f} USDT). "
                    f"Tetap hold untuk akumulasi profit lebih banyak dulu."
                )
                continue

            # Syarat 4: Cek konsistensi koin baru harus > 75%
            if best_new_opp.consistency_score_percent < 75.0:
                log.info(
                    f"⚠️ [Rotasi Ditunda] Kandidat {best_new_opp.base_asset} konsistensinya kurang "
                    f"({best_new_opp.consistency_score_percent:.0f}% < 75%). Menunggu koin lebih stabil."
                )
                continue

            # Syarat 5: APY koin baru harus lebih baik signifikan
            old_opp = next((o for o in opportunities if o.base_asset == pos.base_asset), None)
            old_apy = old_opp.net_apy_percent if old_opp else 0.0
            apy_gain = best_new_opp.net_apy_percent - old_apy

            if apy_gain < settings.MIN_ROTATION_APY_DIFF:
                log.info(
                    f"📊 [Rotasi Tidak Layak] Keunggulan APY {best_new_opp.base_asset} ({apy_gain:+.1f}%) "
                    f"< Ambang rotasi ({settings.MIN_ROTATION_APY_DIFF}%). "
                    f"Hold {pos.base_asset} lebih menguntungkan saat ini."
                )
                continue

            # Syarat 6: Estimasi BEP koin baru harus < 48 jam
            new_bep_hours = best_new_opp.break_even_hours
            if new_bep_hours > 48.0:
                log.info(
                    f"⚠️ [Rotasi Ditunda] BEP koin baru {best_new_opp.base_asset} terlalu lama "
                    f"({new_bep_hours:.1f}h > 48h). Tetap hold {pos.base_asset}."
                )
                continue

            # Konsultasi AI Brain sebelum eksekusi rotasi
            if settings.ENABLE_AI_BRAIN:
                ai_approved = await gemini_brain.evaluate_rotation_candidate(
                    current_pos=pos,
                    candidate_opp=best_new_opp
                )
                if not ai_approved:
                    log.info(
                        f"✋ [Rotasi Ditahan oleh AI Brain] Gemini AI menyarankan tetap hold {pos.base_asset} "
                        f"karena yield saat ini masih optimal dan meminimalkan biaya fee baru."
                    )
                    continue

            # ✅ Semua syarat terpenuhi - Eksekusi Rotasi
            rotation_msg = (
                f"🔄 *[ROTASI PELUANG TERDETEKSI]*\n"
                f"Menutup: `{pos.base_asset}` (Net APY: `{old_apy:.1f}%`)\n"
                f"Rotasi ke: `{best_new_opp.base_asset}` (Net APY: `{best_new_opp.net_apy_percent:.1f}%`)\n"
                f"Keuntungan Yield: `+{apy_gain:.1f}% APY`\n"
                f"Profit yang Sudah Diraih: `+${pos.net_pnl_usdt:.4f} USDT` (akan dikunci ke modal)\n"
                f"BEP Koin Baru: `{new_bep_hours:.1f} jam`\n"
                f"Holding Hours: `{holding_hours:.1f} jam`"
            )
            log.info(rotation_msg.replace("*", "").replace("`", ""))
            await notifier.send_message(rotation_msg)

            # 1. Unwind posisi lama
            success_close = await self.executor.close_delta_neutral_position(
                position_id=pos.position_id,
                reason=f"Rotasi Peluang ke {best_new_opp.base_asset} (+{apy_gain:.1f}% APY | Profit: +${pos.net_pnl_usdt:.4f})"
            )

            # 2. Buka posisi pada koin baru yang lebih superior
            if success_close:
                try:
                    from core.historical_store import historical_store
                    historical_store.record_agi_experience(
                        event_type="ROTATION",
                        base_asset=pos.base_asset,
                        funding_rate=pos.last_funding_rate,
                        harvest_usdt=pos.cumulative_funding_received,
                        net_pnl_usdt=pos.net_pnl_usdt,
                        holding_hours=holding_hours,
                        was_bep_reached=pos.is_bep_reached,
                        ai_decision=f"ROTATE_TO_{best_new_opp.base_asset}",
                        lesson_learned=f"Rotasi berhasil dari {pos.base_asset} ke {best_new_opp.base_asset} setelah {holding_hours:.1f} jam. Profit terkunci: +${pos.net_pnl_usdt:.4f}. APY gain: +{apy_gain:.1f}%.",
                        pair_reputation_score=0.2 if pos.is_bep_reached else 0.0
                    )
                except Exception:
                    pass

                await asyncio.sleep(2)
                await self.executor.open_delta_neutral_position(
                    opportunity=best_new_opp,
                    allocated_capital_usdt=capital_per_position
                )
                break

auto_rebalancer = AutoRebalancer()
