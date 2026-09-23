import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from utils.logger import log
from core.models import TakerSnapshot

class HistoricalStore:
    """
    Penyimpanan Persisten Data Historis 7 Hari (Rolling 7-Day Storage)
    menggunakan SQLite database lokal di data/delta_neutral_history.db.

    Menyimpan:
    1. Riwayat Funding Rate per koin (7 hari rolling window).
    2. Snapshot Taker Potensial (spread basis, taker fee, BEP, prediksi rate).
    3. Akumulasi Memori Pengalaman AGI (Experiential Learning & Pair Reputation).
    """

    def __init__(self, db_path: str = "data/delta_neutral_history.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Membuat tabel jika belum ada dan membuat indeks untuk performa cepat."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Tabel Funding Rate History
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS funding_history (
                    symbol TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    datetime_utc TEXT NOT NULL,
                    funding_rate REAL NOT NULL,
                    funding_interval_hours INTEGER DEFAULT 8,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, timestamp_ms)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_funding_sym_time ON funding_history(symbol, timestamp_ms)")

            # 2. Tabel Snapshot Taker Potensial
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS potential_taker_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    base_asset TEXT NOT NULL,
                    spot_symbol TEXT NOT NULL,
                    perp_symbol TEXT NOT NULL,
                    spot_price REAL NOT NULL,
                    perp_price REAL NOT NULL,
                    basis_spread_percent REAL NOT NULL,
                    current_funding_rate REAL NOT NULL,
                    predicted_next_rate REAL NOT NULL,
                    funding_interval_hours INTEGER NOT NULL,
                    spot_taker_fee_pct REAL NOT NULL,
                    perp_taker_fee_pct REAL NOT NULL,
                    round_trip_fee_pct REAL NOT NULL,
                    break_even_cycles INTEGER NOT NULL,
                    break_even_hours REAL NOT NULL,
                    composite_score REAL NOT NULL,
                    is_eligible INTEGER NOT NULL,
                    rejection_reason TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_taker_base_time ON potential_taker_snapshots(base_asset, timestamp_utc)")

            # 3. Tabel Akumulasi Pengalaman AGI (Experiential Memory & Heuristics)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agi_experience_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    base_asset TEXT NOT NULL,
                    funding_rate REAL,
                    harvest_usdt REAL,
                    net_pnl_usdt REAL,
                    holding_hours REAL,
                    was_bep_reached INTEGER DEFAULT 0,
                    ai_decision TEXT,
                    lesson_learned TEXT,
                    tactical_rule TEXT,
                    pair_reputation_score REAL DEFAULT 0.0
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agi_base_event ON agi_experience_memory(base_asset, event_type)")
            conn.commit()

    def record_funding_rates(self, symbol: str, history: List[Dict[str, Any]], interval_hours: int = 8) -> int:
        """
        Menyimpan riwayat funding rate untuk pasangan tertentu secara idempotent (INSERT OR REPLACE).
        Mengembalikan jumlah entri yang berhasil disimpan.
        """
        if not history:
            return 0

        now_iso = datetime.now(timezone.utc).isoformat()
        records = []
        for h in history:
            rate = h.get("fundingRate")
            if rate is None:
                continue

            ts = h.get("fundingTimestamp") or h.get("timestamp") or h.get("fundingTime")
            if not ts:
                continue

            ts_ms = int(ts)
            dt_iso = datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc).isoformat()
            records.append((
                symbol,
                ts_ms,
                dt_iso,
                float(rate),
                interval_hours,
                now_iso
            ))

        if not records:
            return 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO funding_history (
                    symbol, timestamp_ms, datetime_utc, funding_rate, funding_interval_hours, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, records)
            conn.commit()

        return len(records)

    def get_funding_history(self, symbol: str, days: int = 30) -> List[Dict[str, Any]]:
        """
        Mengambil riwayat funding rate dalam kurun waktu `days` terakhir (default 30 hari),
        diurutkan dari waktu tertua ke terbaru.
        """
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT symbol, timestamp_ms, datetime_utc, funding_rate, funding_interval_hours
                FROM funding_history
                WHERE symbol = ? AND timestamp_ms >= ?
                ORDER BY timestamp_ms ASC
            """, (symbol, cutoff_ms))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def record_taker_snapshot(self, snapshot: TakerSnapshot):
        """Menyimpan snapshot data taker potensial untuk analisis kuantitatif berkala."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO potential_taker_snapshots (
                    timestamp_utc, base_asset, spot_symbol, perp_symbol,
                    spot_price, perp_price, basis_spread_percent,
                    current_funding_rate, predicted_next_rate, funding_interval_hours,
                    spot_taker_fee_pct, perp_taker_fee_pct, round_trip_fee_pct,
                    break_even_cycles, break_even_hours, composite_score,
                    is_eligible, rejection_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.timestamp.isoformat(),
                snapshot.base_asset,
                snapshot.spot_symbol,
                snapshot.perp_symbol,
                snapshot.spot_price,
                snapshot.perp_price,
                snapshot.basis_spread_percent,
                snapshot.current_funding_rate,
                snapshot.predicted_next_rate,
                snapshot.funding_interval_hours,
                snapshot.spot_taker_fee_pct,
                snapshot.perp_taker_fee_pct,
                snapshot.round_trip_fee_pct,
                snapshot.break_even_cycles,
                snapshot.break_even_hours,
                snapshot.composite_score,
                1 if snapshot.is_eligible else 0,
                snapshot.rejection_reason
            ))
            conn.commit()

    def record_agi_experience(
        self,
        event_type: str,
        base_asset: str,
        funding_rate: Optional[float] = None,
        harvest_usdt: Optional[float] = None,
        net_pnl_usdt: Optional[float] = None,
        holding_hours: Optional[float] = None,
        was_bep_reached: bool = False,
        ai_decision: Optional[str] = None,
        lesson_learned: Optional[str] = None,
        tactical_rule: Optional[str] = None,
        pair_reputation_score: float = 0.0
    ):
        """Merekam peristiwa pembelajaran nyata AGI (Harvest, Rotation, Emergency Exit, dsb)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agi_experience_memory (
                    timestamp_utc, event_type, base_asset, funding_rate,
                    harvest_usdt, net_pnl_usdt, holding_hours, was_bep_reached,
                    ai_decision, lesson_learned, tactical_rule, pair_reputation_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_iso, event_type, base_asset, funding_rate,
                harvest_usdt, net_pnl_usdt, holding_hours, 1 if was_bep_reached else 0,
                ai_decision, lesson_learned, tactical_rule, pair_reputation_score
            ))
            conn.commit()

    def get_pair_reputation(self, base_asset: str) -> Dict[str, Any]:
        """
        Menghitung profil reputasi koin berdasarkan riwayat trading nyata:
        - Total panen funding yang berhasil
        - Frekuensi exit darurat / rate flip
        - Skor reputasi komposit (-1.0 s/d +1.0)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT event_type, harvest_usdt, net_pnl_usdt, was_bep_reached, lesson_learned
                FROM agi_experience_memory
                WHERE base_asset = ?
                ORDER BY id DESC
            """, (base_asset,))
            rows = [dict(r) for r in cursor.fetchall()]

        if not rows:
            return {
                "base_asset": base_asset,
                "reputation_score": 0.5, # Netral positif default
                "total_harvest_usdt": 0.0,
                "successful_harvests": 0,
                "emergency_exits": 0,
                "latest_lesson": "Belum ada pengalaman trading riil sebelumnya."
            }

        harvests = [r for r in rows if r["event_type"] == "HARVEST"]
        emergencies = [r for r in rows if "EMERGENCY" in r["event_type"]]
        total_harvest = sum(r.get("harvest_usdt") or 0.0 for r in harvests)

        # Reputasi naik jika panen sukses, anjlok jika ada emergency exit
        rep_score = 0.5 + (len(harvests) * 0.1) - (len(emergencies) * 0.4)
        rep_score = max(-1.0, min(1.0, rep_score))

        latest_lesson = rows[0].get("lesson_learned") or "Performa stabil."
        return {
            "base_asset": base_asset,
            "reputation_score": round(rep_score, 2),
            "total_harvest_usdt": round(total_harvest, 4),
            "successful_harvests": len(harvests),
            "emergency_exits": len(emergencies),
            "latest_lesson": latest_lesson
        }

    def get_all_pair_reputations(self) -> Dict[str, float]:
        """Mengambil dictionary pemetaan base_asset -> reputation_score."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT base_asset FROM agi_experience_memory")
            assets = [r["base_asset"] for r in cursor.fetchall()]
        return {a: self.get_pair_reputation(a)["reputation_score"] for a in assets}

    def prune_older_than_days(self, days: int = 30) -> Dict[str, int]:
        """
        Pembersihan berkala (Rolling 30 Days Maintenance):
        Menghapus data funding rate dan snapshot taker yang lebih tua dari `days` hari.
        Mempertahankan ukuran database tetap sangat kecil (< 5 MB) dan performa tetap kilat.
        """
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM funding_history WHERE timestamp_ms < ?", (cutoff_ms,))
            pruned_funding = cursor.rowcount

            cursor.execute("DELETE FROM potential_taker_snapshots WHERE timestamp_utc < ?", (cutoff_iso,))
            pruned_snapshots = cursor.rowcount

            conn.commit()

        return {
            "pruned_funding_records": pruned_funding,
            "pruned_snapshots": pruned_snapshots
        }

historical_store = HistoricalStore()
