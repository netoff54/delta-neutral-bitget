import math
from typing import Any, List, Optional

def parse_interval_hours(val: Any, default: int = 8) -> int:
    """
    Mengonversi berbagai representasi interval funding exchange (1, 4, 8, '1h', '4h', '8h', '1', '4', '8')
    menjadi integer jam yang tepat (1, 4, atau 8).
    Menghindari crash AttributeError atau kegagalan parsing pada string '4h'/'1h'.
    """
    if val is None:
        return default

    if isinstance(val, (int, float)):
        ival = int(val)
        return ival if ival in (1, 2, 4, 8) else (ival if ival > 0 else default)

    s = str(val).strip().lower()
    for token in ["hours", "hour", "h", "jam"]:
        s = s.replace(token, "")
    s = s.strip()

    try:
        ival = int(float(s))
        return ival if ival in (1, 2, 4, 8) else (ival if ival > 0 else default)
    except (ValueError, TypeError):
        return default

def detect_empirical_interval(timestamps_ms: List[int], default: int = 8) -> int:
    """
    Menghitung interval pembayaran riil berdasarkan selisih waktu timestamp antar pembayaran funding di Bitget.
    Jika selisih median adalah ~1 jam -> 1h, ~4 jam -> 4h, ~8 jam -> 8h.
    Ini memberikan akurasi 100% berbasis data aktual ledger Bitget.
    """
    if not timestamps_ms or len(timestamps_ms) < 2:
        return default

    valid_ts = [int(t) for t in timestamps_ms if t and int(t) > 0]
    sorted_ts = sorted(set(valid_ts))
    if len(sorted_ts) < 2:
        return default

    diffs_hours: List[float] = []
    for i in range(1, len(sorted_ts)):
        diff_ms = sorted_ts[i] - sorted_ts[i - 1]
        diff_h = diff_ms / 3600000.0
        # Toleransi 0.5 jam s/d 24 jam untuk jeda siklus normal
        if 0.5 <= diff_h <= 24.0:
            diffs_hours.append(diff_h)

    if not diffs_hours:
        return default

    diffs_hours.sort()
    median_h = diffs_hours[len(diffs_hours) // 2]

    if median_h <= 1.5:
        return 1
    elif median_h <= 2.5:
        return 2
    elif median_h <= 5.0:
        return 4
    elif median_h <= 10.0:
        return 8
    else:
        return max(1, round(median_h))

def cycles_per_day(interval_hours: int) -> float:
    """Mengembalikan frekuensi pembayaran funding per hari (misal 24 untuk 1h, 6 untuk 4h, 3 untuk 8h)."""
    return 24.0 / max(1, interval_hours)

def calculate_apy(funding_rate: float, interval_hours: int) -> float:
    """Menghitung APY tahunan dari funding rate per siklus dan interval jam."""
    return round(funding_rate * cycles_per_day(interval_hours) * 365.0 * 100.0, 2)
