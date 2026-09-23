import math
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from utils.logger import log
from core.models import HistoricalFundingStats

class FundingHistoryAnalyzer:
    """
    Engine Analisis Kuantitatif Historis 7 Hari (Rolling 7-Day Analytics):
    Menganalisis deret waktu funding rate 7 hari dan biaya taker pasar untuk menghasilkan
    indikator risiko & yield profesional hedge fund bagi sistem Delta-Neutral.
    """

    def analyze_7d_history(
        self,
        symbol: str,
        records: List[Dict[str, Any]],
        spot_taker_fee_pct: float = 0.10,
        perp_taker_fee_pct: float = 0.06,
        funding_interval_hours: int = 8,
        basis_spread_percent: float = 0.0
    ) -> HistoricalFundingStats:
        """
        Mengevaluasi deret waktu funding rate 7 hari untuk simbol tertentu.
        """
        rates = [float(r["funding_rate"]) if "funding_rate" in r else float(r.get("fundingRate", 0.0)) for r in records]
        
        if not rates:
            return HistoricalFundingStats(
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

        # Hitung durasi hari sampel (misal 21 siklus 8h = 7 hari)
        cycles_per_day = 24.0 / max(1, funding_interval_hours)
        effective_days = max(1.0, n / cycles_per_day)
        seven_day_apy = round((cum_yield / effective_days) * 365.0 * 100.0, 2)

        # Konsistensi positif & deteksi flip negatif
        positive_count = sum(1 for r in rates if r > 0)
        negative_flip_count = sum(1 for r in rates if r <= 0)
        consistency_pct = round((positive_count / n) * 100.0, 1)

        # Standar deviasi / volatilitas rate
        mean_rate = cum_yield / n
        variance = sum((r - mean_rate) ** 2 for r in rates) / n
        std_dev = math.sqrt(variance)
        std_dev_pct = round(std_dev * 100.0, 4)

        # Analisis Decay / Momentum Trajectory (Paruh Pertama vs Paruh Kedua 7 Hari)
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

        # Skor Kualitas Kuantitatif Komposit 7 Hari (0 - 100)
        # 1. Konsistensi Positif (Maks 30 poin)
        score_consistency = (consistency_pct / 100.0) * 30.0

        # 2. Proteksi Zero-Flip (Maks 25 poin, penalti -10 per flip negatif)
        score_flip = max(0.0, 25.0 - (negative_flip_count * 10.0))

        # 3. Besaran APY 7 Hari (Maks 25 poin, cap di 100% APY)
        score_apy = min(25.0, max(0.0, (seven_day_apy / 100.0) * 25.0))

        # 4. Kecepatan Balik Modal Biaya Taker (Maks 20 poin)
        # Jika balik modal <= 24 jam = 20 poin penuh, > 72 jam = 0 poin
        score_taker_recovery = max(0.0, (72.0 - min(72.0, recovery_hours)) / 72.0) * 20.0

        # 5. Penalti Volatilitas & Decay
        vol_penalty = min(15.0, (std_dev_pct / max(0.001, abs(effective_mean_rate_pct))) * 5.0)
        decay_penalty = 10.0 if decay_trend == "DECAYING" else 0.0

        composite_score = round(max(0.0, score_consistency + score_flip + score_apy + score_taker_recovery - vol_penalty - decay_penalty), 1)

        return HistoricalFundingStats(
            seven_day_cumulative_yield_pct=cum_yield_pct,
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

funding_history_analyzer = FundingHistoryAnalyzer()
