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
    initial_seed_capital: float = 20.0
    total_profit_compounded: float = 0.0
    current_compounded_capital: float = 20.0
    harvest_events_count: int = 0
    last_updated: datetime = Field(default_factory=datetime.utcnow)

class CompoundingManager:
    """
    Manajer Compounding & Proteksi Saldo Bitget Earn:
    
    1. Compounding: Seluruh profit funding fee yang dihasilkan otomatis dimasukkan kembali
       ke modal trading delta-neutral untuk memperbesar ukuran posisi berikutnya.
    2. Isolasi Mutlak Akun Bitget Earn:
       Bot hanya memiliki hak akses modal sebesar:
           Maksimal Modal = Modal Pokok ($20.0) + Akumulasi Profit Delta-Neutral
       Dana tabungan atau produk 'Earn' milik pengguna di Bitget tidak akan pernah
       disentuh atau ditarik oleh bot dalam keadaan apa pun.
    """

    def __init__(self, state_file: str = "data/compounding_state.json"):
        self.state_path = Path(state_file)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_seed: float = settings.INITIAL_SEED_CAPITAL_USDT
        self.total_profit_compounded: float = 0.0
        self.current_capital: float = self.initial_seed
        self.events: List[HarvestEvent] = []
        self.load_state()

    def load_state(self):
        """Memuat riwayat compounding dari disk."""
        if not self.state_path.exists():
            self.current_capital = self.initial_seed
            self.total_profit_compounded = 0.0
            self.events = []
            self.save_state()
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.initial_seed = float(data.get("initial_seed_capital", settings.INITIAL_SEED_CAPITAL_USDT))
                self.total_profit_compounded = float(data.get("total_profit_compounded", 0.0))
                self.current_capital = round(self.initial_seed + self.total_profit_compounded, 4)
                self.events = [HarvestEvent.model_validate(e) for e in data.get("events", [])]
            log.info(
                f"[CompoundingManager] Modal Ter-Compound: ${self.current_capital:.4f} USDT "
                f"(Modal Pokok: ${self.initial_seed:.2f} + Profit Diputar: ${self.total_profit_compounded:.4f})"
            )
        except Exception as e:
            log.error(f"Gagal memuat compounding state: {e}")
            self.current_capital = self.initial_seed
            self.total_profit_compounded = 0.0

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
        Mendapatkan ukuran modal dinamis untuk posisi berikutnya:
        - Jika USE_ALL_AVAILABLE_BALANCE aktif, gunakan 100% modal cair yang ada di dompet Spot & Futures (dikurangi buffer 2%).
        - Menjamin dana tabungan di Bitget Earn sama sekali tidak tersentuh.
        """
        if settings.USE_ALL_AVAILABLE_BALANCE and available_liquid_usdt is not None and available_liquid_usdt > 0:
            usable = available_liquid_usdt * (1.0 - settings.LIQUID_SAFETY_BUFFER_PERCENT)
            per_pos = usable / max(1, settings.MAX_CONCURRENT_POSITIONS)
            if settings.MAX_CAPITAL_PER_POSITION_USDT:
                per_pos = min(per_pos, settings.MAX_CAPITAL_PER_POSITION_USDT)
            floor_val = math.floor(per_pos * 100.0) / 100.0
            return max(5.0, floor_val)

        # Fallback ke modal ter-compound lokal jika balance live tidak dilewatkan
        return round(self.current_capital, 2)

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
