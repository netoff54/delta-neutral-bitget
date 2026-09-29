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

        try:

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
                        # Spot lebih banyak dari short -> Tambah Short Perp sebagai MAKER
                        target_perp_p = pos.perp_leg.current_price
                        try:
                            pt = await self.client.client.fetch_ticker(pos.perp_leg.symbol)
                            target_perp_p = float(pt.get("ask") or pt.get("last") or target_perp_p)
                            if settings.REQUIRE_POSITIVE_SPREAD:
                                target_perp_p = max(target_perp_p, pos.spot_leg.current_price)
                        except Exception:
                            pass

                        log.info(f"-> Menambah Short Perp {pos.perp_leg.symbol} sejumlah {drift:.4f} @ ${target_perp_p:,.4f} (Maker)...")
                        res = await self.client.execute_perp_order(
                            symbol=pos.perp_leg.symbol,
                            side="sell",
                            amount=abs(drift),
                            order_type="limit" if settings.USE_MAKER_ORDERS else "market",
                            price=target_perp_p,
                            post_only=settings.USE_MAKER_ORDERS
                        )
                        if not res.success and settings.USE_MAKER_ORDERS:
                            # Fallback limit GTC
                            res = await self.client.execute_perp_order(
                                symbol=pos.perp_leg.symbol,
                                side="sell",
                                amount=abs(drift),
                                order_type="limit",
                                price=target_perp_p,
                                post_only=False
                            )
                        if res.success:
                            pos.perp_leg.amount += res.filled_amount
                    else:
                        # Short lebih banyak dari spot -> Tambah Beli Spot sebagai MAKER
                        target_spot_p = pos.spot_leg.current_price
                        try:
                            st = await self.client.client.fetch_ticker(pos.spot_leg.symbol)
                            target_spot_p = float(st.get("bid") or st.get("last") or target_spot_p)
                            if settings.REQUIRE_POSITIVE_SPREAD:
                                target_spot_p = min(target_spot_p, pos.perp_leg.current_price)
                        except Exception:
                            pass

                        log.info(f"-> Menambah Beli Spot {pos.spot_leg.symbol} sejumlah {abs(drift):.4f} @ ${target_spot_p:,.4f} (Maker)...")
                        res = await self.client.execute_spot_order(
                            symbol=pos.spot_leg.symbol,
                            side="buy",
                            amount=abs(drift),
                            order_type="limit" if settings.USE_MAKER_ORDERS else "market",
                            price=target_spot_p,
                            post_only=settings.USE_MAKER_ORDERS
                        )
                        if not res.success and settings.USE_MAKER_ORDERS:
                            res = await self.client.execute_spot_order(
                                symbol=pos.spot_leg.symbol,
                                side="buy",
                                amount=abs(drift),
                                order_type="limit",
                                price=target_spot_p,
                                post_only=False
                            )
                        if res.success:
                            pos.spot_leg.amount += res.filled_amount

                    pos.net_delta = round(pos.spot_leg.amount - pos.perp_leg.amount, 8)
                    self.pos_mgr.update_position(pos)
                    log.info(f"✅ Rebalancing selesai untuk {pos.base_asset}. Delta baru: {pos.net_delta}")

        except Exception as e:
            log.error(f"[AutoRebalancer] Kendala rebalance_delta_drift: {e}")

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

        try:

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
                # 6 SYARAT KETAT ROTASI - Mencegah Fee Churn & Memastikan Pertumbuhan
                # ================================================================

                # Hitung nilai modal pokok BEP posisi (Spot + Futures Margin)
                spot_capital = getattr(pos.spot_leg, "nominal_usdt", 0.0) or (pos.spot_leg.entry_price * pos.spot_leg.amount)
                perp_capital = (getattr(pos.perp_leg, "nominal_usdt", 0.0) or (pos.perp_leg.entry_price * pos.perp_leg.amount)) / max(1, getattr(pos, "leverage", 1))
                position_bep_capital = spot_capital + perp_capital
                if position_bep_capital <= 0.0 and capital_per_position:
                    position_bep_capital = capital_per_position

                # Target surplus 5% (sebelum 1 bulan) dan WAJIB minimal 1% (setelah 1 bulan) dari nilai BEP portofolio
                surplus_5pct_target = getattr(settings, "MIN_PROFIT_SURPLUS_PERCENT", 0.05)
                surplus_1pct_target = getattr(settings, "MIN_PROFIT_SURPLUS_AFTER_DEADLINE_PERCENT", 0.01)

                min_profit_5pct = round(position_bep_capital * surplus_5pct_target, 4)
                min_profit_1pct = round(position_bep_capital * surplus_1pct_target, 4)
                profit_pct_now = (pos.net_pnl_usdt / position_bep_capital * 100.0) if position_bep_capital > 0 else 0.0

                # Tenggat waktu 1 bulan (30 hari / 720 jam)
                deadline_days = getattr(settings, "MAX_HOLDING_DEADLINE_DAYS", 30.0)
                deadline_hours = deadline_days * 24.0
                is_deadline_passed = holding_hours >= deadline_hours
                days_held = holding_hours / 24.0
                days_left = max(0.0, (deadline_hours - holding_hours) / 24.0)

                # Syarat 1: Cek apakah koin aktif saat ini masih mencetak target >= 2.5% per minggu
                # Jika koin aktif masih sangat bagus (>= 0.04% per 4h atau >= 0.09% per 8h), BIARKAN BERJALAN (HOLD)
                curr_rate = getattr(pos, "last_funding_rate", 0.0)
                int_hours = getattr(pos, "funding_interval_hours", 8)
                daily_cycles = 24.0 / max(1, int_hours)
                est_weekly_yield = (curr_rate * daily_cycles * 7.0) * 100.0

                is_winner_running = (curr_rate >= 0.0004 if int_hours <= 4 else curr_rate >= 0.0009)
                if is_winner_running and holding_hours < deadline_hours:
                    log.info(
                        f"💎 [Pemenang Berjalan (Hold)] {pos.base_asset}: Funding Rate masih sangat tinggi "
                        f"({curr_rate * 100:.4f}%/{int_hours}h | Proyeksi Mingguan: {est_weekly_yield:.2f}% >= 2.50%). "
                        f"Tidak perlu rotasi, biarkan bunga terus menggunung tanpa terkena potongan fee baru!"
                    )
                    continue

                # Syarat 2: Minimum holding time awal untuk meredam churn (minimal 4 jam / 1 siklus)
                if holding_hours < settings.MIN_HOLDING_HOURS_BEFORE_ROTATION:
                    log.info(
                        f"⏳ [Rotasi Ditunda] {pos.base_asset}: Baru berjalan {holding_hours:.1f} jam "
                        f"(Minimum stabilitas: {settings.MIN_HOLDING_HOURS_BEFORE_ROTATION}h)"
                    )
                    continue

                # Syarat 3: WAJIB SUDAH BEP MUTLAK (Ground-Truth Portofolio Bitget & Posisi)
                # Menutup 100% dari seluruh 4 biaya transaksi: Beli Spot + Jual Spot + Buka Perp + Tutup Perp!
                is_port_bep = getattr(pos, "portfolio_bep_reached", False)
                port_net_pnl = getattr(pos, "portfolio_net_pnl_usdt", pos.net_pnl_usdt)

                if (not pos.is_bep_reached or pos.net_pnl_usdt <= 0.0) or (not is_port_bep and port_net_pnl < 0.0):
                    log.info(
                        f"⏳ [Rotasi Ditunda] {pos.base_asset} BELUM BEP: "
                        f"Posisi Net PnL: ${pos.net_pnl_usdt:+.4f} USDT | Portofolio Bitget Net: ${port_net_pnl:+.4f} USDT | "
                        f"Real Funding: +${pos.realized_funding_usdt:.4f} USDT. "
                        f"Menolak rotasi sukarela demi melindungi modal awal dari kerugian fee bursa."
                    )
                    continue

                # Syarat 4: SURPLUS PROFIT BERSIH DI ATAS BEP
                # Untuk kelincahan target 2.5% mingguan, cukup surplus 0.3% modal di atas BEP
                target_surplus_pct = surplus_1pct_target if is_deadline_passed else surplus_5pct_target
                min_required_profit = round(position_bep_capital * target_surplus_pct, 4)

                if pos.net_pnl_usdt < min_required_profit:
                    log.info(
                        f"⏳ [Rotasi Ditunda] {pos.base_asset}: Sudah BEP tetapi belum mencapai surplus profit "
                        f"(Saat ini: +${pos.net_pnl_usdt:.4f} USDT / Target: +${min_required_profit:.4f} USDT [{target_surplus_pct*100:.2f}%]). "
                        f"Tahan posisi agar keuntungan bersih terkumpul sebelum berpindah koin."
                    )
                    continue
                else:
                    log.info(
                        f"🎯 [Target Surplus Terpenuhi] {pos.base_asset}: Berhasil mengantongi profit bersih "
                        f"+${pos.net_pnl_usdt:.4f} USDT ({profit_pct_now:+.2f}% di atas seluruh fee transaksi). "
                        f"Koin aktif melambat ({curr_rate*100:.4f}%), siap dievaluasi untuk rotasi!"
                    )

                # Syarat 5: Cek konsistensi koin baru harus stabil (> 75%)
                if best_new_opp.consistency_score_percent < 75.0:
                    log.info(
                        f"⚠️ [Rotasi Ditunda] Kandidat koin baru {best_new_opp.base_asset} konsistensinya kurang "
                        f"({best_new_opp.consistency_score_percent:.1f}% < 75%). Menunggu koin yang lebih stabil."
                    )
                    continue

                # Syarat 5: APY koin baru harus lebih baik signifikan (>= MIN_ROTATION_APY_DIFF)
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

                # Syarat 6: Estimasi BEP koin baru harus cepat (< 48 jam)
                new_bep_hours = best_new_opp.break_even_hours
                if new_bep_hours > 48.0:
                    log.info(
                        f"⚠️ [Rotasi Ditunda] BEP koin baru {best_new_opp.base_asset} terlalu lama "
                        f"({new_bep_hours:.1f}h > 48h). Tetap hold {pos.base_asset}."
                    )
                    continue

                # Syarat 7: JAMIN BASIS SPREAD EXIT AMAN (Spot Sell >= Perp Buy)
                # Saat exit, jual Spot dan beli tutup Perp. Pastikan spread tidak minus agar modal utuh.
                if getattr(settings, "REQUIRE_POSITIVE_SPREAD", True):
                    live_spot_p = pos.spot_leg.current_price
                    live_perp_p = pos.perp_leg.current_price
                    exit_spread = ((live_spot_p - live_perp_p) / live_perp_p * 100.0) if live_perp_p > 0 else 0.0
                    if exit_spread < -0.05:
                        log.info(
                            f"⏳ [Rotasi Ditunda] Basis spread exit {pos.base_asset} sedang negatif ({exit_spread:+.3f}%). "
                            f"Menunggu konvergensi harga Spot >= Perp agar modal keluar tanpa tergerus spread."
                        )
                        continue

                # Konfirmasi Kualitatif AI Gemini Brain
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

                # ✅ Seluruh 6 Syarat Ketat & Target 5% / Tenggat 1 Bulan Terpenuhi - Eksekusi Rotasi
                rotation_msg = (
                    f"🔄 *[ROTASI PELUANG TERVERIFIKASI]*\n"
                    f"Menutup: `{pos.base_asset}` (Net APY: `{old_apy:.1f}%`)\n"
                    f"Rotasi ke: `{best_new_opp.base_asset}` (Net APY: `{best_new_opp.net_apy_percent:.1f}%`)\n"
                    f"Keuntungan APY: `+{apy_gain:.1f}% APY`\n"
                    f"Profit Terkunci: `+${pos.net_pnl_usdt:.4f} USDT` ({profit_pct_now:+.2f}% dari modal BEP ${position_bep_capital:.2f})\n"
                    f"Waktu Holding: `{days_held:.1f} hari` (`{holding_hours:.1f} jam`)\n"
                    f"BEP Koin Baru: `{new_bep_hours:.1f} jam`"
                )
                log.info(rotation_msg.replace("*", "").replace("`", ""))
                await notifier.send_message(rotation_msg)

                # Tarik bunga riil teraktual dari ledger Bitget sebelum menutup posisi
                entry_ts_ms = int(pos.entry_time.timestamp() * 1000)
                try:
                    real_funding_from_ledger = await self.client.fetch_realized_funding_fee(pos.base_asset, since_timestamp_ms=entry_ts_ms)
                    if real_funding_from_ledger > 0.0:
                        pos.realized_funding_usdt = real_funding_from_ledger
                        pos.cumulative_funding_received = real_funding_from_ledger
                except Exception as l_err:
                    log.debug(f"[Rotasi] Fetch realized funding notice: {l_err}")

                # Simpan bunga real secara permanen ke database (tabel funding_harvests)
                from core.database import db
                try:
                    db.record_funding_harvest(
                        position_id=pos.position_id,
                        base_asset=pos.base_asset,
                        perp_symbol=pos.perp_leg.symbol,
                        funding_rate=pos.last_funding_rate,
                        funding_interval_hours=getattr(pos, "funding_interval_hours", 8),
                        payment_usdt=pos.cumulative_funding_received,
                        compounded_amount_usdt=0.0
                    )
                except Exception as dbe:
                    log.debug(f"[Rotasi] Catat funding harvest ke database: {dbe}")

                # Sintesis Pembelajaran Kausal AGI: Mengapa dapat bunga segitu & bagaimana meningkatkannya
                effective_realized_apy = (
                    (pos.cumulative_funding_received / max(1.0, position_bep_capital)) *
                    (8760.0 / max(1.0, holding_hours)) * 100.0
                ) if holding_hours > 0 else 0.0

                why_yield_achieved = (
                    f"Posisi {pos.base_asset} menghasilkan bunga riil ${pos.cumulative_funding_received:.4f} USDT "
                    f"(Net PnL ${pos.net_pnl_usdt:+.4f} USDT, APY Realisasi {effective_realized_apy:.1f}%) selama {holding_hours:.1f} jam. "
                    f"Penyebab keberhasilan: Pembayaran dividen funding interval {getattr(pos, 'funding_interval_hours', 8)}h "
                    f"dengan rate rata-rata {pos.last_funding_rate*100:+.4f}% konsisten positif tanpa flip negatif yang menggerus modal."
                )

                how_to_increase_future_yield = (
                    f"Strategi meningkatkan hasil pada rotasi berikutnya: "
                    f"1) Prioritaskan koin dengan interval funding dinamis (1h/4h) yang memiliki reputasi 'STABLE_AUTHENTIC' 4 bulan "
                    f"agar dividen cair lebih sering (hingga 24x/hari). "
                    f"2) Pastikan basis spread perp >= spot selalu positif tinggi saat entry untuk mempercepat penutupan amortisasi taker fee. "
                    f"3) Biarkan posisi melampaui surplus BEP 5% agar efek bunga berbunga (compounding) bekerja maksimal."
                )

                rotation_context_snapshot = {
                    "rotated_from": pos.base_asset,
                    "rotated_to": best_new_opp.base_asset,
                    "realized_funding_usdt": pos.cumulative_funding_received,
                    "net_pnl_usdt": pos.net_pnl_usdt,
                    "position_bep_capital": position_bep_capital,
                    "effective_realized_apy": round(effective_realized_apy, 2),
                    "holding_hours": round(holding_hours, 2),
                    "days_held": round(days_held, 2),
                    "old_apy": old_apy,
                    "new_apy": best_new_opp.net_apy_percent,
                    "new_token_interval": best_new_opp.funding_interval_hours,
                    "new_token_stability": getattr(best_new_opp.historical_120d_stats, "stability_diagnosis", "STABLE_AUTHENTIC") if best_new_opp.historical_120d_stats else "STABLE_AUTHENTIC",
                    "spot_exit_price": pos.spot_leg.current_price,
                    "perp_exit_price": pos.perp_leg.current_price,
                    "exit_reason": "OPPORTUNITY_ROTATION_SURPLUS_ACHIEVED"
                }

                # Simpan pengalaman AGI permanen ke database agi_experience_memory
                try:
                    db.record_agi_experience(
                        event_type="ROTATION_LEARNING",
                        base_asset=pos.base_asset,
                        funding_rate=pos.last_funding_rate,
                        harvest_usdt=pos.cumulative_funding_received,
                        net_pnl_usdt=pos.net_pnl_usdt,
                        holding_hours=holding_hours,
                        was_bep_reached=pos.is_bep_reached,
                        ai_decision=f"ROTATE_{pos.base_asset}_TO_{best_new_opp.base_asset}",
                        lesson_learned=why_yield_achieved,
                        tactical_rule=how_to_increase_future_yield,
                        pair_reputation_score=0.25 if pos.is_bep_reached else 0.0,
                        context_snapshot=rotation_context_snapshot
                    )
                    log.info(f"🧠 [AGI Permanent Memory] Pengalaman rotasi {pos.base_asset} -> {best_new_opp.base_asset} tersimpan permanen di Database!")
                except Exception as mem_err:
                    log.debug(f"[Rotasi] Catat AGI experience notice: {mem_err}")

                # 1. Unwind posisi lama
                success_close = await self.executor.close_delta_neutral_position(
                    position_id=pos.position_id,
                    reason=f"Rotasi Peluang ke {best_new_opp.base_asset} (+{apy_gain:.1f}% APY | Bunga Riil: +${pos.cumulative_funding_received:.4f} USDT | Net PnL: +${pos.net_pnl_usdt:.4f})"
                )

                # 2. Buka posisi pada koin baru yang lebih superior
                if success_close:
                    await asyncio.sleep(2)
                    await self.executor.open_delta_neutral_position(
                        opportunity=best_new_opp,
                        allocated_capital_usdt=capital_per_position
                    )
                    break

        except Exception as e:
            log.error(f"[AutoRebalancer] Kendala evaluate_and_rotate: {e}")

auto_rebalancer = AutoRebalancer()
