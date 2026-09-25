import math
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from utils.logger import log
from core.models import HistoricalFundingStats

class FundingHistoryAnalyzer:
    """
    Engine Analisis Kuantitatif Historis 60 Hari (2 Bulan), 30 Hari, & 7 Hari (Rolling 60-Day In-Depth Analytics):
    Membaca data historis langsung dari Bitget hingga 60 hari (2 bulan) ke belakang untuk mengukur:
    1. 60-Day Cumulative Yield & Long-Term Expectancy
    2. 60-Day vs 30-Day vs 7-Day APY & Yield Momentum
    3. 60-Day Negative Flip Frequency (Zero-tolerance filter jangka panjang)
    4. Volatilitas & Decay Trajectory 2 Bulan
    5. Taker Fee Recovery Period (Amortisasi biaya taker)
    """

    def analyze_history_deep(
        self,
        symbol: str,
        records: List[Dict[str, Any]],
        spot_taker_fee_pct: float = 0.10,
        perp_taker_fee_pct: float = 0.06,
        funding_interval_hours: int = 8,
        basis_spread_percent: float = 0.0
    ) -> HistoricalFundingStats:
        """
        Mengevaluasi deret waktu funding rate historis hingga 60 hari (2 bulan) dari Bitget.
        """
        rates = [float(r["funding_rate"]) if "funding_rate" in r else float(r.get("fundingRate", 0.0)) for r in records]
        
        if not rates:
            return HistoricalFundingStats(
                sixty_day_cumulative_yield_pct=0.0,
                sixty_day_apy_pct=0.0,
                sixty_day_flip_count=0,
                thirty_day_cumulative_yield_pct=0.0,
                thirty_day_apy_pct=0.0,
                thirty_day_flip_count=0,
                seven_day_cumulative_yield_pct=0.0,
                seven_day_apy_pct=0.0,
                positive_consistency_pct=100.0,
                negative_flip_count=0,
                rate_std_dev=0.0,
                decay_trend="STABLE",
                taker_fee_recovery_cycles=99,
                taker_fee_recovery_hours=999.0,
                historical_quality_score=0.0,
                sample_count=0
            )

        n = len(rates)
        cum_yield = sum(rates)
        cum_yield_pct = round(cum_yield * 100.0, 4)

        cycles_per_day = 24.0 / max(1, funding_interval_hours)
        effective_days = max(1.0, n / cycles_per_day)

        # 60-Day APY
        sixty_day_apy = round((cum_yield / effective_days) * 365.0 * 100.0, 2)

        # Sub-window 30 Hari
        sub_30d_count = min(n, int(math.ceil(30.0 * cycles_per_day)))
        rates_30d = rates[-sub_30d_count:] if sub_30d_count > 0 else rates
        cum_30d_yield = sum(rates_30d)
        cum_30d_yield_pct = round(cum_30d_yield * 100.0, 4)
        days_30d = max(1.0, len(rates_30d) / cycles_per_day)
        thirty_day_apy = round((cum_30d_yield / days_30d) * 365.0 * 100.0, 2)
        thirty_day_flips = sum(1 for r in rates_30d if r <= 0)

        # Sub-window 7 Hari (21 siklus terakhir jika interval 8h)
        sub_7d_count = min(n, int(math.ceil(7.0 * cycles_per_day)))
        rates_7d = rates[-sub_7d_count:] if sub_7d_count > 0 else rates
        cum_7d_yield = sum(rates_7d)
        cum_7d_yield_pct = round(cum_7d_yield * 100.0, 4)
        days_7d = max(1.0, len(rates_7d) / cycles_per_day)
        seven_day_apy = round((cum_7d_yield / days_7d) * 365.0 * 100.0, 2)

        # Konsistensi positif & deteksi flip negatif dalam horizon 60 hari
        positive_count = sum(1 for r in rates if r > 0)
        negative_flip_count = sum(1 for r in rates if r <= 0)
        consistency_pct = round((positive_count / n) * 100.0, 1)

        # Standar deviasi / volatilitas rate
        mean_rate = cum_yield / n
        variance = sum((r - mean_rate) ** 2 for r in rates) / n
        std_dev = math.sqrt(variance)
        std_dev_pct = round(std_dev * 100.0, 4)

        # Analisis Decay / Momentum Trajectory (Paruh Pertama vs Paruh Kedua)
        if n >= 4:
            mid = n // 2
            first_half = rates[:mid]
            second_half = rates[mid:]
            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)

            if avg_second > avg_first * 1.15:
                decay_trend = "ACCELERATING"
            elif avg_second < avg_first * 0.85:
                decay_trend = "DECAYING"
            else:
                decay_trend = "STABLE"
        else:
            decay_trend = "STABLE"

        # Analisis Amortisasi Biaya Taker Round-Trip
        round_trip_taker_pct = (spot_taker_fee_pct + perp_taker_fee_pct) * 2.0
        effective_taker_drag_pct = max(0.01, round_trip_taker_pct - basis_spread_percent)
        
        effective_mean_rate_pct = mean_rate * 100.0
        if effective_mean_rate_pct > 0:
            recovery_cycles = math.ceil(effective_taker_drag_pct / effective_mean_rate_pct)
            recovery_hours = round(recovery_cycles * funding_interval_hours, 1)
        else:
            recovery_cycles = 999
            recovery_hours = 9999.0

        # Skor Kualitas Kuantitatif Komposit 60 Hari (0 - 100)
        # 1. Konsistensi Positif (Maks 30 poin)
        score_consistency = (consistency_pct / 100.0) * 30.0

        # 2. Proteksi Zero-Flip (Maks 25 poin, penalti -3.5 per flip negatif dalam 60 hari)
        score_flip = max(0.0, 25.0 - (negative_flip_count * 3.5))

        # 3. Besaran APY (Maks 25 poin, cap di 100% APY, bobot gabungan 60D dan 30D)
        blended_apy = (sixty_day_apy * 0.5) + (thirty_day_apy * 0.5)
        score_apy = min(25.0, max(0.0, (blended_apy / 100.0) * 25.0))

        # 4. Kecepatan Balik Modal Biaya Taker (Maks 20 poin)
        score_taker_recovery = max(0.0, (72.0 - min(72.0, recovery_hours)) / 72.0) * 20.0

        # 5. Penalti Volatilitas & Decay
        vol_penalty = min(15.0, (std_dev_pct / max(0.001, abs(effective_mean_rate_pct))) * 5.0)
        decay_penalty = 10.0 if decay_trend == "DECAYING" else 0.0

        composite_score = round(max(0.0, score_consistency + score_flip + score_apy + score_taker_recovery - vol_penalty - decay_penalty), 1)

        return HistoricalFundingStats(
            sixty_day_cumulative_yield_pct=cum_yield_pct,
            sixty_day_apy_pct=sixty_day_apy,
            sixty_day_flip_count=negative_flip_count,
            thirty_day_cumulative_yield_pct=cum_30d_yield_pct,
            thirty_day_apy_pct=thirty_day_apy,
            thirty_day_flip_count=thirty_day_flips,
            seven_day_cumulative_yield_pct=cum_7d_yield_pct,
            seven_day_apy_pct=seven_day_apy,
            positive_consistency_pct=consistency_pct,
            negative_flip_count=negative_flip_count,
            rate_std_dev=std_dev_pct,
            decay_trend=decay_trend,
            taker_fee_recovery_cycles=recovery_cycles,
            taker_fee_recovery_hours=recovery_hours,
            historical_quality_score=composite_score,
            sample_count=n
        )

    # Alias untuk kompatibilitas
    analyze_60d_history = analyze_history_deep
    analyze_30d_history = analyze_history_deep
    analyze_7d_history = analyze_history_deep

funding_history_analyzer = FundingHistoryAnalyzer()
