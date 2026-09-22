import json
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

from core.models import YieldVaultRecord, YieldVaultSummary
from utils.logger import log

class YieldVault:
    """
    Brankas Perlindungan Hasil Earn (Yield Vault):
    Prinsip: 'JANGAN SENTUH UANG YANG DI-EARN'
    
    Menjamin seluruh funding fee yang dipanen disimpan secara terisolasi.
    Uang ini dikunci dan TIDAK BOLEH digunakan kembali sebagai modal trading,
    sehingga profit bersih terproteksi 100%.
    """

    def __init__(self, vault_file: str = "data/yield_vault.json"):
        self.vault_path = Path(vault_file)
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        self.records: List[YieldVaultRecord] = []
        self.total_locked_usdt: float = 0.0
        self.load_vault()

    def load_vault(self):
        """Memuat catatan vault dari file disk."""
        if not self.vault_path.exists():
            self.records = []
            self.total_locked_usdt = 0.0
            return

        try:
            with open(self.vault_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.records = [YieldVaultRecord.model_validate(r) for r in data.get("records", [])]
                self.total_locked_usdt = float(data.get("total_locked_usdt", 0.0))
            log.info(f"[YieldVault] Terkunci: ${self.total_locked_usdt:.4f} USDT dari {len(self.records)} kali panen.")
        except Exception as e:
            log.error(f"Gagal memuat YieldVault: {e}")
            self.records = []
            self.total_locked_usdt = 0.0

    def save_vault(self):
        """Menyimpan brankas ke disk."""
        try:
            payload = {
                "total_locked_usdt": round(self.total_locked_usdt, 6),
                "records_count": len(self.records),
                "last_updated": datetime.utcnow().isoformat(),
                "records": [r.model_dump(mode="json") for r in self.records]
            }
            with open(self.vault_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Gagal menyimpan YieldVault: {e}")

    def deposit_harvest(
        self,
        position_id: str,
        base_asset: str,
        amount_usdt: float,
        funding_rate: float
    ):
        """
        Menyetor profit funding fee ke dalam brankas dan menguncinya permanen.
        """
        if amount_usdt <= 0:
            return

        record = YieldVaultRecord(
            timestamp=datetime.utcnow(),
            position_id=position_id,
            base_asset=base_asset,
            amount_usdt=round(amount_usdt, 6),
            funding_rate=funding_rate,
            notes=f"Locked yield from {base_asset} ({funding_rate*100:.4f}%)"
        )
        self.records.append(record)
        self.total_locked_usdt += amount_usdt
        self.save_vault()

        log.info(
            f"🔒 [Yield Vault] PROFIT TERKUNCI: +${amount_usdt:.4f} USDT dari {base_asset}. "
            f"Total Terkunci Aman: ${self.total_locked_usdt:.4f} USDT (Tidak boleh disentuh untuk trading)"
        )

    def get_available_working_capital(self, total_balance_usdt: float) -> float:
        """
        Menghitung modal kerja yang sah digunakan untuk trading.
        Modal kerja = Total Saldo Akun - Dana Profit Terkunci di Brankas.
        """
        available = max(0.0, total_balance_usdt - self.total_locked_usdt)
        return round(available, 2)

    def can_allocate_capital(self, requested_capital: float, total_balance_usdt: float) -> bool:
        """
        Memvalidasi apakah pembukaan posisi baru akan melanggar brankas profit.
        """
        available = self.get_available_working_capital(total_balance_usdt)
        return available >= requested_capital

    def get_summary(self) -> YieldVaultSummary:
        """Mendapatkan ringkasan status brankas."""
        return YieldVaultSummary(
            total_harvested_usdt=self.total_locked_usdt,
            locked_reserve_usdt=self.total_locked_usdt,
            records_count=len(self.records),
            last_updated=datetime.utcnow()
        )

yield_vault = YieldVault()
