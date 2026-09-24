import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from contextlib import contextmanager

from config.settings import settings
from utils.logger import log

class UnifiedDatabase:
    """
    Arsitektur Database Terpadu (Unified Database Architecture) untuk Bot Arbitrase Delta-Neutral.
    Mendukung:
    1. SQLite Lokal (Default, zero-config di data/delta_neutral_history.db)
    2. Cloud PostgreSQL (via DATABASE_URL dari Neon, Supabase, Render Postgres, atau Railway)
    
    Menyimpan 6 Pilar Utama:
    - positions (Lifecycle posisi: entry, rebalance, exit, realized PnL, BEP status)
    - order_executions (Riwayat order Spot & Futures, fill price, fee exchange)
    - funding_harvests (Buku kas penerimaan bunga funding & compounding)
    - funding_history & potential_taker_snapshots (Intelligence data pasar 30 hari)
    - agentic_memory (Evolusi pembelajaran AI Gemini, aturan taktis, reputasi koin)
    - portfolio_snapshots (Kurva ekuitas & saldo trading dari waktu ke waktu)
    """

    def __init__(self, db_url: Optional[str] = None, sqlite_path: str = "data/delta_neutral_history.db"):
        if sqlite_path != "data/delta_neutral_history.db" and db_url is None:
            # Explicit isolated test path requested
            self.db_url = None
        else:
            self.db_url = db_url or getattr(settings, "DATABASE_URL", None)
        self.is_postgres = False
        self.sqlite_path = Path(sqlite_path)
        self._pg_pool = None

        if self.db_url and (self.db_url.startswith("postgres://") or self.db_url.startswith("postgresql://")):
            # Standardisasi protocol URI untuk psycopg2
            if self.db_url.startswith("postgres://"):
                self.db_url = self.db_url.replace("postgres://", "postgresql://", 1)
            self.is_postgres = True
            log.info("🌐 [Database] Terhubung ke Cloud PostgreSQL via DATABASE_URL (Data Abadi & Persisten).")
        else:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            log.info(f"💾 [Database] Menggunakan SQLite Lokal: {self.sqlite_path}")

        self._init_db()

    @contextmanager
    def _get_connection(self):
        """Menyediakan koneksi database dengan abstraksi cursor dictionary."""
        if self.is_postgres:
            try:
                import psycopg2
                import psycopg2.extras
                from psycopg2.pool import ThreadedConnectionPool

                if self._pg_pool is None:
                    self._pg_pool = ThreadedConnectionPool(
                        minconn=1,
                        maxconn=5,
                        dsn=self.db_url,
                        cursor_factory=psycopg2.extras.RealDictCursor
                    )

                conn = self._pg_pool.getconn()
                try:
                    if conn.closed != 0:
                        raise psycopg2.OperationalError("Connection is closed")
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    if self._pg_pool and not conn.closed:
                        self._pg_pool.putconn(conn)
            except Exception as e:
                log.error(f"[Database] Koneksi PostgreSQL gagal: {e}. Fallback ke SQLite lokal.")
                self.is_postgres = False
                self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
                with self._get_connection() as conn:
                    yield conn
        else:
            conn = sqlite3.connect(str(self.sqlite_path), timeout=15.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _format_query(self, query: str) -> str:
        """Mengubah placeholder '?' SQLite menjadi '%s' untuk PostgreSQL jika menggunakan Postgres."""
        if self.is_postgres:
            # Ganti ? dengan %s
            return query.replace("?", "%s")
        return query

    def _init_db(self):
        """Inisialisasi seluruh 6 tabel utama secara terpadu."""
        auto_inc = "SERIAL" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        primary_auto = f"id {auto_inc}" if self.is_postgres else "id INTEGER PRIMARY KEY AUTOINCREMENT"
        
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Tabel Positions (Lifecycle posisi aktif & historis)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    base_asset TEXT NOT NULL,
                    spot_symbol TEXT NOT NULL,
                    perp_symbol TEXT NOT NULL,
                    leverage INTEGER DEFAULT 1,
                    funding_interval_hours INTEGER DEFAULT 8,
                    spot_amount REAL NOT NULL,
                    perp_amount REAL NOT NULL,
                    entry_spot_price REAL NOT NULL,
                    entry_perp_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_spot_price REAL,
                    exit_perp_price REAL,
                    closed_at TEXT,
                    cumulative_funding_usdt REAL DEFAULT 0.0,
                    realized_pnl_usdt REAL DEFAULT 0.0,
                    net_delta REAL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    exit_reason TEXT,
                    is_bep_reached INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pos_base ON positions(base_asset)")

            # 2. Tabel Order Executions (Spot & Futures)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS order_executions (
                    {primary_auto},
                    order_id TEXT NOT NULL,
                    position_id TEXT,
                    market_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_amount REAL NOT NULL,
                    filled_amount REAL NOT NULL,
                    price REAL NOT NULL,
                    fee_paid REAL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_order_pos ON order_executions(position_id)")

            # 3. Tabel Funding Harvests (Buku Kas Bunga Funding)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS funding_harvests (
                    {primary_auto},
                    position_id TEXT NOT NULL,
                    base_asset TEXT NOT NULL,
                    perp_symbol TEXT NOT NULL,
                    funding_rate REAL NOT NULL,
                    funding_interval_hours INTEGER DEFAULT 8,
                    payment_usdt REAL NOT NULL,
                    compounded_amount_usdt REAL DEFAULT 0.0,
                    timestamp_utc TEXT NOT NULL,
                    transaction_id TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_harvest_base ON funding_harvests(base_asset)")

            # 4. Tabel Funding History (Intelligence Riwayat Pasar 30D)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS funding_history (
                    symbol TEXT NOT NULL,
                    timestamp_ms BIGINT NOT NULL,
                    datetime_utc TEXT NOT NULL,
                    funding_rate REAL NOT NULL,
                    funding_interval_hours INTEGER DEFAULT 8,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, timestamp_ms)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_funding_sym_time ON funding_history(symbol, timestamp_ms)")

            # 5. Tabel Potential Taker Snapshots
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS potential_taker_snapshots (
                    {primary_auto},
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

            # 6. Tabel Agentic Memory (Evolusi AI & Pembelajaran Kontinu)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS agi_experience_memory (
                    {primary_auto},
                    timestamp_utc TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    base_asset TEXT,
                    funding_rate REAL,
                    harvest_usdt REAL,
                    net_pnl_usdt REAL,
                    holding_hours REAL,
                    was_bep_reached INTEGER DEFAULT 0,
                    ai_decision TEXT,
                    lesson_learned TEXT,
                    tactical_rule TEXT,
                    pair_reputation_score REAL DEFAULT 0.0,
                    context_snapshot_json TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agi_base_event ON agi_experience_memory(base_asset, event_type)")

            # 7. Tabel Portfolio Snapshots (Kurva Ekuitas Saldo)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    {primary_auto},
                    timestamp_utc TEXT NOT NULL,
                    total_liquid_usdt REAL NOT NULL,
                    spot_free_usdt REAL NOT NULL,
                    swap_free_usdt REAL NOT NULL,
                    otc_free_usdt REAL NOT NULL,
                    active_positions_count INTEGER NOT NULL,
                    compounded_capital_usdt REAL NOT NULL,
                    total_profit_harvested_usdt REAL NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_port_time ON portfolio_snapshots(timestamp_utc)")

            # 8. Tabel Combined Balance History (Saldo Gabungan Multi-Market)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS combined_balance_history (
                    {primary_auto},
                    timestamp_utc TEXT NOT NULL,
                    total_combined_equity_usdt REAL NOT NULL,
                    spot_equity_usdt REAL NOT NULL,
                    spot_free_usdt REAL NOT NULL,
                    futures_equity_usdt REAL NOT NULL,
                    futures_free_usdt REAL NOT NULL,
                    futures_margin_used_usdt REAL NOT NULL,
                    futures_unrealized_pnl_usdt REAL NOT NULL,
                    otc_funding_equity_usdt REAL NOT NULL,
                    earn_savings_equity_usdt REAL NOT NULL,
                    active_positions_count INTEGER NOT NULL,
                    wallet_details_json TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_comb_time ON combined_balance_history(timestamp_utc)")

    # =========================================================================
    # PILAR 1: POSITIONS (LIFECYCLE POSISI)
    # =========================================================================
    def record_position(self, pos: Any):
        """Menyimpan atau memperbarui posisi delta-neutral."""
        now_iso = datetime.now(timezone.utc).isoformat()
        entry_iso = pos.entry_time.isoformat() if hasattr(pos.entry_time, "isoformat") else str(pos.entry_time)
        closed_iso = pos.closed_at.isoformat() if getattr(pos, "closed_at", None) and hasattr(pos.closed_at, "isoformat") else None

        sql = """
            INSERT INTO positions (
                position_id, base_asset, spot_symbol, perp_symbol, leverage,
                funding_interval_hours, spot_amount, perp_amount, entry_spot_price,
                entry_perp_price, entry_time, exit_spot_price, exit_perp_price,
                closed_at, cumulative_funding_usdt, realized_pnl_usdt, net_delta,
                status, exit_reason, is_bep_reached, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if self.is_postgres:
            sql += """
                ON CONFLICT (position_id) DO UPDATE SET
                    spot_amount = EXCLUDED.spot_amount,
                    perp_amount = EXCLUDED.perp_amount,
                    exit_spot_price = EXCLUDED.exit_spot_price,
                    exit_perp_price = EXCLUDED.exit_perp_price,
                    closed_at = EXCLUDED.closed_at,
                    cumulative_funding_usdt = EXCLUDED.cumulative_funding_usdt,
                    realized_pnl_usdt = EXCLUDED.realized_pnl_usdt,
                    net_delta = EXCLUDED.net_delta,
                    status = EXCLUDED.status,
                    exit_reason = EXCLUDED.exit_reason,
                    is_bep_reached = EXCLUDED.is_bep_reached,
                    updated_at = EXCLUDED.updated_at
            """
        else:
            sql = sql.replace("INSERT INTO", "INSERT OR REPLACE INTO")

        params = (
            pos.position_id,
            pos.base_asset,
            pos.spot_leg.symbol,
            pos.perp_leg.symbol,
            getattr(pos, "leverage", 1),
            getattr(pos, "funding_interval_hours", 8),
            float(pos.spot_leg.amount),
            float(pos.perp_leg.amount),
            float(pos.spot_leg.entry_price),
            float(pos.perp_leg.entry_price),
            entry_iso,
            getattr(pos.spot_leg, "current_price", None) if pos.status == "CLOSED" else None,
            getattr(pos.perp_leg, "current_price", None) if pos.status == "CLOSED" else None,
            closed_iso,
            float(getattr(pos, "cumulative_funding_received", 0.0) or getattr(pos, "realized_funding_usdt", 0.0) or 0.0),
            float(getattr(pos, "net_pnl_usdt", 0.0) or 0.0),
            float(getattr(pos, "net_delta", 0.0) or 0.0),
            pos.status,
            getattr(pos, "exit_reason", None),
            1 if getattr(pos, "is_bep_reached", False) else 0,
            now_iso,
            now_iso
        )

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), params)

    def close_position_in_db(self, position_id: str, exit_reason: str, net_pnl: float = 0.0):
        """Menandai posisi sebagai CLOSED di database."""
        now_iso = datetime.now(timezone.utc).isoformat()
        sql = """
            UPDATE positions SET
                status = 'CLOSED',
                exit_reason = ?,
                realized_pnl_usdt = ?,
                closed_at = ?,
                updated_at = ?
            WHERE position_id = ?
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (exit_reason, net_pnl, now_iso, now_iso, position_id))

    def get_positions_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Mengambil seluruh riwayat posisi dari database."""
        sql = "SELECT * FROM positions ORDER BY updated_at DESC LIMIT ?"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_active_positions_from_db(self) -> List[Dict[str, Any]]:
        """Mengambil posisi yang berstatus OPEN dari database."""
        sql = "SELECT * FROM positions WHERE status = 'OPEN'"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # PILAR 2: ORDER EXECUTIONS (RIWAYAT TRANSAKSI ORDER)
    # =========================================================================
    def record_order_execution(
        self,
        order_id: str,
        position_id: Optional[str],
        market_type: str,
        symbol: str,
        side: str,
        requested_amount: float,
        filled_amount: float,
        price: float,
        fee_paid: float = 0.0,
        status: str = "FILLED",
        timestamp_utc: Optional[str] = None
    ):
        """Merekam eksekusi order Spot / Futures ke database."""
        ts = timestamp_utc or datetime.now(timezone.utc).isoformat()
        sql = """
            INSERT INTO order_executions (
                order_id, position_id, market_type, symbol, side,
                requested_amount, filled_amount, price, fee_paid, status, timestamp_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (
                order_id, position_id, market_type, symbol, side,
                requested_amount, filled_amount, price, fee_paid, status, ts
            ))

    def get_orders_history(self, position_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Mengambil riwayat order yang pernah dieksekusi."""
        if position_id:
            sql = "SELECT * FROM order_executions WHERE position_id = ? ORDER BY id DESC LIMIT ?"
            params = (position_id, limit)
        else:
            sql = "SELECT * FROM order_executions ORDER BY id DESC LIMIT ?"
            params = (limit,)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # PILAR 3: FUNDING HARVESTS (BUKU KAS PENERIMAAN BUNGA & COMPOUNDING)
    # =========================================================================
    def record_funding_harvest(
        self,
        position_id: str,
        base_asset: str,
        perp_symbol: str,
        funding_rate: float,
        interval_hours: int,
        payment_usdt: float,
        compounded_amount_usdt: float = 0.0,
        transaction_id: Optional[str] = None,
        timestamp_utc: Optional[str] = None
    ):
        """Merekam penerimaan dividen funding fee ke buku kas database."""
        ts = timestamp_utc or datetime.now(timezone.utc).isoformat()
        sql = """
            INSERT INTO funding_harvests (
                position_id, base_asset, perp_symbol, funding_rate,
                funding_interval_hours, payment_usdt, compounded_amount_usdt,
                timestamp_utc, transaction_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (
                position_id, base_asset, perp_symbol, funding_rate,
                interval_hours, payment_usdt, compounded_amount_usdt,
                ts, transaction_id
            ))

    def get_harvest_history(self, base_asset: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Mengambil riwayat panen funding fee."""
        if base_asset:
            sql = "SELECT * FROM funding_harvests WHERE base_asset = ? ORDER BY id DESC LIMIT ?"
            params = (base_asset, limit)
        else:
            sql = "SELECT * FROM funding_harvests ORDER BY id DESC LIMIT ?"
            params = (limit,)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_total_harvested_profit(self) -> float:
        """Menghitung total profit funding fee yang telah dipanen sepanjang masa."""
        sql = "SELECT SUM(payment_usdt) as total FROM funding_harvests"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql))
            row = cursor.fetchone()
            if row:
                val = row["total"] if isinstance(row, dict) else row[0]
                return float(val or 0.0)
            return 0.0

    # =========================================================================
    # PILAR 4: MARKET INTELLIGENCE & SNAPSHOTS (HISTORICAL DATA)
    # =========================================================================
    def record_funding_rates(self, symbol: str, history: List[Dict[str, Any]], interval_hours: int = 8) -> int:
        """Menyimpan riwayat funding rate secara idempotent."""
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
            records.append((symbol, ts_ms, dt_iso, float(rate), interval_hours, now_iso))

        if not records:
            return 0

        sql = """
            INSERT INTO funding_history (
                symbol, timestamp_ms, datetime_utc, funding_rate, funding_interval_hours, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        if self.is_postgres:
            sql += " ON CONFLICT (symbol, timestamp_ms) DO NOTHING"
        else:
            sql = sql.replace("INSERT INTO", "INSERT OR REPLACE INTO")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(self._format_query(sql), records)

        return len(records)

    def get_funding_history(self, symbol: str, days: int = 30) -> List[Dict[str, Any]]:
        """Mengambil riwayat funding rate dalam kurun waktu N hari terakhir."""
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        sql = """
            SELECT symbol, timestamp_ms, datetime_utc, funding_rate, funding_interval_hours
            FROM funding_history
            WHERE symbol = ? AND timestamp_ms >= ?
            ORDER BY timestamp_ms ASC
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (symbol, cutoff_ms))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def record_taker_snapshot(self, snapshot: Any):
        """Menyimpan snapshot calon taker hasil pemindaian pasar."""
        sql = """
            INSERT INTO potential_taker_snapshots (
                timestamp_utc, base_asset, spot_symbol, perp_symbol,
                spot_price, perp_price, basis_spread_percent,
                current_funding_rate, predicted_next_rate, funding_interval_hours,
                spot_taker_fee_pct, perp_taker_fee_pct, round_trip_fee_pct,
                break_even_cycles, break_even_hours, composite_score,
                is_eligible, rejection_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            snapshot.timestamp.isoformat() if hasattr(snapshot.timestamp, "isoformat") else str(snapshot.timestamp),
            snapshot.base_asset,
            snapshot.spot_symbol,
            snapshot.perp_symbol,
            float(snapshot.spot_price),
            float(snapshot.perp_price),
            float(snapshot.basis_spread_percent),
            float(snapshot.current_funding_rate),
            float(snapshot.predicted_next_rate),
            int(snapshot.funding_interval_hours),
            float(snapshot.spot_taker_fee_pct),
            float(snapshot.perp_taker_fee_pct),
            float(snapshot.round_trip_fee_pct),
            int(snapshot.break_even_cycles),
            float(snapshot.break_even_hours),
            float(snapshot.composite_score),
            1 if snapshot.is_eligible else 0,
            snapshot.rejection_reason
        )
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), params)

    def get_latest_taker_snapshots(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Mengambil snapshot taker terkini."""
        sql = "SELECT * FROM potential_taker_snapshots ORDER BY id DESC LIMIT ?"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # PILAR 5: AGENTIC MEMORY & CONTINUOUS LEARNING
    # =========================================================================
    def record_agi_experience(
        self,
        event_type: str,
        base_asset: Optional[str] = None,
        funding_rate: Optional[float] = None,
        harvest_usdt: Optional[float] = None,
        net_pnl_usdt: Optional[float] = None,
        holding_hours: Optional[float] = None,
        was_bep_reached: bool = False,
        ai_decision: Optional[str] = None,
        lesson_learned: Optional[str] = None,
        tactical_rule: Optional[str] = None,
        pair_reputation_score: float = 0.0,
        context_snapshot: Optional[Dict[str, Any]] = None
    ):
        """Merekam pengalaman nyata pembelajaran AGI ke database."""
        now_iso = datetime.now(timezone.utc).isoformat()
        ctx_json = json.dumps(context_snapshot, default=str) if context_snapshot else None

        sql = """
            INSERT INTO agi_experience_memory (
                timestamp_utc, event_type, base_asset, funding_rate,
                harvest_usdt, net_pnl_usdt, holding_hours, was_bep_reached,
                ai_decision, lesson_learned, tactical_rule, pair_reputation_score,
                context_snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (
                now_iso, event_type, base_asset, funding_rate,
                harvest_usdt, net_pnl_usdt, holding_hours, 1 if was_bep_reached else 0,
                ai_decision, lesson_learned, tactical_rule, pair_reputation_score,
                ctx_json
            ))

    def get_agentic_memory(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Mengambil riwayat pembelajaran dan evaluasi AI dari database."""
        sql = "SELECT * FROM agi_experience_memory ORDER BY id DESC LIMIT ?"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_pair_reputation(self, base_asset: str) -> Dict[str, Any]:
        """Menghitung profil reputasi koin dari pengalaman trading nyata."""
        sql = """
            SELECT event_type, harvest_usdt, net_pnl_usdt, was_bep_reached, lesson_learned
            FROM agi_experience_memory
            WHERE base_asset = ?
            ORDER BY id DESC
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (base_asset,))
            rows = [dict(r) for r in cursor.fetchall()]

        if not rows:
            return {
                "base_asset": base_asset,
                "reputation_score": 0.5,
                "total_harvest_usdt": 0.0,
                "successful_harvests": 0,
                "emergency_exits": 0,
                "latest_lesson": "Belum ada pengalaman trading riil sebelumnya."
            }

        harvests = [r for r in rows if r["event_type"] == "HARVEST"]
        emergencies = [r for r in rows if "EMERGENCY" in str(r.get("event_type", ""))]
        total_harvest = sum(r.get("harvest_usdt") or 0.0 for r in harvests)

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
        """Mengambil skor reputasi untuk seluruh aset yang pernah ditradingkan."""
        sql = "SELECT DISTINCT base_asset FROM agi_experience_memory WHERE base_asset IS NOT NULL"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql))
            assets = [r["base_asset"] for r in cursor.fetchall()]
        return {a: self.get_pair_reputation(a)["reputation_score"] for a in assets}

    # =========================================================================
    # PILAR 6: PORTFOLIO SNAPSHOTS & EQUITY CURVE
    # =========================================================================
    def record_portfolio_snapshot(
        self,
        total_liquid_usdt: float,
        spot_free_usdt: float,
        swap_free_usdt: float,
        otc_free_usdt: float,
        active_positions_count: int,
        compounded_capital_usdt: float,
        total_profit_harvested_usdt: float,
        timestamp_utc: Optional[str] = None
    ):
        """Merekam snapshot saldo portofolio untuk melacak pertumbuhan modal."""
        ts = timestamp_utc or datetime.now(timezone.utc).isoformat()
        sql = """
            INSERT INTO portfolio_snapshots (
                timestamp_utc, total_liquid_usdt, spot_free_usdt, swap_free_usdt,
                otc_free_usdt, active_positions_count, compounded_capital_usdt,
                total_profit_harvested_usdt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (
                ts, total_liquid_usdt, spot_free_usdt, swap_free_usdt,
                otc_free_usdt, active_positions_count, compounded_capital_usdt,
                total_profit_harvested_usdt
            ))

    def get_portfolio_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Mengambil riwayat pertumbuhan portofolio (equity curve)."""
        sql = "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT ?"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # PILAR 8: COMBINED BALANCE HISTORY (SALDO GABUNGAN MULTI-MARKET)
    # =========================================================================
    def record_combined_balance(self, data: Dict[str, Any], active_positions_count: int = 1):
        """Merekam snapshot saldo gabungan seluruh pasar Bitget ke database."""
        now_iso = data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat()
        spot = data.get("spot", {})
        futures = data.get("futures", {})
        otc = data.get("otc", {})
        earn = data.get("earn", {})

        details_json = json.dumps({
            "spot_holdings": spot.get("holdings", []),
            "futures": futures,
            "earn_status": earn.get("status", "100% PROTECTED & UNTOUCHED")
        }, default=str)

        sql = """
            INSERT INTO combined_balance_history (
                timestamp_utc, total_combined_equity_usdt, spot_equity_usdt,
                spot_free_usdt, futures_equity_usdt, futures_free_usdt,
                futures_margin_used_usdt, futures_unrealized_pnl_usdt,
                otc_funding_equity_usdt, earn_savings_equity_usdt,
                active_positions_count, wallet_details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            now_iso,
            float(data.get("total_combined_equity_usdt", 0.0)),
            float(spot.get("equity_usdt", 0.0)),
            float(spot.get("free_usdt", 0.0)),
            float(futures.get("equity_usdt", 0.0)),
            float(futures.get("free_usdt", 0.0)),
            float(futures.get("margin_used_usdt", 0.0)),
            float(futures.get("unrealized_pnl_usdt", 0.0)),
            float(otc.get("equity_usdt", 0.0)),
            float(earn.get("equity_usdt", 0.0)),
            int(active_positions_count),
            details_json
        )
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), params)
            return cursor.lastrowid

    def get_combined_balance_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Mengambil riwayat saldo gabungan dari database."""
        sql = "SELECT * FROM combined_balance_history ORDER BY id DESC LIMIT ?"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_latest_combined_balance(self) -> Optional[Dict[str, Any]]:
        """Mengambil rekaman saldo gabungan paling mutakhir."""
        sql = "SELECT * FROM combined_balance_history ORDER BY id DESC LIMIT 1"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_equity_growth_stats(self, days: int = 7) -> Dict[str, Any]:
        """
        Menghitung statistik pertumbuhan portofolio dan stabilitas delta-neutral:
        - Persentase perubahan Net Worth (% 7D / 24H)
        - Standar deviasi / fluktuasi (Stabilitas hedging delta-neutral)
        """
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sql = """
            SELECT timestamp_utc, total_combined_equity_usdt, spot_equity_usdt, futures_equity_usdt
            FROM combined_balance_history
            WHERE timestamp_utc >= ?
            ORDER BY id ASC
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (cutoff_iso,))
            rows = [dict(r) for r in cursor.fetchall()]

        if not rows:
            return {
                "sample_count": 0,
                "growth_pct": 0.0,
                "net_change_usdt": 0.0,
                "stability_rating": "STABLE"
            }

        start_eq = float(rows[0]["total_combined_equity_usdt"])
        end_eq = float(rows[-1]["total_combined_equity_usdt"])
        net_change = end_eq - start_eq
        growth_pct = (net_change / start_eq * 100.0) if start_eq > 0 else 0.0

        return {
            "sample_count": len(rows),
            "start_equity_usdt": round(start_eq, 4),
            "current_equity_usdt": round(end_eq, 4),
            "net_change_usdt": round(net_change, 4),
            "growth_pct": round(growth_pct, 2),
            "stability_rating": "HIGHLY_STABLE" if abs(growth_pct) < 15.0 else "MODERATE"
        }

    def prune_older_than_days(self, days: int = 30) -> Dict[str, int]:
        """Pembersihan berkala data historis non-kritis (funding rate mentah)."""
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            del_fund = "DELETE FROM funding_history WHERE timestamp_ms < ?"
            cursor.execute(self._format_query(del_fund), (cutoff_ms,))
            pruned_funding = cursor.rowcount

            del_snap = "DELETE FROM potential_taker_snapshots WHERE timestamp_utc < ?"
            cursor.execute(self._format_query(del_snap), (cutoff_iso,))
            pruned_snapshots = cursor.rowcount

        return {
            "pruned_funding_records": pruned_funding,
            "pruned_snapshots": pruned_snapshots
        }

    # =========================================================================
    # PILAR 9: PNL MULTI-TIMEFRAME ANALYTICS (1D / 1W / 1M / 1Y)
    # =========================================================================
    def get_pnl_for_timeframe(self, days: int) -> Dict[str, Any]:
        """
        Menghitung PnL portofolio untuk timeframe tertentu berdasarkan data REAL dari Bitget.
        Membandingkan saldo gabungan awal vs saldo saat ini untuk menghitung pertumbuhan nyata.

        Args:
            days: Jumlah hari ke belakang (1=1 hari, 7=1 minggu, 30=1 bulan, 365=1 tahun)

        Returns:
            Dict dengan start_equity, current_equity, pnl_usdt, pnl_pct, funding_harvested,
            positions_closed, avg_daily_yield
        """
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # 1. Ambil saldo awal pada periode tersebut
        sql_start = """
            SELECT total_combined_equity_usdt, timestamp_utc
            FROM combined_balance_history
            WHERE timestamp_utc >= ?
            ORDER BY timestamp_utc ASC LIMIT 1
        """
        # 2. Ambil saldo terkini
        sql_current = """
            SELECT total_combined_equity_usdt, timestamp_utc
            FROM combined_balance_history
            ORDER BY id DESC LIMIT 1
        """
        # 3. Hitung total funding harvested dalam periode
        sql_harvest = """
            SELECT COALESCE(SUM(payment_usdt), 0.0) as total_harvest
            FROM funding_harvests
            WHERE timestamp_utc >= ?
        """
        # 4. Hitung posisi yang ditutup dalam periode
        sql_positions = """
            SELECT COUNT(*) as closed_count, COALESCE(SUM(realized_pnl_usdt), 0.0) as total_realized_pnl
            FROM positions
            WHERE status = 'CLOSED' AND closed_at >= ?
        """

        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(self._format_query(sql_start), (cutoff_iso,))
            row_start = cursor.fetchone()

            cursor.execute(self._format_query(sql_current))
            row_current = cursor.fetchone()

            cursor.execute(self._format_query(sql_harvest), (cutoff_iso,))
            row_harvest = cursor.fetchone()

            cursor.execute(self._format_query(sql_positions), (cutoff_iso,))
            row_pos = cursor.fetchone()

        start_equity = float(row_start["total_combined_equity_usdt"] if row_start else 0.0)
        current_equity = float(row_current["total_combined_equity_usdt"] if row_current else 0.0)
        total_harvest = float(row_harvest["total_harvest"] if row_harvest else 0.0)
        closed_count = int(row_pos["closed_count"] if row_pos else 0)
        realized_pnl = float(row_pos["total_realized_pnl"] if row_pos else 0.0)

        pnl_usdt = current_equity - start_equity
        pnl_pct = (pnl_usdt / start_equity * 100.0) if start_equity > 0 else 0.0
        avg_daily = pnl_usdt / days if days > 0 else 0.0

        return {
            "timeframe_days": days,
            "timeframe_label": self._days_to_label(days),
            "start_equity_usdt": round(start_equity, 4),
            "current_equity_usdt": round(current_equity, 4),
            "pnl_usdt": round(pnl_usdt, 4),
            "pnl_pct": round(pnl_pct, 2),
            "funding_harvested_usdt": round(total_harvest, 4),
            "positions_closed": closed_count,
            "realized_pnl_usdt": round(realized_pnl, 4),
            "avg_daily_pnl_usdt": round(avg_daily, 4),
            "annualized_yield_pct": round(pnl_pct / days * 365.0, 2) if days > 0 and start_equity > 0 else 0.0,
            "has_data": start_equity > 0 and current_equity > 0
        }

    def get_all_pnl_timeframes(self) -> Dict[str, Any]:
        """
        Mengambil PnL untuk semua timeframe sekaligus: 1 Hari, 1 Minggu, 1 Bulan, 1 Tahun.
        Menggunakan data REAL yang tersimpan dari Bitget API.
        """
        results = {}
        for days in [1, 7, 30, 365]:
            label = self._days_to_label(days)
            results[label] = self.get_pnl_for_timeframe(days)

        # Tambahkan ringkasan total keseluruhan
        total_harvest = self.get_total_harvested_profit()
        latest = self.get_latest_combined_balance()
        results["all_time"] = {
            "timeframe_label": "All Time",
            "total_funding_harvested_usdt": round(total_harvest, 4),
            "current_equity_usdt": float(latest.get("total_combined_equity_usdt", 0.0)) if latest else 0.0
        }

        return results

    def _days_to_label(self, days: int) -> str:
        """Konversi jumlah hari ke label yang mudah dibaca."""
        mapping = {1: "1D", 7: "1W", 30: "1M", 365: "1Y"}
        return mapping.get(days, f"{days}D")

    def get_pnl_equity_curve(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        Mengambil kurva ekuitas untuk periode tertentu (equity curve).
        Berguna untuk visualisasi pertumbuhan portofolio.
        """
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sql = """
            SELECT timestamp_utc, total_combined_equity_usdt, spot_equity_usdt,
                   futures_equity_usdt, otc_funding_equity_usdt, earn_savings_equity_usdt
            FROM combined_balance_history
            WHERE timestamp_utc >= ?
            ORDER BY timestamp_utc ASC
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._format_query(sql), (cutoff_iso,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

db = UnifiedDatabase()
