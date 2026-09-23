import asyncio
from datetime import datetime
from typing import List, Optional

from config.settings import settings
from utils.logger import log
from utils.notifier import notifier
from core.models import Opportunity, DeltaNeutralPosition
from core.bitget_client import BitgetClient, bitget_client
from execution.position_manager import PositionManager, position_manager
from execution.order_executor import OrderExecutor, order_executor

class AutoRebalancer:
    """
    Modul Auto-Rebalancing & Dynamic Opportunity Rotation:
    1. Rebalancing Delta: Memastikan delta Spot Long vs Futures Short tetap mendekati 0.
    2. Dynamic Opportunity Rotation ('Mencari Taker Lain yang Memiliki Potensial'):
       Secara berkala membandingkan performa posisi aktif dengan peluang terbaru di pasar.
       Jika yield posisi aktif meredup dan muncul peluang baru dengan keunggulan yield signifikan,
       bot otomatis merotasi modal ke pasangan baru tersebut.
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
        Evaluasi rotasi peluang:
        Jika ada pasangan baru dengan Net APY jauh lebih tinggi dibanding posisi aktif yang sedang di-hold.
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
                continue

            now = datetime.utcnow()
            holding_hours = (now - pos.entry_time).total_seconds() / 3600.0

            # Sinkronisasi status PnL riil dan funding fee dari ledger
            await self.client.update_position_live_pnl(pos)

            # Syarat rotasi:
            # 1. Sudah di-hold minimal jam holding yang ditentukan (misal 16 jam)
            # 2. Posisi HARUS SUDAH BEP (Net PnL > 0) agar tidak terjadi fee churn
            # 3. Keunggulan APY pasangan baru melampaui ambang batas MIN_ROTATION_APY_DIFF
            if holding_hours >= settings.MIN_HOLDING_HOURS_BEFORE_ROTATION:
                if pos.net_pnl_usdt <= 0.0 and not pos.is_bep_reached:
                    log.info(
                        f"⏳ [Rotasi Ditunda] {pos.base_asset} belum mencapai BEP "
                        f"(Net PnL: ${pos.net_pnl_usdt:+.4f} | Real Funding: +${pos.realized_funding_usdt:.4f} | "
                        f"Unrealized PnL: ${pos.unrealized_pnl_usdt:+.4f}). "
                        f"Menunggu akumulasi funding fee menutupi trading fee sebelum rotasi."
                    )
                    continue

                # Perkirakan current net yield dari posisi lama
                old_rate = pos.perp_leg.current_price  # fallback
                # Ambil funding rate terkini koin lama
                old_opp = next((o for o in opportunities if o.base_asset == pos.base_asset), None)
                old_apy = old_opp.net_apy_percent if old_opp else 0.0

                apy_gain = best_new_opp.net_apy_percent - old_apy

                if apy_gain >= settings.MIN_ROTATION_APY_DIFF:
                    # Konsultasi AI Brain sebelum eksekusi rotasi
                    from core.gemini_brain import gemini_brain
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

                    rotation_msg = (
                        f"🔄 *[ROTASI PELUANG TERDETEKSI]*\n"
                        f"Menutup: `{pos.base_asset}` (Net APY: `{old_apy:.1f}%`)\n"
                        f"Rotasi ke: `{best_new_opp.base_asset}` (Net APY: `{best_new_opp.net_apy_percent:.1f}%`)\n"
                        f"Keuntungan Yield: `+{apy_gain:.1f}% APY`\n"
                        f"Holding Hours: `{holding_hours:.1f} jam`"
                    )
                    log.info(rotation_msg.replace("*", "").replace("`", ""))
                    await notifier.send_message(rotation_msg)

                    # 1. Unwind posisi lama
                    success_close = await self.executor.close_delta_neutral_position(
                        position_id=pos.position_id,
                        reason=f"Rotasi Peluang ke {best_new_opp.base_asset} (+{apy_gain:.1f}% APY)"
                    )

                    # 2. Buka posisi pada koin baru yang lebih superior
                    if success_close:
                        await asyncio.sleep(2)
                        await self.executor.open_delta_neutral_position(
                            opportunity=best_new_opp,
                            allocated_capital_usdt=capital_per_position
                        )
                        break

auto_rebalancer = AutoRebalancer()
