import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

from config.settings import settings
from core.models import DeltaNeutralPosition, PositionLeg
from core.database import db
from utils.logger import log
from utils.interval_helper import parse_interval_hours

class PositionManager:
    """
    Manajer status posisi aktif Delta-Neutral.
    Menyimpan data posisi ke file JSON lokal dan Database Terpadu (SQLite/PostgreSQL)
    agar status tetap persisten meskipun bot di-restart atau di-redeploy di cloud.
    """

    def __init__(self, data_file: str = "data/positions.json", db_instance: Optional[Any] = None):
        self.data_path = Path(data_file)
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        self.positions: Dict[str, DeltaNeutralPosition] = {}
        self.db = db_instance if db_instance is not None else db
        self.load_positions()

    def load_positions(self):
        """Memuat posisi dari file penyimpanan lokal atau fallback ke database."""
        loaded_from_disk = False
        if self.data_path.exists():
            try:
                with open(self.data_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    self.positions = {
                        pos_id: DeltaNeutralPosition.model_validate(pos_data)
                        for pos_id, pos_data in raw_data.items()
                    }
                if self.positions:
                    log.info(f"Berhasil memuat {len(self.positions)} posisi dari {self.data_path}")
                    loaded_from_disk = True
            except Exception as e:
                log.error(f"Gagal membaca file data posisi: {e}")
                self.positions = {}

        # Jika di disk kosong (misal baru redeploy Render), coba muat posisi OPEN dari database
        if not loaded_from_disk or not self.positions:
            try:
                db_positions = self.db.get_active_positions_from_db()
                if db_positions:
                    for row in db_positions:
                        base = row["base_asset"]
                        spot_leg = PositionLeg(
                            market_type="spot",
                            symbol=row["spot_symbol"],
                            side="buy",
                            amount=float(row["spot_amount"]),
                            entry_price=float(row["entry_spot_price"]),
                            current_price=float(row["entry_spot_price"]),
                            nominal_usdt=float(row["spot_amount"]) * float(row["entry_spot_price"]),
                            fee_paid=0.0
                        )
                        perp_leg = PositionLeg(
                            market_type="perp",
                            symbol=row["perp_symbol"],
                            side="short",
                            amount=float(row["perp_amount"]),
                            entry_price=float(row["entry_perp_price"]),
                            current_price=float(row["entry_perp_price"]),
                            nominal_usdt=float(row["perp_amount"]) * float(row["entry_perp_price"]),
                            fee_paid=0.0
                        )
                        dn_pos = DeltaNeutralPosition(
                            position_id=row["position_id"],
                            base_asset=base,
                            spot_leg=spot_leg,
                            perp_leg=perp_leg,
                            leverage=int(row["leverage"]),
                            funding_interval_hours=int(row["funding_interval_hours"]),
                            entry_time=datetime.fromisoformat(row["entry_time"]) if "T" in str(row["entry_time"]) else datetime.now(timezone.utc),
                            net_delta=float(row["net_delta"]),
                            cumulative_funding_received=float(row.get("cumulative_funding_usdt", 0.0) or 0.0),
                            status=row["status"]
                        )
                        self.positions[dn_pos.position_id] = dn_pos
                    log.info(f"💾 [PositionManager] Berhasil memulihkan {len(self.positions)} posisi aktif langsung dari Database!")
            except Exception as dbe:
                log.debug(f"[PositionManager] Database recovery notice: {dbe}")

    def save_positions(self):
        """Menyimpan posisi aktif saat ini ke file disk dan database."""
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
        try:
            self.db.record_position(position)
        except Exception as e:
            log.error(f"[PositionManager] Gagal merekam posisi ke DB: {e}")
        log.info(f"[PositionManager] Posisi {position.position_id} ({position.base_asset}) berhasil dicatat di memory & database.")

    def update_position(self, position: DeltaNeutralPosition):
        """Memperbarui data posisi aktif."""
        self.positions[position.position_id] = position
        self.save_positions()
        try:
            self.db.record_position(position)
        except Exception as e:
            log.debug(f"[PositionManager] Gagal update posisi ke DB: {e}")

    def remove_position(self, position_id: str):
        """Menghapus posisi yang sudah ditutup."""
        if position_id in self.positions:
            pos = self.positions[position_id]
            pos.status = "CLOSED"
            pos.closed_at = datetime.now(timezone.utc)
            try:
                self.db.close_position_in_db(position_id, exit_reason="REMOVED", net_pnl=pos.net_pnl_usdt)
            except Exception as e:
                log.debug(f"[PositionManager] Gagal menutup posisi di DB: {e}")
            del self.positions[position_id]
            self.save_positions()
            log.info(f"[PositionManager] Posisi {position_id} telah ditutup dan diarsipkan di database.")

    def get_active_positions(self) -> List[DeltaNeutralPosition]:
        """Mengambil semua posisi yang berstatus 'OPEN'."""
        return [pos for pos in self.positions.values() if pos.status == "OPEN"]

    def get_position_by_base(self, base_asset: str) -> Optional[DeltaNeutralPosition]:
        """Cari posisi aktif berdasarkan aset acuan."""
        for pos in self.positions.values():
            if pos.base_asset == base_asset and pos.status == "OPEN":
                return pos
        return None

    async def sync_active_positions_from_exchange(self, client: Any) -> List[DeltaNeutralPosition]:
        """
        Sinkronisasi posisi aktif langsung dari Bitget Exchange.
        Sangat krusial untuk cloud deployment (Render/Railway/Docker) yang memiliki
        ephemeral disk, atau saat restart agar bot langsung mengenali posisi terbuka
        di exchange tanpa kehilangan kendali pelindung risiko (Margin/Funding Guard).
        """
        if settings.DRY_RUN:
            return self.get_active_positions()

        try:
            # 1. Ambil posisi futures terbuka langsung dari Bitget
            positions = await client.client.fetch_positions(params={"productType": "USDT-FUTURES"})
            active_perps = [
                p for p in positions
                if float(p.get("contracts", 0.0) or p.get("size", 0.0) or 0.0) > 0
            ]

            # 2. Ambil saldo spot untuk mencocokkan kaki spot
            spot_bal = await client.client.fetch_balance({"type": "spot"})
            spot_totals = spot_bal.get("total", {})

            active_bases = set()

            for p in active_perps:
                symbol = p.get("symbol", "")  # Contoh: ARX/USDT:USDT
                if "/" not in symbol:
                    continue
                base = symbol.split("/")[0].upper()
                active_bases.add(base)

                contracts = float(p.get("contracts", 0.0) or p.get("size", 0.0) or 0.0)
                entry_price = float(p.get("entryPrice", 0.0) or p.get("markPrice", 0.0) or 0.0)
                side = p.get("side", "short")
                leverage = int(float(p.get("leverage", 1) or 1))

                spot_qty = float(spot_totals.get(base, 0.0) or 0.0)
                spot_sym = f"{base}/USDT"

                existing_pos = self.get_position_by_base(base)

                if existing_pos:
                    # Update data kuantitas jika terjadi perubahan
                    existing_pos.perp_leg.amount = contracts
                    if spot_qty > 0:
                        existing_pos.spot_leg.amount = spot_qty
                    existing_pos.net_delta = round(existing_pos.spot_leg.amount - existing_pos.perp_leg.amount, 6)
                    existing_pos.leverage = leverage
                    await client.update_position_live_pnl(existing_pos)
                    self.update_position(existing_pos)
                    log.info(f"[PositionManager] Posisi {base} disinkronkan dari exchange (Net Delta: {existing_pos.net_delta}).")
                else:
                    # Posisi baru terdeteksi dari exchange (misal setelah redeploy Render / restart)
                    interval_hours = 8
                    try:
                        fr = await client.client.fetch_funding_rate(symbol)
                        interval_raw = (
                            fr.get("interval") or
                            fr.get("info", {}).get("fundingRateInterval") or
                            fr.get("info", {}).get("ratePeriod")
                        )
                        interval_hours = parse_interval_hours(interval_raw, default=8)
                    except Exception as fe:
                        log.debug(f"Gagal mengambil interval funding untuk {symbol}: {fe}")

                    spot_amount = spot_qty if spot_qty > 0 else contracts
                    spot_leg = PositionLeg(
                        market_type="spot",
                        symbol=spot_sym,
                        side="buy",
                        amount=spot_amount,
                        entry_price=entry_price,
                        current_price=entry_price,
                        nominal_usdt=spot_amount * entry_price,
                        fee_paid=0.0
                    )
                    perp_leg = PositionLeg(
                        market_type="perp",
                        symbol=symbol,
                        side=side,
                        amount=contracts,
                        entry_price=entry_price,
                        current_price=entry_price,
                        nominal_usdt=contracts * entry_price,
                        fee_paid=0.0
                    )

                    new_pos = DeltaNeutralPosition(
                        position_id=f"dn-{base.lower()}-synced",
                        base_asset=base,
                        spot_leg=spot_leg,
                        perp_leg=perp_leg,
                        leverage=leverage,
                        funding_interval_hours=interval_hours,
                        entry_time=datetime.now(timezone.utc),
                        net_delta=round(spot_amount - contracts, 6),
                        status="OPEN"
                    )

                    await client.update_position_live_pnl(new_pos)
                    self.add_position(new_pos)
                    log.info(
                        f"🛡️ [PositionManager] Berhasil merekonsiliasi posisi {base} langsung dari Bitget! "
                        f"Spot: {spot_amount} {base} | Perp Short: {contracts} {base} | "
                        f"Unrealized PnL: ${new_pos.unrealized_pnl_usdt:+.4f}"
                    )

            # 3. Tandai CLOSED untuk posisi lokal yang sudah tidak ada di Bitget
            for pos_id, pos in list(self.positions.items()):
                if pos.status == "OPEN" and pos.base_asset not in active_bases:
                    log.warning(
                        f"[PositionManager] Posisi {pos.base_asset} ({pos_id}) sudah tidak aktif di Bitget. "
                        f"Mengubah status menjadi CLOSED."
                    )
                    pos.status = "CLOSED"
                    pos.closed_at = datetime.now(timezone.utc)
                    self.update_position(pos)

        except Exception as e:
            log.error(f"[PositionManager] Gagal merekonsiliasi posisi dari exchange: {e}")

        return self.get_active_positions()

position_manager = PositionManager()

