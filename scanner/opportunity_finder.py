import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional

from config.settings import settings
from utils.logger import log
from core.models import Opportunity
from core.bitget_client import BitgetClient, bitget_client
from analytics.fee_calculator import FeeCalculator, fee_calculator
from analytics.performance_scorer import PerformanceScorer, performance_scorer

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

            # Basis Spread (%)
            basis_spread_pct = ((perp_price - spot_price) / spot_price) * 100.0

            # Next funding time & interval dinamis (bisa 4h, 8h, 1h)
            interval_hours = int(fr_data.get("fundingInterval", 8))
            next_funding_ts = fr_data.get("nextUpdate") or fr_data.get("fundingTimestamp")
            next_funding_time = datetime.utcfromtimestamp(next_funding_ts / 1000.0) if next_funding_ts else None

            # Evaluasi Fee & Yield dengan interval dinamis
            eval_result = self.calculator.evaluate_opportunity(
                funding_rate=funding_rate,
                nominal_value_usdt=target_nominal_usdt,
                funding_interval_hours=interval_hours,
                basis_spread_percent=basis_spread_pct
            )

            # Cek likuiditas minimum
            is_eligible = eval_result["is_eligible"]
            rejection_reason = eval_result["rejection_reason"]

            # Cek apakah modal mencukupi batas minimum order exchange
            effective_leverage = min(2, settings.LEVERAGE)
            leg_nominal = target_nominal_usdt / (1.0 + (1.0 / effective_leverage))
            aligned_qty = self.client.align_quantity(
                spot_symbol=spot_symbol,
                perp_symbol=perp_symbol,
                target_nominal_usdt=leg_nominal,
                price=spot_price
            )
            if is_eligible and aligned_qty <= 0:
                is_eligible = False
                rejection_reason = f"Modal per leg (${leg_nominal:.1f}) di bawah minimum lot size exchange"

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

        # Urutkan sementara berdasarkan Net APY untuk memilih kandidat yang dianalisis mendalam
        opportunities.sort(key=lambda x: (x.is_eligible, x.net_apy_percent), reverse=True)

        # Analisis mendalam (10 siklus historis & prediktif) untuk kandidat teratas
        top_candidates = [o for o in opportunities if o.is_eligible][:10]
        if not top_candidates:
            # Jika tidak ada yang lolos kriteria keras, ambil top 5 untuk dianalisis
            top_candidates = opportunities[:5]

        for opp in top_candidates:
            history = await self.client.fetch_funding_rate_history(
                opp.perp_symbol,
                limit=settings.PREDICTIVE_HORIZON_CYCLES
            )
            opp.historical_funding_rates = [float(h["fundingRate"]) for h in history if h.get("fundingRate") is not None]

            # Analisis data historis
            hist_stats = performance_scorer.analyze_history(history)
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

            # Hitung skor performa komposit berbasis data (disesuaikan dengan interval 4h/8h/1h)
            opp.composite_performance_score = performance_scorer.calculate_composite_score(
                predicted_next_rate=pred_rate,
                historical_mean=opp.historical_mean_rate,
                consistency_pct=opp.consistency_score_percent,
                std_rate=opp.historical_std_rate,
                net_apy_percent=opp.net_apy_percent,
                break_even_hours=opp.break_even_hours,
                funding_interval_hours=opp.funding_interval_hours
            )

            # Jika prediksi rate ke depan negatif, jangan izinkan buka posisi
            if pred_rate <= 0:
                opp.is_eligible = False
                opp.rejection_reason = f"Prediksi funding rate ke depan negatif ({pred_rate*100:.4f}%)"
            elif opp.consistency_score_percent < 70.0:
                opp.is_eligible = False
                opp.rejection_reason = f"Konsistensi historis rendah ({opp.consistency_score_percent:.0f}% < 70%)"

        # Urutkan final: Mengutamakan yang Eligible dengan Composite Performance Score tertinggi
        opportunities.sort(
            key=lambda x: (x.is_eligible, x.composite_performance_score, x.net_apy_percent),
            reverse=True
        )
        return opportunities

opportunity_finder = OpportunityFinder()
