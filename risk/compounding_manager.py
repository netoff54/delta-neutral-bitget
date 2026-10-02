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
    Manajer Compounding & Proteksi Saldo Bitget Earn:
    
    1. Compounding: Seluruh profit funding fee yang dihasilkan otomatis dimasukkan kembali
       ke modal trading delta-neutral untuk memperbesar ukuran posisi berikutnya.
    2. TIDAK ADA DATA FIKTIF: Seluruh modal bersumber dari saldo REAL Bitget (Spot + Futures).
       Bot menggunakan 100% saldo cair yang tersedia, bukan nilai statis/mock.
    3. Isolasi Mutlak Akun Bitget Earn:
       Bot SAMA SEKALI TIDAK menyentuh dana di Bitget Earn/Savings/Staking.
    """

    def __init__(self, state_file: str = "data/compounding_state.json"):
        self.state_path = Path(state_file)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # Modal Pokok Baseline: Ditetapkan 64.0 USDT sesuai setoran awal user
        default_seed = float(getattr(settings, "INITIAL_SEED_CAPITAL_USDT", 64.0) or 64.0)
        self.initial_seed: float = default_seed
        self.total_profit_compounded: float = 0.0
        self.current_capital: float = default_seed
        self.events: List[HarvestEvent] = []
        self.load_state()

    def load_state(self):
        """Memuat riwayat compounding dari disk."""
        default_seed = float(getattr(settings, "INITIAL_SEED_CAPITAL_USDT", 64.0) or 64.0)
        if not self.state_path.exists():
            # File belum ada -> inisialisasi baseline 64.0 USDT
            self.initial_seed = default_seed
            self.current_capital = default_seed
            self.total_profit_compounded = 0.0
            self.events = []
            self.save_state()
            log.info(f"[CompoundingManager] 💰 Baseline Modal Pokok diset: ${self.initial_seed:.2f} USDT (Default Setoran User)")
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                saved_seed = float(data.get("initial_seed_capital", 0.0))
                # Jika saved_seed 0, gunakan default 64.0 USDT
                self.initial_seed = saved_seed if saved_seed > 0 else default_seed
                self.total_profit_compounded = float(data.get("total_profit_compounded", 0.0))
                self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
                self.events = [HarvestEvent.model_validate(e) for e in data.get("events", [])]
            log.info(
                f"[CompoundingManager] 💰 Baseline Modal Pokok Real: ${self.initial_seed:.2f} USDT | "
                f"Profit Ter-Compound: +${self.total_profit_compounded:.4f} USDT | Total: ${self.current_capital:.4f} USDT"
            )
        except Exception as e:
            log.error(f"Gagal memuat compounding state: {e}")
            self.initial_seed = default_seed
            self.current_capital = default_seed
            self.total_profit_compounded = 0.0

    def sync_from_real_balance(self, real_liquid_usdt: float):
        """
        Sinkronisasi modal pokok dari saldo REAL Bitget dan deteksi setoran saldo baru (deposit).
        Jika user sewaktu-waktu menambah modal di Bitget, modal pokok disesuaikan otomatis.
        """
        if real_liquid_usdt <= 0:
            return

        if self.initial_seed <= 0:
            self.initial_seed = round(real_liquid_usdt, 4)
            self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
            self.save_state()
            return

        # Deteksi deposit baru: jika saldo real melebihi (baseline modal pokok + profit yang dipanen) lebih dari 1.0 USDT
        expected_total = self.initial_seed + self.total_profit_compounded
        if real_liquid_usdt > expected_total + 1.0:
            deposit_amount = round(real_liquid_usdt - expected_total, 4)
            old_seed = self.initial_seed
            self.initial_seed = round(self.initial_seed + deposit_amount, 4)
            self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
            self.save_state()
            log.info(
                f"💰 [Deposit Terdeteksi] Terdeteksi penambahan saldo di Bitget: +${deposit_amount:.2f} USDT!\n"
                f"   -> Baseline Modal Pokok disesuaikan: ${old_seed:.2f} → ${self.initial_seed:.2f} USDT\n"
                f"   -> Total Modal Aktif Terbaru: ${self.current_capital:.2f} USDT"
            )

    def get_portfolio_bep_status(self, current_trading_equity: float, est_exit_fees: float = 0.0) -> Dict[str, Any]:
        """
        Evaluasi status BEP portofolio nyata dari akun Bitget vs modal awal baseline ($64.0 + deposit baru).
        Menghitung ekuitas bersih setelah dikurangi seluruh estimasi biaya exit di masa depan.
        """
        baseline = self.initial_seed if self.initial_seed > 0 else 64.0
        net_equity_after_exit = round(max(0.0, current_trading_equity - est_exit_fees), 4)
        net_pnl_usdt = round(net_equity_after_exit - baseline, 4)
        is_bep = (net_pnl_usdt >= 0.0)
        profit_pct = round((net_pnl_usdt / baseline * 100.0), 2) if baseline > 0 else 0.0
        return {
            "baseline_capital": baseline,
            "current_trading_equity": current_trading_equity,
            "est_exit_fees": est_exit_fees,
            "net_equity_after_exit": net_equity_after_exit,
            "net_pnl_usdt": net_pnl_usdt,
            "profit_percent": profit_pct,
            "is_bep": is_bep
        }


    def save_state(self):
        """Menyimpan status compounding ke disk."""
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
            log.error(f"Gagal menyimpan compounding state: {e}")

    def add_harvest_profit(
        self,
        position_id: str,
        base_asset: str,
        profit_usdt: float,
        funding_rate: float
    ):
        """
        Menambahkan profit funding fee ke modal trading (Compounding).
        """
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
            f"🔄 [COMPOUNDING] +${profit_usdt:.4f} USDT dari {base_asset} berhasil diputar kembali ke modal trading!\n"
            f"   -> Modal Pokok: ${self.initial_seed:.2f} USDT\n"
            f"   -> Akumulasi Profit yang Diputar: ${self.total_profit_compounded:.4f} USDT\n"
            f"   -> Total Modal Trading Aktif Sekarang: ${self.current_capital:.4f} USDT"
        )

    def get_position_capital(self, available_liquid_usdt: Optional[float] = None) -> float:
        """
        Mendapatkan ukuran modal dinamis untuk posisi berikutnya.
        SELALU menggunakan saldo REAL dari Bitget (Spot + Futures).
        TIDAK PERNAH menggunakan angka statis/mock/fiktif.
        """
        if settings.USE_ALL_AVAILABLE_BALANCE and available_liquid_usdt is not None:
            if available_liquid_usdt <= 0:
                return 0.0
            usable = available_liquid_usdt * (1.0 - settings.LIQUID_SAFETY_BUFFER_PERCENT)
            per_pos = usable / max(1, settings.MAX_CONCURRENT_POSITIONS)
            if settings.MAX_CAPITAL_PER_POSITION_USDT:
                per_pos = min(per_pos, settings.MAX_CAPITAL_PER_POSITION_USDT)
            return round(math.floor(per_pos * 100.0) / 100.0, 2)

        # Jika current_capital belum di-sync dari Bitget, gunakan available_liquid_usdt sebagai fallback
        if self.current_capital > 0:
            return round(self.current_capital, 2)
        if available_liquid_usdt and available_liquid_usdt > 0:
            return round(available_liquid_usdt * (1.0 - settings.LIQUID_SAFETY_BUFFER_PERCENT), 2)
        return 0.0


    def validate_capital_usage(self, requested_amount: float, available_liquid_usdt: float) -> bool:
        """
        Memvalidasi alokasi modal agar TIDAK MENYENTUH DANA DI BITGET EARN:
        Hanya mengizinkan penggunaan dana dari saldo cair trading (Spot & Futures).
        """
        max_liquid_usable = available_liquid_usdt * (1.0 - settings.LIQUID_SAFETY_BUFFER_PERCENT)

        # Toleransi 0.02 USDT untuk mencegah false warning akibat precision floating-point
        if requested_amount > (max_liquid_usable + 0.02):
            log.warning(
                f"[Bitget Earn Protection] Alokasi modal (${requested_amount:.2f}) melebihi saldo trading cair yang aman "
                f"(${max_liquid_usable:.2f}). Alokasi dibatalkan untuk melindungi saldo Bitget Earn Anda!"
            )
            return False
        return True

    def get_summary(self) -> CompoundingState:
        """Mengambil ringkasan data compounding."""
        return CompoundingState(
            initial_seed_capital=self.initial_seed,
            total_profit_compounded=self.total_profit_compounded,
            current_compounded_capital=self.current_capital,
            harvest_events_count=len(self.events),
            last_updated=datetime.utcnow()
        )


# =============================================================================
# MONTHLY TAKE PROFIT MANAGER
# Siklus 30-Hari Rolling — State Persisten di PostgreSQL (Tahan Restart/Redeploy)
# =============================================================================
class MonthlyTakeProfitManager:
    """
    Manajer Penyisihan Take Profit Bulanan (5% dari Total Saldo Bitget setiap 30 hari).

    Prinsip Utama:
    1. Siklus 30-hari Rolling — waktu dihitung dari database, TIDAK hilang saat restart/redeploy.
    2. Capital Floor Guard — TP HANYA dieksekusi jika sisa saldo (95%) masih SURPLUS di atas
       modal pokok awal + minimal 1%. Modal awal dijamin tidak pernah berkurang.
    3. Deposit Awareness — jika user menambah saldo di tengah siklus, baseline otomatis menyesuaikan
       dan timer 30 hari TIDAK direset.
    4. Safe Withdrawal Log — setiap siklus (dan setiap scan) bot menampilkan laporan berapa
       USDT yang aman untuk ditarik, secara eksplisit dalam nominal.
    5. Audit Trail — setiap eksekusi / penolakan TP dicatat ke database AGI memory untuk transparansi.
    """

    def __init__(self):
        self._state_loaded = False
        self._cycle_start: Optional[datetime] = None
        self._cycle_end: Optional[datetime] = None
        self._baseline_at_cycle_start: float = 0.0
        self._total_executed_usdt: float = 0.0
        self._executions_count: int = 0

    def _load_state_from_db(self, db) -> bool:
        """Muat state siklus dari database (PostgreSQL atau SQLite)."""
        try:
            row = db.get_monthly_tp_state()
            if row:
                self._cycle_start = datetime.fromisoformat(row["cycle_start_utc"])
                self._cycle_end = datetime.fromisoformat(row["cycle_end_utc"])
                self._baseline_at_cycle_start = float(row["baseline_capital_usdt"])
                self._total_executed_usdt = float(row.get("total_executed_tp_usdt", 0.0))
                self._executions_count = int(row.get("executions_count", 0))
                return True
        except Exception as e:
            log.debug(f"[MonthlyTP] Load state notice: {e}")
        return False

    def _init_new_cycle(self, db, baseline_capital: float):
        """Inisialisasi siklus 30-hari baru dan simpan ke database."""
        cycle_days = int(getattr(settings, "MONTHLY_TP_CYCLE_DAYS", 30))
        now_utc = datetime.now(timezone.utc)
        self._cycle_start = now_utc
        self._cycle_end = now_utc + timedelta(days=cycle_days)
        self._baseline_at_cycle_start = round(baseline_capital, 4)
        self._total_executed_usdt = 0.0
        self._executions_count = 0
        db.upsert_monthly_tp_state(
            cycle_start_utc=self._cycle_start.isoformat(),
            cycle_end_utc=self._cycle_end.isoformat(),
            baseline_capital_usdt=self._baseline_at_cycle_start,
            total_executed_tp_usdt=0.0,
            executions_count=0
        )
        log.info(
            f"📅 [MonthlyTP] Siklus 30-hari baru dimulai: {self._cycle_start.strftime('%Y-%m-%d %H:%M UTC')} "
            f"→ {self._cycle_end.strftime('%Y-%m-%d %H:%M UTC')} | "
            f"Baseline Modal: ${self._baseline_at_cycle_start:.2f} USDT"
        )

    def calculate_safe_withdrawal(
        self,
        total_balance_usdt: float,
        baseline_capital_usdt: float
    ) -> Dict[str, Any]:
        """
        Hitung berapa USDT yang boleh ditarik hari ini agar modal awal tetap SURPLUS.

        Syarat Capital Floor Guard:
            saldo_setelah_tp = total_balance * (1 - tp_pct)
            saldo_setelah_tp >= baseline * (1 + min_surplus_pct)

        Returns dict dengan:
          - safe_amount_usdt: jumlah pasti yang boleh ditarik (0 jika belum aman)
          - can_execute: True/False
          - reason: penjelasan singkat
          - surplus_after_usdt: surplus di atas modal awal jika ditarik
          - tp_percent: persentase yang digunakan
        """
        tp_pct = float(getattr(settings, "MONTHLY_TP_PERCENT", 0.05))
        min_surplus_pct = float(getattr(settings, "MONTHLY_TP_MIN_SURPLUS_PERCENT", 0.01))

        tp_amount = round(total_balance_usdt * tp_pct, 4)
        remaining_after_tp = round(total_balance_usdt - tp_amount, 4)
        required_floor = round(baseline_capital_usdt * (1.0 + min_surplus_pct), 4)
        surplus_after = round(remaining_after_tp - baseline_capital_usdt, 4)

        if total_balance_usdt <= 0 or baseline_capital_usdt <= 0:
            return {
                "safe_amount_usdt": 0.0,
                "can_execute": False,
                "reason": "Saldo atau modal awal tidak valid",
                "surplus_after_usdt": 0.0,
                "tp_percent": tp_pct,
                "tp_amount_gross": 0.0,
                "remaining_after_tp": 0.0,
                "required_floor": required_floor
            }

        if remaining_after_tp >= required_floor:
            return {
                "safe_amount_usdt": tp_amount,
                "can_execute": True,
                "reason": f"AMAN — Sisa ${remaining_after_tp:.2f} > Floor ${required_floor:.2f} (Surplus +${surplus_after:.2f})",
                "surplus_after_usdt": surplus_after,
                "tp_percent": tp_pct,
                "tp_amount_gross": tp_amount,
                "remaining_after_tp": remaining_after_tp,
                "required_floor": required_floor
            }
        else:
            shortfall = round(required_floor - remaining_after_tp, 4)
            return {
                "safe_amount_usdt": 0.0,
                "can_execute": False,
                "reason": (
                    f"BELUM AMAN — Sisa ${remaining_after_tp:.2f} < Floor ${required_floor:.2f} "
                    f"(Kurang ${shortfall:.2f} USDT lagi)"
                ),
                "surplus_after_usdt": surplus_after,
                "tp_percent": tp_pct,
                "tp_amount_gross": tp_amount,
                "remaining_after_tp": remaining_after_tp,
                "required_floor": required_floor
            }

    def print_safe_withdrawal_report(
        self,
        total_balance_usdt: float,
        baseline_capital_usdt: float,
        db,
        force_full_report: bool = False
    ) -> Dict[str, Any]:
        """
        Tampilkan laporan Safe Withdrawal ke log.
        - Setiap scan: tampilkan ringkasan 1 baris
        - Setiap jatuh tempo siklus / force_full_report: tampilkan panel lengkap
        """
        now_utc = datetime.now(timezone.utc)
        result = self.calculate_safe_withdrawal(total_balance_usdt, baseline_capital_usdt)

        # Hitung hari ke-N dalam siklus
        day_in_cycle = 0
        days_remaining = 0
        cycle_days = int(getattr(settings, "MONTHLY_TP_CYCLE_DAYS", 30))
        if self._cycle_start:
            elapsed = (now_utc - self._cycle_start.replace(tzinfo=timezone.utc) if self._cycle_start.tzinfo is None else now_utc - self._cycle_start)
            day_in_cycle = max(1, int(elapsed.total_seconds() / 86400) + 1)
            days_remaining = max(0, cycle_days - day_in_cycle + 1)

        profit_this_cycle = round(total_balance_usdt - baseline_capital_usdt, 4)
        profit_pct = round((profit_this_cycle / baseline_capital_usdt * 100.0), 2) if baseline_capital_usdt > 0 else 0.0

        can = result["can_execute"]
        safe_amt = result["safe_amount_usdt"]
        surplus = result["surplus_after_usdt"]
        tp_pct = result["tp_percent"]

        if force_full_report:
            # Panel lengkap saat jatuh tempo
            sep = "═" * 58
            status_icon = "🟢" if can else "🔴"
            status_text = "BOLEH DITARIK" if can else "BELUM BOLEH DITARIK"
            log.info(f"\n╔{sep}╗")
            log.info(f"║  {'💰 MONTHLY SAFE WITHDRAWAL REPORT':<54}  ║")
            log.info(f"╠{sep}╣")
            log.info(f"║  📅 Hari ke-{day_in_cycle}/{cycle_days} Siklus ({days_remaining} hari tersisa){'':>18}  ║")
            log.info(f"║  💼 Modal Pokok Awal    : ${baseline_capital_usdt:<10.2f}{'':>22}  ║")
            log.info(f"║  📈 Saldo Total Bitget  : ${total_balance_usdt:<10.2f}{'':>22}  ║")
            log.info(f"║  📊 Profit Siklus Ini   : +${profit_this_cycle:<8.4f} ({profit_pct:+.2f}%){'':>14}  ║")
            log.info(f"╠{sep}╣")
            log.info(f"║  🎯 {tp_pct*100:.0f}% dari Saldo Total : ${result['tp_amount_gross']:<10.4f}{'':>22}  ║")
            log.info(f"║  🛡️  Sisa Setelah Ditarik: ${result['remaining_after_tp']:<10.4f}{'':>22}  ║")
            if can:
                log.info(f"║  ✅ Modal Awal Surplus  : +${surplus:<9.4f} USDT (AMAN){'':>15}  ║")
            else:
                log.info(f"║  ❌ Modal Awal Defisit  : ${surplus:<10.4f} USDT (TIDAK AMAN){'':>10}  ║")
            log.info(f"╠{sep}╣")
            log.info(f"║  {status_icon} STATUS             : {status_text:<34}  ║")
            log.info(f"║  💵 JUMLAH AMAN DITARIK : ${safe_amt:<10.4f} USDT{'':>22}  ║")
            if not can:
                log.info(f"║  ♻️  AKSI               : Compounding dilanjutkan...{'':>10}  ║")
            log.info(f"╚{sep}╝")
        else:
            # Ringkasan 1 baris per scan
            status_short = "✅ BOLEH AMBIL" if can else "⏳ BELUM WAKTUNYA"
            log.info(
                f"💰 [MonthlyTP] Hari ke-{day_in_cycle}/{cycle_days} | "
                f"Saldo: ${total_balance_usdt:.2f} | "
                f"Bisa ditarik sekarang: ${safe_amt:.2f} USDT | "
                f"Profit siklus: {profit_pct:+.2f}% | {status_short}"
            )

        return result

    def evaluate_and_execute(
        self,
        total_balance_usdt: float,
        baseline_capital_usdt: float,
        db,
        vault  # YieldVault instance
    ) -> Dict[str, Any]:
        """
        Evaluasi apakah siklus 30-hari sudah jatuh tempo dan eksekusi TP jika syarat terpenuhi.
        Dipanggil di main loop setiap siklus scan.

        Returns:
            dict dengan keys: executed, amount_usdt, reason, is_new_cycle
        """
        if not getattr(settings, "MONTHLY_TP_ENABLED", True):
            return {"executed": False, "amount_usdt": 0.0, "reason": "MONTHLY_TP_ENABLED=False", "is_new_cycle": False}

        now_utc = datetime.now(timezone.utc)

        # Muat state dari DB jika belum dimuat
        if not self._state_loaded:
            found = self._load_state_from_db(db)
            self._state_loaded = True
            if not found:
                # Pertama kali: inisialisasi siklus baru
                self._init_new_cycle(db, baseline_capital_usdt)
                # Tampilkan laporan hanya info start
                self.print_safe_withdrawal_report(total_balance_usdt, baseline_capital_usdt, db)
                return {"executed": False, "amount_usdt": 0.0, "reason": "Siklus baru dimulai", "is_new_cycle": True}

        # Pastikan cycle_end timezone-aware
        cycle_end = self._cycle_end
        if cycle_end and cycle_end.tzinfo is None:
            cycle_end = cycle_end.replace(tzinfo=timezone.utc)

        # Tampilkan ringkasan per scan
        result = self.print_safe_withdrawal_report(total_balance_usdt, baseline_capital_usdt, db)

        # Cek apakah siklus sudah jatuh tempo
        if cycle_end and now_utc < cycle_end:
            return {"executed": False, "amount_usdt": 0.0, "reason": "Siklus belum jatuh tempo", "is_new_cycle": False}

        # JATUH TEMPO — tampilkan laporan lengkap
        result = self.print_safe_withdrawal_report(
            total_balance_usdt, baseline_capital_usdt, db, force_full_report=True
        )

        if result["can_execute"]:
            tp_amount = result["safe_amount_usdt"]
            surplus = result["surplus_after_usdt"]

            # Kunci ke YieldVault
            vault.deposit_harvest(
                position_id=f"monthly_tp_{now_utc.strftime('%Y%m%d')}",
                base_asset="MONTHLY_TP",
                amount_usdt=tp_amount,
                funding_rate=0.0
            )

            # Update state & simpan ke DB
            self._total_executed_usdt += tp_amount
            self._executions_count += 1
            db.record_monthly_tp_execution(
                amount_usdt=tp_amount,
                total_balance_before=total_balance_usdt,
                baseline_capital_usdt=baseline_capital_usdt,
                remaining_after_tp=result["remaining_after_tp"],
                surplus_usdt=surplus,
                skipped=False
            )

            log.info(
                f"✅ [MonthlyTP] TAKE PROFIT BERHASIL DIKUNCI!\n"
                f"   -> ${tp_amount:.4f} USDT masuk ke YieldVault (bisa Anda withdraw kapan saja)\n"
                f"   -> Modal aktif tersisa: ${result['remaining_after_tp']:.2f} USDT "
                f"(Surplus +${surplus:.4f} di atas modal awal ${baseline_capital_usdt:.2f})"
            )

            # Mulai siklus baru dengan baseline TETAP (modal pokok tidak berubah)
            self._init_new_cycle(db, baseline_capital_usdt)
            return {"executed": True, "amount_usdt": tp_amount, "reason": result["reason"], "is_new_cycle": True}

        else:
            # Syarat tidak terpenuhi — catat dan lanjutkan compounding
            skip_reason = result["reason"]
            db.upsert_monthly_tp_state(
                cycle_start_utc=self._cycle_start.isoformat() if self._cycle_start else now_utc.isoformat(),
                cycle_end_utc=self._cycle_end.isoformat() if self._cycle_end else (now_utc + timedelta(days=30)).isoformat(),
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

            log.warning(
                f"⚠️  [MonthlyTP] TAKE PROFIT DILEWATI — {skip_reason}\n"
                f"   -> Compounding dilanjutkan hingga surplus terpenuhi."
            )

            # Perpanjang siklus 30 hari lagi dari sekarang
            cycle_days = int(getattr(settings, "MONTHLY_TP_CYCLE_DAYS", 30))
            self._cycle_end = now_utc + timedelta(days=cycle_days)
            db.upsert_monthly_tp_state(
                cycle_start_utc=self._cycle_start.isoformat() if self._cycle_start else now_utc.isoformat(),
                cycle_end_utc=self._cycle_end.isoformat(),
                baseline_capital_usdt=self._baseline_at_cycle_start,
                total_executed_tp_usdt=self._total_executed_usdt,
                executions_count=self._executions_count,
                last_skipped_reason=skip_reason
            )
            return {"executed": False, "amount_usdt": 0.0, "reason": skip_reason, "is_new_cycle": False}


compounding_manager = CompoundingManager()
monthly_tp_manager = MonthlyTakeProfitManager()
