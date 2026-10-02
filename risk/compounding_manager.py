import json
import math
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, Field

from config.settings import settings
from utils.logger import log

class HarvestEvent(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    position_id: str
    base_asset: str
    profit_usdt: float
    funding_rate: float
    compounded_balance_after: float

class CompoundingState(BaseModel):
    initial_seed_capital: float = 0.0
    total_profit_compounded: float = 0.0
    current_compounded_capital: float = 0.0
    harvest_events_count: int = 0
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class CompoundingManager:
    """
    Manajer Compounding & Proteksi Saldo Bitget Earn.

    Prinsip Utama:
    1. Modal 100% dari saldo REAL Bitget — tidak ada angka fiktif/mock.
    2. Compounding: seluruh profit funding fee diputar kembali ke modal trading.
    3. Isolasi Mutlak Earn: dana di Bitget Earn/Savings TIDAK PERNAH disentuh.
    4. Deposit Detection: jika user tambah saldo, baseline otomatis naik — dan
       vault_locked diperhitungkan agar tidak salah hitung.
    """

    def __init__(self, state_file: str = "data/compounding_state.json"):
        self.state_path = Path(state_file)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        default_seed = float(getattr(settings, "INITIAL_SEED_CAPITAL_USDT", 64.0) or 64.0)
        self.initial_seed: float = default_seed
        self.total_profit_compounded: float = 0.0
        self.current_capital: float = default_seed
        self.events: List[HarvestEvent] = []
        self.load_state()

    def load_state(self):
        """
        Muat riwayat compounding dari Database (Ground-Truth Riil Bitget).
        Prioritas utama: sinkronisasi 100% dari buku kas riil di database PostgreSQL.
        Fallback ke disk json jika DB tidak tersedia.
        """
        default_seed = float(getattr(settings, "INITIAL_SEED_CAPITAL_USDT", 64.0) or 64.0)
        self.initial_seed = default_seed

        # 1. Ground-Truth Utama: Database Riil Bitget
        try:
            from core.database import db
            real_harvest = db.get_total_harvested_profit()
            real_harvests = db.get_harvest_history(limit=500)
            if real_harvest > 0 or real_harvests:
                self.total_profit_compounded = round(real_harvest, 6)
                self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
                log.info(
                    f"[CompoundingManager] 💰 Ground-Truth Bitget DB: Modal Pokok: ${self.initial_seed:.2f} | "
                    f"Profit Ter-Compound Riil: +${self.total_profit_compounded:.4f} USDT ({len(real_harvests)}x panen riil)"
                )
                self.save_state()
                return
        except Exception as dbe:
            log.debug(f"[CompoundingManager] DB load notice: {dbe}")

        # 2. Fallback disk jika DB belum siap
        if not self.state_path.exists():
            self.initial_seed = default_seed
            self.current_capital = default_seed
            self.total_profit_compounded = 0.0
            self.events = []
            self.save_state()
            log.info(f"[CompoundingManager] 💰 Baseline Modal Pokok diset: ${self.initial_seed:.2f} USDT")
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                saved_seed = float(data.get("initial_seed_capital", 0.0))
                self.initial_seed = saved_seed if saved_seed > 0 else default_seed
                self.total_profit_compounded = float(data.get("total_profit_compounded", 0.0))
                self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
                self.events = [HarvestEvent.model_validate(e) for e in data.get("events", [])]
            log.info(
                f"[CompoundingManager] 💰 Modal Pokok: ${self.initial_seed:.2f} | "
                f"Profit Compound: +${self.total_profit_compounded:.4f} | Total: ${self.current_capital:.4f} USDT"
            )
        except Exception as e:
            log.error(f"Gagal muat compounding state: {e}")
            self.initial_seed = default_seed
            self.current_capital = default_seed
            self.total_profit_compounded = 0.0

    def save_state(self):
        """Simpan status compounding ke disk."""
        try:
            payload = {
                "initial_seed_capital": self.initial_seed,
                "total_profit_compounded": round(self.total_profit_compounded, 6),
                "current_compounded_capital": round(self.current_capital, 6),
                "harvest_events_count": len(self.events),
                "last_updated": datetime.utcnow().isoformat(),
                "events": [e.model_dump(mode="json") for e in self.events]
            }
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Gagal simpan compounding state: {e}")

    def sync_from_real_balance(self, real_liquid_usdt: float, vault_locked_usdt: float = 0.0):
        """
        Sinkronisasi modal dari saldo REAL Bitget dan deteksi deposit baru.

        FIX #1 (Deposit Detection akurat setelah TP):
        - vault_locked_usdt = dana yang sudah disisihkan ke vault (masih fisik di Bitget,
          tapi sudah "diklaim"). Diperhitungkan agar deposit detection tidak salah.
        - Formula: expected_total = initial_seed + total_profit_compounded + vault_locked_usdt
          (karena vault_locked masih ada di Bitget, maka expected juga harus include-kan)
        - Ketika user deposit $5: real naik $5, expected tidak naik → deteksi $5 dengan benar.
        - Ketika TP dikunci tapi belum ditarik: real = X, expected = X → tidak ada false positive.
        - Ketika TP sudah ditarik dari Bitget: real < expected → tidak ada aksi (benar).
        """
        if real_liquid_usdt <= 0:
            return

        if self.initial_seed <= 0:
            # Inisialisasi pertama kali
            net = max(0.0, real_liquid_usdt - vault_locked_usdt)
            self.initial_seed = round(net, 4)
            self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
            self.save_state()
            return

        # Expected = apa yang seharusnya ada di Bitget (seed + profit + vault yang masih fisik di sana)
        expected_total = self.initial_seed + self.total_profit_compounded + vault_locked_usdt

        if real_liquid_usdt > expected_total + 1.0:
            deposit_amount = round(real_liquid_usdt - expected_total, 4)
            old_seed = self.initial_seed
            self.initial_seed = round(self.initial_seed + deposit_amount, 4)
            self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
            self.save_state()
            log.info(
                f"💰 [Deposit Terdeteksi] Saldo Bitget bertambah +${deposit_amount:.2f} USDT!\n"
                f"   -> Modal Pokok disesuaikan: ${old_seed:.2f} → ${self.initial_seed:.2f} USDT\n"
                f"   -> Total Modal Aktif: ${self.current_capital:.2f} USDT"
            )

    def add_harvest_profit(self, position_id: str, base_asset: str, profit_usdt: float, funding_rate: float):
        """Tambah profit funding fee ke modal compounding."""
        if profit_usdt <= 0:
            return

        self.total_profit_compounded += profit_usdt
        self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)

        event = HarvestEvent(
            timestamp=datetime.utcnow(),
            position_id=position_id,
            base_asset=base_asset,
            profit_usdt=round(profit_usdt, 6),
            funding_rate=funding_rate,
            compounded_balance_after=self.current_capital
        )
        self.events.append(event)
        self.save_state()

        log.info(
            f"🔄 [COMPOUNDING] +${profit_usdt:.4f} USDT dari {base_asset} diputar kembali!\n"
            f"   -> Modal Pokok: ${self.initial_seed:.2f} | Profit Compound: ${self.total_profit_compounded:.4f} | "
            f"Total Aktif: ${self.current_capital:.4f} USDT"
        )

    def deduct_vaulted_profit(self, amount_usdt: float):
        """
        FIX #1 (lanjutan): Saat TP dieksekusi dan dikunci ke vault, kurangi
        total_profit_compounded agar accounting tetap konsisten.
        Dana fisik masih di Bitget — ini hanya adjustment buku catatan internal.
        """
        if amount_usdt <= 0:
            return
        self.total_profit_compounded = max(0.0, self.total_profit_compounded - amount_usdt)
        self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
        self.save_state()
        log.info(
            f"📊 [CompoundingManager] Profit accounting disesuaikan setelah TP:\n"
            f"   -> Profit compounding (working capital): ${self.total_profit_compounded:.4f} USDT\n"
            f"   -> (${amount_usdt:.4f} USDT dikunci di Vault — masih fisik di Bitget)"
        )

    def get_portfolio_bep_status(self, current_trading_equity: float, est_exit_fees: float = 0.0) -> Dict[str, Any]:
        """Evaluasi BEP portofolio nyata vs modal awal baseline."""
        baseline = self.initial_seed if self.initial_seed > 0 else 64.0
        net_equity = round(max(0.0, current_trading_equity - est_exit_fees), 4)
        net_pnl = round(net_equity - baseline, 4)
        profit_pct = round((net_pnl / baseline * 100.0), 2) if baseline > 0 else 0.0
        return {
            "baseline_capital": baseline,
            "current_trading_equity": current_trading_equity,
            "est_exit_fees": est_exit_fees,
            "net_equity_after_exit": net_equity,
            "net_pnl_usdt": net_pnl,
            "profit_percent": profit_pct,
            "is_bep": net_pnl >= 0.0
        }

    def get_position_capital(self, available_liquid_usdt: Optional[float] = None) -> float:
        """Modal dinamis untuk posisi berikutnya berdasarkan saldo REAL Bitget."""
        if settings.USE_ALL_AVAILABLE_BALANCE and available_liquid_usdt is not None:
            if available_liquid_usdt <= 0:
                return 0.0
            usable = available_liquid_usdt * (1.0 - settings.LIQUID_SAFETY_BUFFER_PERCENT)
            per_pos = usable / max(1, settings.MAX_CONCURRENT_POSITIONS)
            if settings.MAX_CAPITAL_PER_POSITION_USDT:
                per_pos = min(per_pos, settings.MAX_CAPITAL_PER_POSITION_USDT)
            return round(math.floor(per_pos * 100.0) / 100.0, 2)

        if self.current_capital > 0:
            return round(self.current_capital, 2)
        if available_liquid_usdt and available_liquid_usdt > 0:
            return round(available_liquid_usdt * (1.0 - settings.LIQUID_SAFETY_BUFFER_PERCENT), 2)
        return 0.0

    def validate_capital_usage(self, requested_amount: float, available_liquid_usdt: float) -> bool:
        """Validasi alokasi modal tidak melebihi saldo cair trading."""
        max_usable = available_liquid_usdt * (1.0 - settings.LIQUID_SAFETY_BUFFER_PERCENT)
        if requested_amount > (max_usable + 0.02):
            log.warning(
                f"[Bitget Earn Protection] Alokasi (${requested_amount:.2f}) melebihi saldo aman "
                f"(${max_usable:.2f}). Dibatalkan."
            )
            return False
        return True

    def get_summary(self) -> CompoundingState:
        """Ringkasan data compounding."""
        events_count = len(self.events)
        try:
            from core.database import db
            db_harvests = db.get_harvest_history(limit=500)
            if db_harvests:
                events_count = max(events_count, len(db_harvests))
        except Exception:
            pass
        return CompoundingState(
            initial_seed_capital=self.initial_seed,
            total_profit_compounded=self.total_profit_compounded,
            current_compounded_capital=self.current_capital,
            harvest_events_count=events_count,
            last_updated=datetime.utcnow()
        )


# =============================================================================
# MONTHLY TAKE PROFIT MANAGER
# Siklus 30-Hari Rolling — State Persisten di PostgreSQL
# Auto-Reinvest Dormant: jika tidak diambil dalam 7 hari → compound kembali
# =============================================================================
class MonthlyTakeProfitManager:
    """
    Manajer Penyisihan Take Profit Bulanan (5% dari total saldo Bitget).

    Semua Cacat Diperbaiki:
    Fix #1: Deposit detection akurat — vault_locked diperhitungkan di expected_total
    Fix #2: Vault persisten di PostgreSQL — tidak hilang saat Render redeploy
    Fix #3: Auto-reinvest dormant — 5% otomatis di-compound jika tidak ditarik 7 hari
    Fix #4: Kalkulasi TP menggunakan net balance (total - vault_locked yang sudah ada)
    Fix #5: Hapus double-print report saat jatuh tempo
    Fix #6: _state_loaded retry jika DB error — tidak stuck di state lama
    Fix #7: Baseline siklus baru = saldo aktif pasca-TP (bukan seed lama yang stale)
    """

    def __init__(self):
        self._state_loaded = False
        self._db_load_failed = False          # Fix #6: track jika load DB gagal
        self._cycle_start: Optional[datetime] = None
        self._cycle_end: Optional[datetime] = None
        self._baseline_at_cycle_start: float = 0.0
        self._total_executed_usdt: float = 0.0
        self._executions_count: int = 0

    def _ensure_tz(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Pastikan datetime selalu timezone-aware (UTC)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _load_state_from_db(self, db) -> bool:
        """
        Fix #6: Muat state dari DB dengan retry flag.
        Jika DB benar-benar tidak tersedia, tandai _db_load_failed=True
        agar bot tidak stuck di state lama — state akan diinisialisasi fresh.
        """
        try:
            row = db.get_monthly_tp_state()
            if row:
                self._cycle_start = self._ensure_tz(datetime.fromisoformat(row["cycle_start_utc"]))
                self._cycle_end = self._ensure_tz(datetime.fromisoformat(row["cycle_end_utc"]))
                self._baseline_at_cycle_start = float(row["baseline_capital_usdt"])
                self._total_executed_usdt = float(row.get("total_executed_tp_usdt", 0.0))
                self._executions_count = int(row.get("executions_count", 0))
                self._db_load_failed = False
                return True
            # DB OK tapi tidak ada record — fresh start
            self._db_load_failed = False
            return False
        except Exception as e:
            log.debug(f"[MonthlyTP] DB load notice: {e}")
            self._db_load_failed = True
            return False

    def _init_new_cycle(self, db, baseline_capital: float):
        """Inisialisasi siklus 30-hari baru dan simpan ke DB."""
        cycle_days = int(getattr(settings, "MONTHLY_TP_CYCLE_DAYS", 30))
        now_utc = datetime.now(timezone.utc)
        self._cycle_start = now_utc
        self._cycle_end = now_utc + timedelta(days=cycle_days)
        self._baseline_at_cycle_start = round(baseline_capital, 4)
        self._total_executed_usdt = 0.0
        self._executions_count = 0
        try:
            db.upsert_monthly_tp_state(
                cycle_start_utc=self._cycle_start.isoformat(),
                cycle_end_utc=self._cycle_end.isoformat(),
                baseline_capital_usdt=self._baseline_at_cycle_start,
                total_executed_tp_usdt=0.0,
                executions_count=0
            )
        except Exception as e:
            log.debug(f"[MonthlyTP] _init_new_cycle DB notice: {e}")
        log.info(
            f"📅 [MonthlyTP] Siklus baru dimulai: "
            f"{self._cycle_start.strftime('%Y-%m-%d %H:%M UTC')} → "
            f"{self._cycle_end.strftime('%Y-%m-%d %H:%M UTC')} | "
            f"Baseline Modal: ${self._baseline_at_cycle_start:.2f} USDT"
        )

    def calculate_safe_withdrawal(
        self,
        total_balance_usdt: float,
        baseline_capital_usdt: float,
        existing_vault_locked: float = 0.0
    ) -> Dict[str, Any]:
        """
        Fix #4: Hitung TP dari NET balance (total Bitget - vault yang sudah dikunci sebelumnya).
        Ini mencegah double-dip: vault yang sudah ada tidak dihitung ulang sebagai basis TP baru.

        Capital Floor Guard:
            net_balance = total_balance - existing_vault_locked
            tp_amount = net_balance * tp_pct
            remaining_after_tp = net_balance - tp_amount
            required: remaining >= baseline * (1 + min_surplus_pct)
        """
        tp_pct = float(getattr(settings, "MONTHLY_TP_PERCENT", 0.05))
        min_surplus_pct = float(getattr(settings, "MONTHLY_TP_MIN_SURPLUS_PERCENT", 0.01))

        # Fix #4: gunakan net balance (bukan gross)
        net_balance = max(0.0, total_balance_usdt - existing_vault_locked)
        tp_amount = round(net_balance * tp_pct, 4)
        remaining_after_tp = round(net_balance - tp_amount, 4)
        required_floor = round(baseline_capital_usdt * (1.0 + min_surplus_pct), 4)
        surplus_after = round(remaining_after_tp - baseline_capital_usdt, 4)

        if net_balance <= 0 or baseline_capital_usdt <= 0:
            return {
                "safe_amount_usdt": 0.0, "can_execute": False,
                "reason": "Saldo atau modal awal tidak valid",
                "surplus_after_usdt": 0.0, "tp_percent": tp_pct,
                "tp_amount_gross": 0.0, "remaining_after_tp": 0.0,
                "required_floor": required_floor, "net_balance_used": net_balance
            }

        if remaining_after_tp >= required_floor:
            return {
                "safe_amount_usdt": tp_amount, "can_execute": True,
                "reason": f"AMAN — Sisa ${remaining_after_tp:.2f} ≥ Floor ${required_floor:.2f} (Surplus +${surplus_after:.2f})",
                "surplus_after_usdt": surplus_after, "tp_percent": tp_pct,
                "tp_amount_gross": tp_amount, "remaining_after_tp": remaining_after_tp,
                "required_floor": required_floor, "net_balance_used": net_balance
            }
        else:
            shortfall = round(required_floor - remaining_after_tp, 4)
            return {
                "safe_amount_usdt": 0.0, "can_execute": False,
                "reason": (
                    f"BELUM AMAN — Sisa ${remaining_after_tp:.2f} < Floor ${required_floor:.2f} "
                    f"(Kurang ${shortfall:.2f} USDT lagi)"
                ),
                "surplus_after_usdt": surplus_after, "tp_percent": tp_pct,
                "tp_amount_gross": tp_amount, "remaining_after_tp": remaining_after_tp,
                "required_floor": required_floor, "net_balance_used": net_balance
            }

    def print_safe_withdrawal_compact(
        self,
        total_balance_usdt: float,
        baseline_capital_usdt: float,
        existing_vault_locked: float = 0.0
    ) -> Dict[str, Any]:
        """Fix #5: Hanya tampilkan ringkasan 1 baris per scan (bukan full report)."""
        result = self.calculate_safe_withdrawal(total_balance_usdt, baseline_capital_usdt, existing_vault_locked)

        now_utc = datetime.now(timezone.utc)
        cycle_days = int(getattr(settings, "MONTHLY_TP_CYCLE_DAYS", 30))
        day_in_cycle = 1
        if self._cycle_start:
            elapsed = now_utc - self._ensure_tz(self._cycle_start)
            day_in_cycle = max(1, int(elapsed.total_seconds() / 86400) + 1)

        safe_amt = result["safe_amount_usdt"]
        profit_this_cycle = round(total_balance_usdt - baseline_capital_usdt, 4)
        profit_pct = round((profit_this_cycle / baseline_capital_usdt * 100.0), 2) if baseline_capital_usdt > 0 else 0.0
        status = "✅ BOLEH AMBIL" if result["can_execute"] else "⏳ BELUM CUKUP PROFIT"

        reinvest_days = int(getattr(settings, "MONTHLY_TP_AUTO_REINVEST_DAYS", 7))
        log.info(
            f"💰 [MonthlyTP] Hari ke-{day_in_cycle}/{cycle_days} | "
            f"Saldo: ${total_balance_usdt:.2f} | Vault: ${existing_vault_locked:.2f} | "
            f"Boleh ditarik: ${safe_amt:.2f} USDT | Profit: {profit_pct:+.2f}% | "
            f"{status} (auto-reinvest jika tidak diambil dalam {reinvest_days}h)"
        )
        return result

    def print_full_tp_report(
        self,
        total_balance_usdt: float,
        baseline_capital_usdt: float,
        result: Dict[str, Any]
    ):
        """Fix #5: Panel laporan lengkap HANYA saat jatuh tempo."""
        now_utc = datetime.now(timezone.utc)
        cycle_days = int(getattr(settings, "MONTHLY_TP_CYCLE_DAYS", 30))
        day_in_cycle = 1
        days_remaining = 0
        if self._cycle_start:
            elapsed = now_utc - self._ensure_tz(self._cycle_start)
            day_in_cycle = max(1, int(elapsed.total_seconds() / 86400) + 1)
            days_remaining = max(0, cycle_days - day_in_cycle + 1)

        profit_this_cycle = round(total_balance_usdt - baseline_capital_usdt, 4)
        profit_pct = round((profit_this_cycle / baseline_capital_usdt * 100.0), 2) if baseline_capital_usdt > 0 else 0.0
        can = result["can_execute"]
        safe_amt = result["safe_amount_usdt"]
        surplus = result["surplus_after_usdt"]
        tp_pct = result["tp_percent"]
        net_bal = result.get("net_balance_used", total_balance_usdt)
        reinvest_days = int(getattr(settings, "MONTHLY_TP_AUTO_REINVEST_DAYS", 7))

        sep = "═" * 60
        status_icon = "🟢" if can else "🔴"
        status_text = "BOLEH DITARIK" if can else "BELUM BOLEH DITARIK"

        log.info(f"\n╔{sep}╗")
        log.info(f"║  {'💰 MONTHLY SAFE WITHDRAWAL REPORT (30-HARI)':<56}  ║")
        log.info(f"╠{sep}╣")
        log.info(f"║  📅 Hari ke-{day_in_cycle}/{cycle_days} ({days_remaining} hari tersisa dari siklus ini){'':>19}  ║")
        log.info(f"║  💼 Modal Pokok Awal    : ${baseline_capital_usdt:<12.2f}{'':>24}  ║")
        log.info(f"║  📈 Saldo Total Bitget  : ${total_balance_usdt:<12.2f}{'':>24}  ║")
        log.info(f"║  🔒 Vault Sudah Terkunci: ${total_balance_usdt - net_bal:<12.4f}{'':>24}  ║")
        log.info(f"║  💵 Net Balance (Basis TP): ${net_bal:<11.4f}{'':>24}  ║")
        log.info(f"║  📊 Profit Siklus Ini   : +${profit_this_cycle:<9.4f} ({profit_pct:+.2f}%){'':>16}  ║")
        log.info(f"╠{sep}╣")
        log.info(f"║  🎯 {tp_pct*100:.0f}% dari Net Balance : ${result['tp_amount_gross']:<12.4f}{'':>24}  ║")
        log.info(f"║  🛡️  Sisa Setelah Ditarik: ${result['remaining_after_tp']:<12.4f}{'':>24}  ║")
        if can:
            log.info(f"║  ✅ Surplus Modal Awal  : +${surplus:<10.4f} USDT (AMAN){'':>17}  ║")
        else:
            log.info(f"║  ❌ Defisit Modal Awal  : ${surplus:<11.4f} USDT (TIDAK AMAN){'':>12}  ║")
        log.info(f"╠{sep}╣")
        log.info(f"║  {status_icon} STATUS             : {status_text:<36}  ║")
        log.info(f"║  💵 JUMLAH AMAN DITARIK : ${safe_amt:<12.4f} USDT{'':>24}  ║")
        if can:
            log.info(f"║  ⏱️  Auto-reinvest dalam: {reinvest_days} hari jika tidak ditarik{'':>20}  ║")
        else:
            log.info(f"║  ♻️  AKSI               : Compounding dilanjutkan 30 hari lagi{'':>12}  ║")
        log.info(f"╚{sep}╝")

    def check_and_auto_reinvest(
        self,
        vault,
        compounding_manager_ref,
        db
    ) -> float:
        """
        Fix #3: Periksa record MONTHLY_TP yang dormant (belum ditarik > 7 hari)
        dan kembalikan ke modal compounding secara otomatis.

        Returns: total USDT yang berhasil di-reinvest (0.0 jika tidak ada).
        """
        total_reinvested = 0.0
        try:
            dormant = vault.get_dormant_tp_records()
            for record in dormant:
                record_id = record.get("id")
                if not record_id:
                    continue
                amount = vault.release_record_for_reinvest(record_id)
                if amount > 0:
                    # Tambahkan kembali ke modal compounding
                    compounding_manager_ref.add_harvest_profit(
                        position_id=f"auto_reinvest_{record_id}",
                        base_asset="AUTO_REINVEST_TP",
                        profit_usdt=amount,
                        funding_rate=0.0
                    )
                    total_reinvested += amount
                    reinvest_days = int(getattr(settings, "MONTHLY_TP_AUTO_REINVEST_DAYS", 7))
                    log.info(
                        f"♻️  [Auto-Reinvest] ${amount:.4f} USDT dari vault dikembalikan ke modal compounding!\n"
                        f"   -> Tidak ditarik selama {reinvest_days} hari → otomatis di-compound untuk growth lebih besar.\n"
                        f"   -> Modal Compounding Aktif Baru: ${compounding_manager_ref.current_capital:.4f} USDT"
                    )
                    # Catat ke AGI memory
                    try:
                        db.record_agi_experience(
                            event_type="AUTO_REINVEST_TP",
                            harvest_usdt=amount,
                            lesson_learned=f"${amount:.4f} USDT auto-reinvest setelah {reinvest_days} hari tidak ditarik.",
                            context_snapshot={"record_id": record_id, "amount": amount}
                        )
                    except Exception:
                        pass
        except Exception as e:
            log.debug(f"[MonthlyTP] check_and_auto_reinvest notice: {e}")
        return total_reinvested

    def evaluate_and_execute(
        self,
        total_balance_usdt: float,
        baseline_capital_usdt: float,
        db,
        vault  # YieldVault instance
    ) -> Dict[str, Any]:
        """
        Evaluasi siklus 30-hari dan eksekusi TP jika jatuh tempo + syarat terpenuhi.
        Dipanggil di main loop setiap siklus scan.

        Fix #5: Tidak ada double-print. Ringkasan 1 baris per scan, full report HANYA saat jatuh tempo.
        Fix #6: Retry load dari DB jika sebelumnya gagal (_db_load_failed reset per call).
        """
        if not getattr(settings, "MONTHLY_TP_ENABLED", True):
            return {"executed": False, "amount_usdt": 0.0, "reason": "Disabled", "is_new_cycle": False}

        now_utc = datetime.now(timezone.utc)
        existing_vault_locked = getattr(vault, "total_locked_usdt", 0.0)

        # Fix #6: Load state dari DB. Jika sebelumnya gagal, coba lagi setiap kali.
        if not self._state_loaded or self._db_load_failed:
            found = self._load_state_from_db(db)
            self._state_loaded = True
            if not found and not self._db_load_failed:
                # Siklus baru: gunakan baseline = saldo aktif saat ini (Fix #7)
                self._init_new_cycle(db, baseline_capital_usdt)
                self.print_safe_withdrawal_compact(total_balance_usdt, baseline_capital_usdt, existing_vault_locked)
                return {"executed": False, "amount_usdt": 0.0, "reason": "Siklus baru dimulai", "is_new_cycle": True}
            elif self._db_load_failed:
                # DB tidak tersedia saat ini, skip evaluasi (tidak crash)
                log.debug("[MonthlyTP] DB tidak tersedia, skip evaluasi siklus ini.")
                return {"executed": False, "amount_usdt": 0.0, "reason": "DB unavailable", "is_new_cycle": False}

        cycle_end = self._ensure_tz(self._cycle_end)

        # Fix #5: Tampilkan HANYA ringkasan 1 baris per scan (bukan full report)
        if cycle_end and now_utc < cycle_end:
            self.print_safe_withdrawal_compact(total_balance_usdt, baseline_capital_usdt, existing_vault_locked)
            return {"executed": False, "amount_usdt": 0.0, "reason": "Siklus belum jatuh tempo", "is_new_cycle": False}

        # JATUH TEMPO — Hitung dan tampilkan full report SEKALI (Fix #5: hapus double call)
        result = self.calculate_safe_withdrawal(total_balance_usdt, baseline_capital_usdt, existing_vault_locked)
        self.print_full_tp_report(total_balance_usdt, baseline_capital_usdt, result)

        if result["can_execute"]:
            tp_amount = result["safe_amount_usdt"]
            surplus = result["surplus_after_usdt"]

            # Kunci ke YieldVault (dengan auto-reinvest deadline)
            vault.deposit_harvest(
                position_id=f"monthly_tp_{now_utc.strftime('%Y%m%d_%H%M')}",
                base_asset="MONTHLY_TP",
                amount_usdt=tp_amount,
                funding_rate=0.0
            )

            # Fix #1: Kurangi profit compounding agar deposit detection tidak salah
            # Dana fisik masih di Bitget, tapi accounting internal disesuaikan
            # (compounding_manager diakses via referensi — lihat catatan di main.py)
            # NOTE: compounding_manager.deduct_vaulted_profit() dipanggil di main.py

            # Update state & simpan ke DB
            self._total_executed_usdt += tp_amount
            self._executions_count += 1
            try:
                db.upsert_monthly_tp_state(
                    cycle_start_utc=self._cycle_start.isoformat() if self._cycle_start else now_utc.isoformat(),
                    cycle_end_utc=self._cycle_end.isoformat() if self._cycle_end else (now_utc + timedelta(days=30)).isoformat(),
                    baseline_capital_usdt=self._baseline_at_cycle_start,
                    total_executed_tp_usdt=self._total_executed_usdt,
                    last_execution_utc=now_utc.isoformat(),
                    executions_count=self._executions_count
                )
                db.record_monthly_tp_execution(
                    amount_usdt=tp_amount,
                    total_balance_before=total_balance_usdt,
                    baseline_capital_usdt=baseline_capital_usdt,
                    remaining_after_tp=result["remaining_after_tp"],
                    surplus_usdt=surplus,
                    skipped=False
                )
            except Exception as e:
                log.debug(f"[MonthlyTP] DB save notice: {e}")

            log.info(
                f"✅ [MonthlyTP] TAKE PROFIT BERHASIL DIKUNCI!\n"
                f"   -> ${tp_amount:.4f} USDT masuk ke YieldVault.\n"
                f"   -> Bisa Anda tarik kapan saja dari Bitget.\n"
                f"   -> Jika TIDAK ditarik dalam {getattr(settings, 'MONTHLY_TP_AUTO_REINVEST_DAYS', 7)} hari → "
                f"otomatis di-compound kembali.\n"
                f"   -> Modal aktif tersisa: ${result['remaining_after_tp']:.2f} USDT "
                f"(Surplus +${surplus:.4f} di atas modal awal ${baseline_capital_usdt:.2f})"
            )

            # Fix #7: Siklus baru — baseline = remaining_after_tp (modal kerja aktual pasca-TP)
            # Ini memastikan Capital Floor Guard bulan depan dihitung dari kondisi nyata
            self._init_new_cycle(db, result["remaining_after_tp"])
            return {"executed": True, "amount_usdt": tp_amount, "reason": result["reason"], "is_new_cycle": True}

        else:
            # Syarat tidak terpenuhi — perpanjang siklus 30 hari lagi
            skip_reason = result["reason"]
            cycle_days = int(getattr(settings, "MONTHLY_TP_CYCLE_DAYS", 30))
            self._cycle_end = now_utc + timedelta(days=cycle_days)
            try:
                db.upsert_monthly_tp_state(
                    cycle_start_utc=self._cycle_start.isoformat() if self._cycle_start else now_utc.isoformat(),
                    cycle_end_utc=self._cycle_end.isoformat(),
                    baseline_capital_usdt=self._baseline_at_cycle_start,
                    total_executed_tp_usdt=self._total_executed_usdt,
                    executions_count=self._executions_count,
                    last_skipped_reason=skip_reason
                )
                db.record_monthly_tp_execution(
                    amount_usdt=0.0,
                    total_balance_before=total_balance_usdt,
                    baseline_capital_usdt=baseline_capital_usdt,
                    remaining_after_tp=total_balance_usdt,
                    surplus_usdt=result["surplus_after_usdt"],
                    skipped=True,
                    skip_reason=skip_reason
                )
            except Exception as e:
                log.debug(f"[MonthlyTP] DB skip save notice: {e}")

            log.warning(
                f"⚠️  [MonthlyTP] TAKE PROFIT DILEWATI — {skip_reason}\n"
                f"   -> Compounding dilanjutkan 30 hari lagi hingga surplus terpenuhi."
            )
            return {"executed": False, "amount_usdt": 0.0, "reason": skip_reason, "is_new_cycle": False}


compounding_manager = CompoundingManager()
monthly_tp_manager = MonthlyTakeProfitManager()
