import math
from typing import List, Dict, Any, Tuple
from datetime import datetime

from config.settings import settings
from utils.logger import log

class PerformanceScorer:
    """
    Engine Analisis Prediktif & Scoring Kuantitatif:
    1. Memproyeksikan estimasi funding rate siklus berikutnya (Forward-Looking)
       berdasarkan basis spread spot-futures dan momentum.
    2. Menghitung statistik historis 10 siklus terakhir (Mean, Volatilitas/Std, Rasio Konsistensi).
    3. Meranking seluruh peluang berdasarkan skor komposit performa terbaik.
    """

    def analyze_history(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Menganalisis riwayat siklus funding rate.
        """
        rates = [float(h["fundingRate"]) for h in history if h.get("fundingRate") is not None]
        if not rates:
            return {
                "mean_rate": 0.0,
                "std_rate": 0.0,
                "consistency_pct": 0.0,
                "trend": "STABLE",
                "latest_rate": 0.0,
                "rates_count": 0
            }

        n = len(rates)
        mean_rate = sum(rates) / n
        variance = sum((r - mean_rate) ** 2 for r in rates) / n
        std_rate = math.sqrt(variance)

        positive_count = sum(1 for r in rates if r > 0)
        consistency_pct = (positive_count / n) * 100.0

        # Tentukan tren momentum (perbandingan rata-rata 3 siklus terbaru vs rata-rata keseluruhan)
        recent_window = rates[-3:] if n >= 3 else rates
        recent_avg = sum(recent_window) / len(recent_window)

        if recent_avg > mean_rate * 1.10:
            trend = "UP"
        elif recent_avg < mean_rate * 0.90:
            trend = "DOWN"
        else:
            trend = "STABLE"

        return {
            "mean_rate": mean_rate,
            "std_rate": std_rate,
            "consistency_pct": round(consistency_pct, 1),
            "trend": trend,
            "latest_rate": rates[-1],
            "rates_count": n
        }

    def predict_next_funding_rate(
        self,
        current_funding_rate: float,
        basis_spread_percent: float,
        trend: str,
        historical_mean: float
    ) -> float:
        """
        Model prediktif ke depan (Forward-Looking):
        Memproyeksikan rate siklus berikutnya menggunakan kombinasi:
        - Current Rate (40%)
        - Basis Spread (Futures vs Spot Premium) (30%)
        - Historical Mean & Trend Momentum (30%)
        """
        # Pengaruh basis spread: jika perp lebih mahal dari spot, funding rate terdorong naik
        spread_bias = (basis_spread_percent / 100.0) * 0.15

        # Pengaruh tren momentum
        momentum_factor = 1.05 if trend == "UP" else (0.92 if trend == "DOWN" else 1.0)

        predicted_rate = (
            (current_funding_rate * 0.40) +
            (spread_bias * 0.30) +
            (historical_mean * 0.30)
        ) * momentum_factor

        # Clamp nilai wajar (-0.75% s/d +0.75% per siklus)
        return round(max(-0.0075, min(0.0075, predicted_rate)), 6)

    def calculate_composite_score(
        self,
        predicted_next_rate: float,
        historical_mean: float,
        consistency_pct: float,
        std_rate: float,
        net_apy_percent: float,
        break_even_hours: float,
        funding_interval_hours: int = 8,
        basis_spread_percent: float = 0.0,
        volume_24h_usdt: float = 10000.0,
        flip_free_days: float = 365.0,
    ) -> float:
        """
        Menghitung Skor Performa Komposit Kuantitatif (Bobot 0 - 100):
        - 25%: Kecepatan BEP Tercepat (Break-even hours terpendek)
        - 25%: Bunga Tertinggi (Current / Predicted weekly funding yield)
        - 25%: Waktu Tercepat Mencapai Target 2.5% Profit Bersih (Setelah BEP Lunas)
        - 15%: Kualitas Basis Spread Positif & Proteksi Modal (Cegah rugi di bawah modal awal)
        - 10%: Durasi Bebas Flip Historis (5%) & Likuiditas Pasar $10k+ (5%)
        """
        cycles_per_day = 24.0 / max(1, funding_interval_hours)
        weekly_yield_pct = (predicted_next_rate * cycles_per_day * 7.0) * 100.0

        # 1. Bobot 25%: Kecepatan BEP Tercepat (BEP <= 8 jam = 25 poin penuh, 72 jam = 0 poin)
        score_bep = max(0.0, (72.0 - min(72.0, break_even_hours)) / 72.0) * 25.0

        # 2. Bobot 25%: Bunga Tertinggi (Yield mingguan 2.5%+ = 25 poin penuh)
        score_yield = max(0.0, min(25.0, (weekly_yield_pct / 2.5) * 25.0))

        # 3. Bobot 25%: Waktu Tercepat Mencapai Target 2.5% Profit Bersih (Setelah BEP Lunas)
        rate_per_hour_pct = (predicted_next_rate * 100.0) / max(1, funding_interval_hours)
        if rate_per_hour_pct > 0:
            hours_to_target_net = break_even_hours + (2.5 / rate_per_hour_pct)
        else:
            hours_to_target_net = 9999.0
        # Target mingguan tercapai <= 168 jam (7 hari) = 25 poin penuh
        score_target_speed = max(0.0, min(25.0, (168.0 / max(1.0, hours_to_target_net)) * 25.0))

        # 4. Bobot 15%: Kualitas Basis Spread Positif (Proteksi Modal agar tidak rugi modal awal)
        score_spread = min(15.0, max(0.0, (basis_spread_percent / 0.05) * 15.0)) if basis_spread_percent > 0 else 0.0

        # 5. Bobot 10%: Durasi Bebas Flip (5%) & Likuiditas Pasar $10k+ (5%)
        score_flip = min(5.0, (flip_free_days / 365.0) * 5.0)
        score_liq = min(5.0, (volume_24h_usdt / 50000.0) * 5.0) if volume_24h_usdt >= 10000.0 else 0.0

        composite_score = score_bep + score_yield + score_target_speed + score_spread + score_flip + score_liq
        return round(max(0.0, composite_score), 2)

performance_scorer = PerformanceScorer()
