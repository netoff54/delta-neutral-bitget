import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta

from core.models import YieldVaultRecord, YieldVaultSummary
from config.settings import settings
from utils.logger import log


class YieldVault:
    """
    Brankas Perlindungan Profit (Yield Vault) — Persisten di PostgreSQL.

    Desain:
    1. PRIMARY STORAGE: PostgreSQL (via core.database) — tahan restart/redeploy Render.
    2. FALLBACK: data/yield_vault.json — untuk development lokal / jika DB tidak tersedia.
    3. VIRTUAL ACCOUNTING: Vault tidak memindahkan uang di Bitget. Dana tetap di Bitget
       sampai Anda menariknya manual. Vault hanya mencatat nominal yang "sudah menjadi milik Anda
       untuk ditarik" agar bot tidak menghitung ulang sebagai working capital.
    4. AUTO-REINVEST: Record MONTHLY_TP yang tidak ditarik setelah N hari otomatis
       dikembalikan ke modal compounding — compounding makin besar.
    """

    def __init__(self, vault_file: str = "data/yield_vault.json"):
        self.vault_path = Path(vault_file)
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = None           # Lazy-loaded dari core.database
        self.records: List[Dict] = []
        self.total_locked_usdt: float = 0.0
        self._load_from_db_or_file()

    def _get_db(self):
        """Lazy-load database singleton untuk menghindari circular import."""
        if self._db is None:
            try:
                from core.database import db as _db
                self._db = _db
            except Exception as e:
                log.warning(f"[YieldVault] DB import notice: {e}")
        return self._db

    def _load_from_db_or_file(self):
        """
        Prioritas load: PostgreSQL DB → fallback ke JSON file.
        Jika DB tersedia dan ada record, gunakan DB (sumber kebenaran utama).
        """
        db = self._get_db()
        if db:
            try:
                records = db.get_vault_records(only_active=True)
                total = db.get_vault_total_locked()
                self.records = records
                self.total_locked_usdt = round(total, 6)
                log.info(
                    f"[YieldVault] 🔒 Dimuat dari DB: ${self.total_locked_usdt:.4f} USDT "
                    f"terkunci aman dari {len(records)} record."
                )
                return
            except Exception as e:
                log.debug(f"[YieldVault] DB load notice, fallback ke JSON: {e}")

        # Fallback: JSON file (local dev / DB unavailable)
        self._load_from_file()

    def _load_from_file(self):
        """Fallback: muat vault dari JSON lokal."""
        if not self.vault_path.exists():
            self.records = []
            self.total_locked_usdt = 0.0
            return
        try:
            with open(self.vault_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.total_locked_usdt = float(data.get("total_locked_usdt", 0.0))
                self.records = data.get("records", [])
            log.info(f"[YieldVault] Dimuat dari JSON: ${self.total_locked_usdt:.4f} USDT dari {len(self.records)} record.")
        except Exception as e:
            log.error(f"[YieldVault] Gagal load JSON: {e}")
            self.records = []
            self.total_locked_usdt = 0.0

    def _save_to_file(self):
        """Backup ke JSON file (untuk local dev dan sebagai secondary backup)."""
        try:
            payload = {
                "total_locked_usdt": round(self.total_locked_usdt, 6),
                "records_count": len(self.records),
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "records": self.records[:50]  # Simpan max 50 terbaru
            }
            with open(self.vault_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception as e:
            log.debug(f"[YieldVault] JSON backup notice: {e}")

    def deposit_harvest(
        self,
        position_id: str,
        base_asset: str,
        amount_usdt: float,
        funding_rate: float
    ) -> Optional[int]:
        """
        Kunci profit ke Vault.
        - Untuk MONTHLY_TP: set auto_reinvest_deadline = sekarang + N hari (default 7)
        - Untuk non-MONTHLY_TP: tidak ada auto-reinvest deadline

        Returns: DB record id (untuk tracking), atau None jika gagal.
        """
        if amount_usdt <= 0:
            return None

        now_utc = datetime.now(timezone.utc)
        notes = f"Locked yield from {base_asset} ({funding_rate*100:.4f}%)"

        # Auto-reinvest deadline: hanya untuk MONTHLY_TP
        auto_reinvest_deadline = None
        if base_asset == "MONTHLY_TP":
            reinvest_days = int(getattr(settings, "MONTHLY_TP_AUTO_REINVEST_DAYS", 7))
            auto_reinvest_deadline = (now_utc + timedelta(days=reinvest_days)).isoformat()

        record_id = None
        db = self._get_db()
        if db:
            try:
                record_id = db.record_vault_deposit(
                    position_id=position_id,
                    base_asset=base_asset,
                    amount_usdt=amount_usdt,
                    funding_rate=funding_rate,
                    notes=notes,
                    deposited_at=now_utc.isoformat(),
                    auto_reinvest_deadline=auto_reinvest_deadline
                )
            except Exception as e:
                log.warning(f"[YieldVault] DB deposit notice: {e}")

        # Update in-memory
        self.total_locked_usdt = round(self.total_locked_usdt + amount_usdt, 6)
        record_dict = {
            "id": record_id,
            "position_id": position_id,
            "base_asset": base_asset,
            "amount_usdt": round(amount_usdt, 6),
            "funding_rate": funding_rate,
            "notes": notes,
            "deposited_at": now_utc.isoformat(),
            "auto_reinvest_deadline": auto_reinvest_deadline,
            "is_released": 0
        }
        self.records.append(record_dict)
        self._save_to_file()

        auto_info = ""
        if auto_reinvest_deadline:
            auto_info = f" | Auto-reinvest dalam {getattr(settings, 'MONTHLY_TP_AUTO_REINVEST_DAYS', 7)} hari jika tidak ditarik"

        log.info(
            f"🔒 [Yield Vault] PROFIT TERKUNCI: +${amount_usdt:.4f} USDT dari {base_asset}.{auto_info}\n"
            f"   -> Total Terkunci Aman: ${self.total_locked_usdt:.4f} USDT"
        )
        return record_id

    def get_dormant_tp_records(self) -> List[Dict]:
        """
        Ambil record MONTHLY_TP yang belum ditarik dalam N hari.
        Ini adalah kandidat untuk auto-reinvest ke modal compounding.
        """
        db = self._get_db()
        if db:
            try:
                return db.get_dormant_vault_records()
            except Exception as e:
                log.debug(f"[YieldVault] get_dormant_tp_records notice: {e}")

        # Fallback: cek in-memory records
        now_iso = datetime.now(timezone.utc).isoformat()
        dormant = []
        for r in self.records:
            deadline = r.get("auto_reinvest_deadline")
            if (
                not r.get("is_released", False)
                and r.get("base_asset") == "MONTHLY_TP"
                and deadline
                and deadline <= now_iso
            ):
                dormant.append(r)
        return dormant

    def release_record_for_reinvest(self, record_id: int) -> float:
        """
        Rilis satu record vault untuk auto-reinvest.
        Returns: jumlah USDT yang dirilis (0.0 jika gagal/tidak ditemukan).
        """
        db = self._get_db()
        if db:
            try:
                amount = db.release_vault_record(record_id, release_type="AUTO_REINVEST")
                if amount > 0:
                    self.total_locked_usdt = max(0.0, round(self.total_locked_usdt - amount, 6))
                    # Update in-memory record
                    for r in self.records:
                        if r.get("id") == record_id:
                            r["is_released"] = 1
                            r["release_type"] = "AUTO_REINVEST"
                            break
                    self._save_to_file()
                    return amount
            except Exception as e:
                log.warning(f"[YieldVault] release_record notice: {e}")
        return 0.0

    def refresh_from_db(self):
        """Paksa reload total dari DB (dipanggil setelah redeploy / startup)."""
        db = self._get_db()
        if db:
            try:
                self.total_locked_usdt = round(db.get_vault_total_locked(), 6)
                self.records = db.get_vault_records(only_active=True)
            except Exception as e:
                log.debug(f"[YieldVault] refresh notice: {e}")

    def get_available_working_capital(self, total_balance_usdt: float) -> float:
        """
        Modal kerja yang sah untuk trading = total saldo Bitget - vault locked.
        Vault locked masih fisik di Bitget, tapi sudah "diklaim" untuk ditarik.
        """
        available = max(0.0, total_balance_usdt - self.total_locked_usdt)
        return round(available, 2)

    def can_allocate_capital(self, requested_capital: float, total_balance_usdt: float) -> bool:
        """Validasi alokasi modal tidak melanggar vault protection."""
        available = self.get_available_working_capital(total_balance_usdt)
        return available >= requested_capital

    def get_summary(self) -> YieldVaultSummary:
        """Ringkasan status vault."""
        return YieldVaultSummary(
            total_harvested_usdt=self.total_locked_usdt,
            locked_reserve_usdt=self.total_locked_usdt,
            records_count=len([r for r in self.records if not r.get("is_released", False)]),
            last_updated=datetime.now(timezone.utc)
        )


yield_vault = YieldVault()
