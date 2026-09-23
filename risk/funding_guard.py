import asyncio
from datetime import datetime
from typing import Dict, Any

from config.settings import settings
from utils.logger import log
from utils.notifier import notifier
from core.bitget_client import BitgetClient, bitget_client
from execution.position_manager import PositionManager, position_manager
from execution.order_executor import OrderExecutor, order_executor
from risk.yield_vault import yield_vault
from risk.compounding_manager import compounding_manager

from core.gemini_brain import gemini_brain
from core.historical_store import historical_store

class FundingGuard:
    """
    Penjaga Funding Rate:
    1. Memantau apakah funding rate pada posisi aktif berubah menjadi negatif.
    2. Pemantauan Pra-Settlement (5 menit sebelum pembayaran funding) untuk mendeteksi perubahan drastis rate.
    3. AI Continuous Learning: Mempelajari efisiensi posisi dengan Google Gemini setelah tiap panen.
    4. Memicu auto-exit jika funding rate merugikan trader.
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
        self.last_funding_times: Dict[str, datetime] = {}

    async def check_positions_funding(self):
        """Pemeriksaan siklus funding untuk seluruh posisi aktif."""
        active_positions = self.pos_mgr.get_active_positions()
        if not active_positions:
            return

        funding_rates_dict = await self.client.fetch_all_funding_rates()

        for pos in active_positions:
            fr_data = funding_rates_dict.get(pos.perp_leg.symbol)
            if not fr_data:
                continue

            current_rate = float(fr_data.get("fundingRate", 0.0))
            pos_interval = int(getattr(pos, "funding_interval_hours", 8) or 8)
            log.info(f"[FundingGuard] {pos.base_asset}: Funding Rate saat ini: {current_rate * 100:.4f}%/{pos_interval}h")

            # Sinkronisasi status PnL riil dan funding fee dari ledger Bitget
            prev_cumulative = pos.cumulative_funding_received
            await self.client.update_position_live_pnl(pos)

            # 1. Pemantauan Pra-Settlement (5 Menit Menuju Pembayaran Funding Fee)
            countdown = await self.client.get_funding_settlement_countdown(pos.perp_leg.symbol)
            if countdown.get("is_near_settlement"):
                mins_left = countdown["minutes_until_settlement"]
                proj_rate = countdown["current_projected_rate"]
                log.info(
                    f"⏱️ [Pra-Settlement 5 Menit] {pos.base_asset}: {mins_left}m menuju settlement. "
                    f"Projected Rate: {proj_rate * 100:+.4f}%"
                )
                if settings.ENABLE_AI_BRAIN:
                    ai_risk = await gemini_brain.evaluate_pre_settlement_risk(
                        pos=pos,
                        projected_rate=proj_rate,
                        seconds_until_settlement=countdown["seconds_until_settlement"]
                    )
                    if ai_risk.get("action") == "EMERGENCY_EXIT":
                        log.critical(
                            f"🚨 [AI Brain Emergency Exit] {pos.base_asset}: {ai_risk.get('message')}. "
                            f"Menutup posisi sebelum pemotongan funding fee minus!"
                        )
                        await self.executor.close_delta_neutral_position(
                            position_id=pos.position_id,
                            reason=f"AI Pre-Settlement Exit ({proj_rate * 100:+.4f}%)"
                        )
                        continue

            # 2. Deteksi Pembalikan Funding Rate (Zero-Tolerance: Rate < 0.0% langsung exit)
            if current_rate < settings.EMERGENCY_EXIT_FUNDING_RATE:
                log.warning(
                    f"🚨 [ZERO-TOLERANCE EXIT] Funding rate untuk {pos.base_asset} menjadi negatif "
                    f"({current_rate * 100:.4f}% < 0.0%). Menutup seluruh posisi dan membatalkan delta-neutral di taker ini!"
                )
                success_close = await self.executor.close_delta_neutral_position(
                    position_id=pos.position_id,
                    reason=f"Zero-Tolerance Negative Funding ({current_rate * 100:.4f}%)"
                )
                if success_close:
                    try:
                        historical_store.record_agi_experience(
                            event_type="EMERGENCY_EXIT_NEGATIVE_RATE",
                            base_asset=pos.base_asset,
                            funding_rate=current_rate,
                            harvest_usdt=pos.cumulative_funding_received,
                            net_pnl_usdt=pos.net_pnl_usdt,
                            holding_hours=(datetime.utcnow() - pos.entry_time).total_seconds() / 3600.0,
                            was_bep_reached=pos.is_bep_reached,
                            ai_decision="EMERGENCY_EXIT",
                            lesson_learned=f"Terkikis rate negatif ({current_rate*100:.4f}% < 0.0%). Exit darurat untuk selamatkan modal.",
                            pair_reputation_score=-0.4
                        )
                    except Exception:
                        pass

                    await self.find_and_reopen_new_taker()
                continue

            # Proyeksi Dividen Dinamis Menit-ke-Menit
            interval = int(getattr(pos, "funding_interval_hours", 8) or 8)
            proj = gemini_brain.generate_funding_projection(pos, current_rate, interval)
            pos.last_funding_rate = current_rate
            pos.projected_next_funding_payout = proj["projected_next_payout_usdt"]
            self.pos_mgr.update_position(pos)

            log.info(
                f"📊 [Proyeksi Funding 1-Menit] {pos.base_asset}: Rate {proj['current_rate_percent']:+.4f}%/{interval}h -> "
                f"Est. Payout Berikutnya: +${proj['projected_next_payout_usdt']:.5f} USDT "
                f"(Est. +${proj['projected_daily_usdt']:.4f}/hari | {proj['projected_annual_apy_percent']:.1f}% APY)"
            )

            # 3. Akumulasi Funding Fee Riil dari Ledger Bitget & Continuous Learning
            if pos.realized_funding_usdt > prev_cumulative:
                harvested = pos.realized_funding_usdt - prev_cumulative
                pos.funding_payments_count += 1
                self.pos_mgr.update_position(pos)

                compounding_manager.add_harvest_profit(
                    position_id=pos.position_id,
                    base_asset=pos.base_asset,
                    profit_usdt=harvested,
                    funding_rate=current_rate
                )

                log.info(
                    f"💰 [Harvest Riil Ledger] Posisi {pos.base_asset} menerima funding fee: "
                    f"+${harvested:.4f} USDT (Total Terkumpul: ${pos.cumulative_funding_received:.4f} | "
                    f"Net PnL: ${pos.net_pnl_usdt:+.4f} | Status BEP: {'✅ Sudah BEP' if pos.is_bep_reached else '⏳ Belum BEP'})"
                )

                # Evaluasi & Pembelajaran Adaptif Gemini AI Brain
                if settings.ENABLE_AI_BRAIN:
                    await gemini_brain.evaluate_harvest_and_learn(
                        pos=pos,
                        harvest_amount_usdt=harvested,
                        current_rate=current_rate
                    )
            elif pos.realized_funding_usdt == 0.0:
                # Fallback estimasi siklus jika mode dry-run atau ledger delay
                now = datetime.utcnow()
                last_checked = self.last_funding_times.get(pos.position_id, pos.entry_time)
                hours_passed = (now - last_checked).total_seconds() / 3600.0

                if hours_passed >= interval:
                    cycle_count = int(hours_passed // interval)
                    harvested = pos.perp_leg.nominal_usdt * current_rate * cycle_count
                    pos.cumulative_funding_received += harvested
                    pos.funding_payments_count += cycle_count
                    self.last_funding_times[pos.position_id] = now
                    self.pos_mgr.update_position(pos)

                    compounding_manager.add_harvest_profit(
                        position_id=pos.position_id,
                        base_asset=pos.base_asset,
                        profit_usdt=harvested,
                        funding_rate=current_rate
                    )

                    log.info(
                        f"💰 [Harvest Estimasi] Posisi {pos.base_asset} estimasi funding fee: "
                        f"+${harvested:.4f} USDT (Total Terkumpul: ${pos.cumulative_funding_received:.4f})"
                    )

    async def find_and_reopen_new_taker(self):
        """
        Setelah pembatalan taker darurat akibat rate negatif, segera pindai pasar
        dan buka posisi di taker baru yang konsisten & potensial tanpa menunggu jeda waktu.
        """
        try:
            log.info("🔍 [Pencarian Taker Baru] Memindai pasar untuk mencari taker potensial & konsisten...")
            from scanner.opportunity_finder import opportunity_finder
            from risk.compounding_manager import compounding_manager
            
            bal = await self.client.fetch_balance()
            total_bal = float(bal.get("total", {}).get("USDT", 0.0) or bal.get("USDT", {}).get("total", 0.0) or 0.0)
            capital = compounding_manager.get_position_capital(total_bal)
            
            opps = await opportunity_finder.scan(target_nominal_usdt=capital)
            eligible_opps = [o for o in opps if o.is_eligible and o.current_funding_rate > 0.0]
            
            if not eligible_opps:
                log.info("[Pencarian Taker Baru] Belum ada taker yang memenuhi syarat modal saat ini. Menunggu siklus pemindaian berikutnya.")
                return

            # Gunakan Gemini AGI untuk memilih taker paling konsisten & aman
            best_opp = await gemini_brain.select_optimal_taker_agi(eligible_opps, capital) or eligible_opps[0]
            
            log.info(
                f"🚀 [Relokasi Modal Cepat] Membuka posisi Delta-Neutral baru di {best_opp.base_asset} "
                f"(Rate: {best_opp.current_funding_rate*100:+.4f}%/{best_opp.funding_interval_hours}h, Net APY: {best_opp.net_apy_percent:.1f}%)..."
            )
            await self.executor.open_delta_neutral_position(
                opportunity=best_opp,
                allocated_capital_usdt=capital
            )
        except Exception as e:
            log.error(f"Gagal melakukan relokasi ke taker baru: {e}")

funding_guard = FundingGuard()
