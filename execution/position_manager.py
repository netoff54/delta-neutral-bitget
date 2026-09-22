import json
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from core.models import DeltaNeutralPosition, PositionLeg
from utils.logger import log

class PositionManager:
    """
    Manajer status posisi aktif Delta-Neutral.
    Menyimpan data posisi ke file JSON lokal agar status tetap persisten
    meskipun bot di-restart.
    """

    def __init__(self, data_file: str = "data/positions.json"):
        self.data_path = Path(data_file)
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        self.positions: Dict[str, DeltaNeutralPosition] = {}
        self.load_positions()

    def load_positions(self):
        """Memuat posisi dari file penyimpanan lokal."""
        if not self.data_path.exists():
            self.positions = {}
            return

        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                self.positions = {
                    pos_id: DeltaNeutralPosition.model_validate(pos_data)
                    for pos_id, pos_data in raw_data.items()
                }
            log.info(f"Berhasil memuat {len(self.positions)} posisi dari {self.data_path}")
        except Exception as e:
            log.error(f"Gagal membaca file data posisi: {e}")
            self.positions = {}

    def save_positions(self):
        """Menyimpan posisi aktif saat ini ke file disk."""
        try:
            serialized = {
                pos_id: pos.model_dump(mode="json")
                for pos_id, pos in self.positions.items()
            }
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(serialized, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Gagal menyimpan data posisi: {e}")

    def add_position(self, position: DeltaNeutralPosition):
        """Menambahkan posisi baru."""
        self.positions[position.position_id] = position
        self.save_positions()
        log.info(f"[PositionManager] Posisi {position.position_id} ({position.base_asset}) berhasil dicatat.")

    def update_position(self, position: DeltaNeutralPosition):
        """Memperbarui data posisi aktif."""
        self.positions[position.position_id] = position
        self.save_positions()

    def remove_position(self, position_id: str):
        """Menghapus posisi yang sudah ditutup."""
        if position_id in self.positions:
            del self.positions[position_id]
            self.save_positions()
            log.info(f"[PositionManager] Posisi {position_id} telah dihapus dari daftar aktif.")

    def get_active_positions(self) -> List[DeltaNeutralPosition]:
        """Mengambil semua posisi yang berstatus 'OPEN'."""
        return [pos for pos in self.positions.values() if pos.status == "OPEN"]

    def get_position_by_base(self, base_asset: str) -> Optional[DeltaNeutralPosition]:
        """Cari posisi aktif berdasarkan aset acuan."""
        for pos in self.positions.values():
            if pos.base_asset == base_asset and pos.status == "OPEN":
                return pos
        return None

position_manager = PositionManager()
