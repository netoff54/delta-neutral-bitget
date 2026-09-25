import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional

from config.settings import settings
from utils.logger import log
from core.models import Opportunity, TakerSnapshot
from core.bitget_client import BitgetClient, bitget_client
from core.historical_store import historical_store
from analytics.fee_calculator import FeeCalculator, fee_calculator
from analytics.performance_scorer import PerformanceScorer, performance_scorer
from analytics.funding_history_analyzer import funding_history_analyzer
from utils.interval_helper import parse_interval_hours, detect_empirical_interval, cycles_per_day

class OpportunityFinder:
    """
    Pemindai otomatis pasar Bitget untuk mencari peluang Delta-Neutral
    dengan yield funding rate tertinggi, likuiditas memadai, dan break-even tercepat.
    """

    def __init__(
        self,
        client: BitgetClient = bitget_client,
        calculator: FeeCalculator = fee_calculator
    ):
        self.client = client
        self.calculator = calculator

    async def scan(self, target_nominal_usdt: Optional[float] = None) -> List[Opportunity]:
        """
        Memindai seluruh pasar Bitget secara menyeluruh.
        """
        await self.client.initialize()
        if target_nominal_usdt is None or target_nominal_usdt <= 0:
            target_nominal_usdt = 50.0

        log.info(f"Memulai pemindaian funding rate dan likuiditas pasar Bitget (Kalkulasi Modal: ${target_nominal_usdt:.2f})...")

        # Ambil data funding rates (dengan interval dinamis 4h/8h/1h) dan tickers spot & swap secara paralel
        rates_task = self.client.fetch_fund_rates_and_intervals()
        swap_tickers_task = self.client.fetch_tickers_by_type("swap")
        spot_tickers_task = self.client.fetch_tickers_by_type("spot")
        
        rates_dict, swap_tickers_dict, spot_tickers_dict = await asyncio.gather(
            rates_task, swap_tickers_task, spot_tickers_task
        )

        opportunities: List[Opportunity] = []

        for perp_symbol, fr_data in rates_dict.items():
            funding_rate = fr_data.get("fundingRate")
            if funding_rate is None or funding_rate <= 0:
                continue

            # Hanya evaluasi USDT-M Perpetual (format CCXT: BASE/USDT:USDT)
            if not perp_symbol.endswith("/USDT:USDT"):
                continue

            base_asset = perp_symbol.split("/")[0]
            spot_symbol = f"{base_asset}/USDT"

            # Pastikan pasangan spot tersedia di Bitget
            if spot_symbol not in self.client.client.markets:
                continue

            # Periksa data ticker harga & volume
            perp_ticker = swap_tickers_dict.get(perp_symbol)
            spot_ticker = spot_tickers_dict.get(spot_symbol)

            if not perp_ticker or not spot_ticker:
                continue

            spot_price = spot_ticker.get("last") or spot_ticker.get("close")
            perp_price = perp_ticker.get("last") or perp_ticker.get("close")

            if not spot_price or not perp_price or spot_price <= 0 or perp_price <= 0:
                continue

            # Volume 24h dalam USDT
            spot_volume = spot_ticker.get("quoteVolume") or (spot_ticker.get("baseVolume", 0) * spot_price)
            perp_volume = perp_ticker.get("quoteVolume") or (perp_ticker.get("baseVolume", 0) * perp_price)

            # Full Maker Spread Pricing: Buy Spot di Best Bid, Short Perp di Best Ask
            spot_bid = float(spot_ticker.get("bid") or spot_price)
            perp_ask = float(perp_ticker.get("ask") or perp_price)

            if getattr(settings, "USE_MAKER_ORDERS", True):
                maker_spread_pct = ((perp_ask - spot_bid) / spot_bid) * 100.0
                # Jamin spread positif (Perp Ask >= Spot Bid)
                if getattr(settings, "REQUIRE_POSITIVE_SPREAD", True):
                    basis_spread_pct = max(0.0001, maker_spread_pct)
                else:
                    basis_spread_pct = maker_spread_pct
            else:
                basis_spread_pct = ((perp_price - spot_price) / spot_price) * 100.0

            # Next funding time & interval dinamis (1h, 4h, 8h)
            interval_hours = parse_interval_hours(fr_data.get("fundingInterval"), default=8)
            next_funding_ts = fr_data.get("nextUpdate") or fr_data.get("fundingTimestamp")
            next_funding_time = datetime.utcfromtimestamp(next_funding_ts / 1000.0) if next_funding_ts else None

            # Evaluasi Fee & Yield dengan interval dinamis & Full Maker Mode
            eval_result = self.calculator.evaluate_opportunity(
                funding_rate=funding_rate,
                nominal_value_usdt=target_nominal_usdt,
                funding_interval_hours=interval_hours,
                basis_spread_percent=basis_spread_pct,
                is_maker=getattr(settings, "USE_MAKER_ORDERS", True)
            )

            # Cek likuiditas minimum
            is_eligible = eval_result["is_eligible"]
            rejection_reason = eval_result["rejection_reason"]

            # Cek apakah modal mencukupi batas minimum order exchange (Token size & USDT notional)
            effective_leverage = max(1, settings.LEVERAGE)
            leg_nominal = target_nominal_usdt / (1.0 + (1.0 / effective_leverage))
            
            min_cost_info = self.client.get_min_order_cost(spot_symbol, perp_symbol)
            if is_eligible and target_nominal_usdt < min_cost_info["required_min_capital_usdt"]:
                is_eligible = False
                rejection_reason = (
                    f"Modal (${target_nominal_usdt:.2f}) < Syarat Min Capital Exchange (${min_cost_info['required_min_capital_usdt']:.2f} USDT)"
                )

            aligned_qty = self.client.align_quantity(
                spot_symbol=spot_symbol,
                perp_symbol=perp_symbol,
                target_nominal_usdt=leg_nominal,
                price=spot_price
            )
            if is_eligible and aligned_qty <= 0:
                is_eligible = False
                rejection_reason = (
                    f"Nominal kaki (${leg_nominal:.2f}) di bawah batas minimal order "
                    f"(Futures Min: ${min_cost_info['perp_cost_min']:.2f} USDT)"
                )

            if is_eligible:
                if spot_volume < settings.MIN_24H_VOLUME_USDT or perp_volume < settings.MIN_24H_VOLUME_USDT:
                    is_eligible = False
                    rejection_reason = (
                        f"Volume 24h rendah (Spot: ${spot_volume:,.0f}, Perp: ${perp_volume:,.0f} < "
                        f"Min: ${settings.MIN_24H_VOLUME_USDT:,.0f})"
                    )

            opp = Opportunity(
                base_asset=base_asset,
                spot_symbol=spot_symbol,
                perp_symbol=perp_symbol,
                spot_price=float(spot_price),
                perp_price=float(perp_price),
                basis_spread_percent=round(basis_spread_pct, 4),
                current_funding_rate=float(funding_rate),
                next_funding_time=next_funding_time,
                funding_interval_hours=interval_hours,
                spot_volume_24h=float(spot_volume),
                perp_volume_24h=float(perp_volume),
                fee_breakdown=eval_result["fee_breakdown"],
                gross_cycle_yield_percent=eval_result["gross_cycle_yield_percent"],
                gross_apy_percent=eval_result["gross_apy_percent"],
                net_apy_percent=eval_result["net_apy_percent"],
                break_even_cycles=eval_result["break_even_cycles"],
                break_even_hours=eval_result["break_even_hours"],
                is_eligible=is_eligible,
                rejection_reason=rejection_reason,
                scanned_at=datetime.utcnow()
            )

            opportunities.append(opp)

            # Rekam snapshot taker potensial ke SQLite store 7 hari
            try:
                snap = TakerSnapshot(
                    base_asset=opp.base_asset,
                    spot_symbol=opp.spot_symbol,
                    perp_symbol=opp.perp_symbol,
                    spot_price=opp.spot_price,
                    perp_price=opp.perp_price,
                    basis_spread_percent=opp.basis_spread_percent,
                    current_funding_rate=opp.current_funding_rate,
                    predicted_next_rate=0.0,
                    funding_interval_hours=opp.funding_interval_hours,
                    spot_taker_fee_pct=self.calculator.spot_taker_fee * 100.0,
                    perp_taker_fee_pct=self.calculator.perp_taker_fee * 100.0,
                    round_trip_fee_pct=opp.fee_breakdown.total_fee_percent,
                    break_even_cycles=opp.break_even_cycles,
                    break_even_hours=opp.break_even_hours,
                    composite_score=0.0,
                    is_eligible=opp.is_eligible,
                    rejection_reason=opp.rejection_reason
                )
                historical_store.record_taker_snapshot(snap)
            except Exception as e:
                log.debug(f"Gagal rekam snapshot taker {opp.base_asset}: {e}")

        # Urutkan sementara berdasarkan Net APY untuk memilih kandidat yang dianalisis mendalam
        opportunities.sort(key=lambda x: (x.is_eligible, x.net_apy_percent), reverse=True)

        # Analisis mendalam (10 siklus historis, 7-hari rolling data, & prediktif) untuk kandidat teratas
        top_candidates = [o for o in opportunities if o.is_eligible][:10]
        if not top_candidates:
            # Jika tidak ada yang lolos kriteria keras, ambil top 5 untuk dianalisis
            top_candidates = opportunities[:5]

        analysis_days = getattr(settings, "HISTORICAL_ANALYSIS_DAYS", 60) or 60
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        since_analysis = now_ms - (analysis_days * 86400 * 1000)

        for opp in top_candidates:
            await asyncio.sleep(0.01)  # Yield kendali ke event loop agar responsif
            # 1. Ambil riwayat dari SQLite/PostgreSQL store 60 hari (2 bulan)
            stored_history = historical_store.get_funding_history(opp.perp_symbol, days=analysis_days)
            
            # Hitung target siklus 60 hari sesuai interval dinamis (1h = 1440 siklus, 4h = 360 siklus, 8h = 180 siklus)
            dyn_cycles_per_day = cycles_per_day(opp.funding_interval_hours)
            expected_cycles = int(analysis_days * dyn_cycles_per_day)
            min_required_cycles = max(15, int(expected_cycles * 0.4))

            # Jika data di SQLite belum lengkap (< 40% target siklus), lakukan backfill 60 hari (2 bulan) dari API Bitget
            if len(stored_history) < min_required_cycles:
                fetched_history = await self.client.fetch_funding_rate_history(
                    opp.perp_symbol,
                    since=since_analysis,
                    limit=min(500, max(100, expected_cycles))
                )
                if fetched_history:
                    # Deteksi interval empiris dari selisih timestamp riil antar-settlement Bitget
                    ts_list = [
                        int(h.get("fundingTimestamp") or h.get("timestamp") or h.get("fundingTime") or 0)
                        for h in fetched_history
                        if (h.get("fundingTimestamp") or h.get("timestamp") or h.get("fundingTime"))
                    ]
                    empirical_int = detect_empirical_interval(ts_list, default=opp.funding_interval_hours)
                    if empirical_int != opp.funding_interval_hours:
                        log.info(
                            f"⚡ [Interval Dinamis Terdeteksi] {opp.base_asset}: Interval disesuaikan dari "
                            f"{opp.funding_interval_hours}h ke {empirical_int}h berdasarkan timestamp riil Bitget."
                        )
                        opp.funding_interval_hours = empirical_int
                        # Re-evaluasi kelayakan peluang dengan interval baru yang tepat
                        eval_updated = self.calculator.evaluate_opportunity(
                            funding_rate=opp.current_funding_rate,
                            nominal_value_usdt=target_nominal_usdt,
                            funding_interval_hours=opp.funding_interval_hours,
                            basis_spread_percent=opp.basis_spread_percent
                        )
                        opp.fee_breakdown = eval_updated["fee_breakdown"]
                        opp.gross_cycle_yield_percent = eval_updated["gross_cycle_yield_percent"]
                        opp.gross_apy_percent = eval_updated["gross_apy_percent"]
                        opp.net_apy_percent = eval_updated["net_apy_percent"]
                        opp.break_even_cycles = eval_updated["break_even_cycles"]
                        opp.break_even_hours = eval_updated["break_even_hours"]
                        opp.is_eligible = eval_updated["is_eligible"]
                        opp.rejection_reason = eval_updated["rejection_reason"]

                    historical_store.record_funding_rates(
                        opp.perp_symbol,
                        fetched_history,
                        interval_hours=opp.funding_interval_hours
                    )
                    stored_history = historical_store.get_funding_history(opp.perp_symbol, days=analysis_days)
            else:
                # Ambil histori siklus terbaru jika perlu
                fetched_history = await self.client.fetch_funding_rate_history(
                    opp.perp_symbol,
                    limit=settings.PREDICTIVE_HORIZON_CYCLES
                )
                if fetched_history:
                    historical_store.record_funding_rates(
                        opp.perp_symbol,
                        fetched_history,
                        interval_hours=opp.funding_interval_hours
                    )
                    stored_history = historical_store.get_funding_history(opp.perp_symbol, days=analysis_days)

            opp.historical_funding_rates = [
                float(h["funding_rate"]) if "funding_rate" in h else float(h.get("fundingRate", 0.0))
                for h in stored_history[-settings.PREDICTIVE_HORIZON_CYCLES:]
            ] if stored_history else []

            # Format data untuk performance_scorer (10 siklus terakhir)
            history_scorer_input = [
                {"fundingRate": float(h["funding_rate"]) if "funding_rate" in h else float(h.get("fundingRate", 0.0))}
                for h in stored_history[-settings.PREDICTIVE_HORIZON_CYCLES:]
            ] if stored_history else []

            # Analisis data historis siklus jangka pendek
            hist_stats = performance_scorer.analyze_history(history_scorer_input)
            opp.historical_mean_rate = hist_stats["mean_rate"]
            opp.historical_std_rate = hist_stats["std_rate"]
            opp.consistency_score_percent = hist_stats["consistency_pct"]
            opp.funding_trend = hist_stats["trend"]

            # Prediksi rate ke depan
            pred_rate = performance_scorer.predict_next_funding_rate(
                current_funding_rate=opp.current_funding_rate,
                basis_spread_percent=opp.basis_spread_percent,
                trend=opp.funding_trend,
                historical_mean=opp.historical_mean_rate
            )
            opp.predicted_next_funding_rate = pred_rate

            # 2. Analisis Kuantitatif Historis 60 Hari (2 Bulan) Mendalam dari Data Asli Bitget
            stats_deep = funding_history_analyzer.analyze_history_deep(
                symbol=opp.perp_symbol,
                records=stored_history,
                spot_taker_fee_pct=self.calculator.spot_taker_fee * 100.0,
                perp_taker_fee_pct=self.calculator.perp_taker_fee * 100.0,
                funding_interval_hours=opp.funding_interval_hours,
                basis_spread_percent=opp.basis_spread_percent
            )
            opp.historical_60d_stats = stats_deep
            opp.historical_30d_stats = stats_deep
            opp.historical_7d_stats = stats_deep

            # Hitung skor performa komposit berbasis data (disesuaikan dengan interval 4h/8h/1h)
            base_comp_score = performance_scorer.calculate_composite_score(
                predicted_next_rate=pred_rate,
                historical_mean=opp.historical_mean_rate,
                consistency_pct=opp.consistency_score_percent,
                std_rate=opp.historical_std_rate,
                net_apy_percent=opp.net_apy_percent,
                break_even_hours=opp.break_even_hours,
                funding_interval_hours=opp.funding_interval_hours
            )

            # Integrasikan skor kualitas historis 60 hari (Bobot: 50% jangka pendek + 50% kualitas 60 hari)
            if stats_deep and stats_deep.historical_quality_score > 0:
                opp.composite_performance_score = round(
                    (base_comp_score * 0.5) + (stats_deep.historical_quality_score * 0.5),
                    1
                )
            else:
                opp.composite_performance_score = base_comp_score

            # Proteksi Delta Neutral Profesional:
            # Jika prediksi rate ke depan negatif, tolak
            if pred_rate <= 0:
                opp.is_eligible = False
                opp.rejection_reason = f"Prediksi funding rate ke depan negatif ({pred_rate*100:.4f}%)"
            elif opp.consistency_score_percent < 70.0:
                opp.is_eligible = False
                opp.rejection_reason = f"Konsistensi historis rendah ({opp.consistency_score_percent:.0f}% < 70%)"
            # Jika dalam 60 hari (2 bulan) koin sering berbalik negatif (> 6 kali flip)
            elif stats_deep and stats_deep.negative_flip_count > 6:
                opp.is_eligible = False
                opp.rejection_reason = f"Risiko flip 60 hari tinggi ({stats_deep.negative_flip_count}x rate negatif dalam 2 bulan)"
            elif stats_deep and stats_deep.sixty_day_cumulative_yield_pct <= 0.0 and stats_deep.sample_count >= 15:
                opp.is_eligible = False
                opp.rejection_reason = f"Total yield 60 hari (2 bulan) negatif ({stats_deep.sixty_day_cumulative_yield_pct:+.2f}%)"

        # Bersihkan data lama > 60 hari secara berkala (Auto-pruning 60 hari / 2 bulan)
        try:
            historical_store.prune_older_than_days(days=analysis_days)
        except Exception:
            pass

        # Urutkan final: Mengutamakan yang Eligible dengan Composite Performance Score tertinggi
        opportunities.sort(
            key=lambda x: (x.is_eligible, x.composite_performance_score, x.net_apy_percent),
            reverse=True
        )
        return opportunities

opportunity_finder = OpportunityFinder()
