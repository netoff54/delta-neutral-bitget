import json
import math
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
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
        # Seed kapital 0 → akan diisi dari saldo REAL Bitget saat pertama kali scan
        self.initial_seed: float = 0.0
        self.total_profit_compounded: float = 0.0
        self.current_capital: float = 0.0
        self.events: List[HarvestEvent] = []
        self.load_state()

    def load_state(self):
        """Memuat riwayat compounding dari disk."""
        if not self.state_path.exists():
            # File belum ada → akan di-sync dari saldo REAL Bitget saat pertama berjalan
            self.current_capital = 0.0
            self.total_profit_compounded = 0.0
            self.events = []
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                saved_seed = float(data.get("initial_seed_capital", 0.0))
                self.initial_seed = saved_seed
                self.total_profit_compounded = float(data.get("total_profit_compounded", 0.0))
                self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
                self.events = [HarvestEvent.model_validate(e) for e in data.get("events", [])]
            if self.initial_seed > 0:
                log.info(
                    f"[CompoundingManager] Modal Ter-Compound: ${self.current_capital:.4f} USDT "
                    f"(Modal Pokok Real: ${self.initial_seed:.2f} + Profit Diputar: ${self.total_profit_compounded:.4f})"
                )
            else:
                log.info("[CompoundingManager] Menunggu sinkronisasi saldo real dari Bitget...")
        except Exception as e:
            log.error(f"Gagal memuat compounding state: {e}")
            self.current_capital = 0.0
            self.total_profit_compounded = 0.0

    def sync_from_real_balance(self, real_liquid_usdt: float):
        """
        Sinkronisasi modal pokok dari saldo REAL Bitget.
        Dipanggil setiap siklus utama agar modal selalu mencerminkan saldo aktual.
        TIDAK PERNAH menggunakan angka fiktif/statis/mock.
        """
        if real_liquid_usdt <= 0:
            return
        if self.initial_seed <= 0:
            # Pertama kali: set seed dari saldo real
            self.initial_seed = round(real_liquid_usdt, 4)
            self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
            self.save_state()
            log.info(
                f"[CompoundingManager] 🔗 Modal pokok diinisialisasi dari saldo REAL Bitget: "
                f"${self.initial_seed:.4f} USDT (Total Modal Aktif: ${self.current_capital:.4f} USDT)"
            )
        else:
            # Siklus berikutnya: perbarui modal aktif jika saldo real > modal ter-compound
            # (misalnya user deposit lebih banyak atau ada peningkatan dari spot/futures)
            real_total = real_liquid_usdt + self.total_profit_compounded
            if real_liquid_usdt > self.initial_seed * 1.01:  # Ada kenaikan > 1%
                old_seed = self.initial_seed
                self.initial_seed = round(real_liquid_usdt, 4)
                self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
                self.save_state()
                log.info(
                    f"[CompoundingManager] 📈 Modal pokok diperbarui dari saldo real: "
                    f"${old_seed:.4f} → ${self.initial_seed:.4f} USDT "
                    f"(Modal Aktif Total: ${self.current_capital:.4f} USDT)"
                )


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

compounding_manager = CompoundingManager()
