import asyncio
import uuid
from datetime import datetime
from typing import Optional, Tuple

from config.settings import settings
from utils.logger import log
from utils.notifier import notifier
from core.models import (
    Opportunity,
    DeltaNeutralPosition,
    PositionLeg,
    OrderExecutionResult
)
from core.bitget_client import BitgetClient, bitget_client
from execution.position_manager import PositionManager, position_manager
from core.database import db

class OrderExecutor:
    """
    Engine eksekusi order sinkron untuk strategi Delta-Neutral.
    Menjamin kedua kaki (Spot Long dan Futures Short) dieksekusi secara terkalibrasi,
    dengan proteksi auto-unwind jika salah satu kaki gagal.
    """

    def __init__(
        self,
        client: BitgetClient = bitget_client,
        pos_mgr: PositionManager = position_manager
    ):
        self.client = client
        self.pos_mgr = pos_mgr

    async def open_delta_neutral_position(
        self,
        opportunity: Opportunity,
        allocated_capital_usdt: Optional[float] = None
    ) -> Optional[DeltaNeutralPosition]:
        """
        Membuka posisi sinkron Spot Long + Perp Short dengan modal ter-compound.
        """
        from risk.compounding_manager import compounding_manager

        # 1. Ambil saldo trading cair (Spot Free + Futures Free) - Mengabaikan produk Bitget Earn
        liquid_bal = await self.client.fetch_liquid_trading_balance()
        free_liquid_usdt = liquid_bal["total_liquid_usdt"]

        # Jika capital tidak ditentukan, gunakan seluruh modal trading cair yang tersedia
        if allocated_capital_usdt is None or allocated_capital_usdt <= 0:
            allocated_capital_usdt = compounding_manager.get_position_capital(free_liquid_usdt)

        base = opportunity.base_asset
        if self.pos_mgr.get_position_by_base(base):
            log.warning(f"Posisi untuk {base} sudah aktif. Melewatkan pembukaan posisi baru.")
            return None

        # Proteksi Mutlak Akun Bitget Earn:
        if not compounding_manager.validate_capital_usage(allocated_capital_usdt, free_liquid_usdt):
            log.warning(
                f"[Bitget Earn Protection] Pembukaan posisi {base} dibatalkan untuk melindungi saldo Bitget Earn Anda! "
                f"Saldo trading cair tersedia: ${free_liquid_usdt:.2f} USDT."
            )
            return None

        if free_liquid_usdt < allocated_capital_usdt:
            log.warning(f"Saldo USDT cair di dompet trading (${free_liquid_usdt:.2f}) < Kebutuhan (${allocated_capital_usdt:.2f}).")
            return None

        # Leverage sesuai pengaturan bot (Maksimal 1x)
        effective_leverage = max(1, settings.LEVERAGE)

        # Alokasi nominal per leg agar total (Spot + Margin Perp) pas dengan allocated_capital_usdt:
        # Total Capital = Spot_Cost + Perp_Margin = Spot_Nominal * (1.0 + 1.0 / effective_leverage)
        # Spot_Nominal = allocated_capital_usdt / (1.0 + 1.0 / effective_leverage)
        leg_nominal_usdt = allocated_capital_usdt / (1.0 + (1.0 / effective_leverage))

        # Hitung kuantitas yang terkalibrasi presisi agar Net Delta = 0
        aligned_qty = self.client.align_quantity(
            spot_symbol=opportunity.spot_symbol,
            perp_symbol=opportunity.perp_symbol,
            target_nominal_usdt=leg_nominal_usdt,
            price=opportunity.spot_price
        )

        min_cost_info = self.client.get_min_order_cost(opportunity.spot_symbol, opportunity.perp_symbol)
        if aligned_qty <= 0 or (aligned_qty * opportunity.spot_price) < min_cost_info["perp_cost_min"]:
            log.warning(
                f"Kuantitas {base} ({aligned_qty} token, ~${aligned_qty * opportunity.spot_price:.2f}) "
                f"di bawah batas minimum order exchange (Futures Min: ${min_cost_info['perp_cost_min']:.2f} USDT)."
            )
            return None

        # Hitung kebutuhan dana per dompet (Spot vs Futures Margin)
        spot_needed = aligned_qty * opportunity.spot_price
        margin_needed = (aligned_qty * opportunity.perp_price) / effective_leverage

        log.info(
            f"[Buka Posisi] Memulai eksekusi {base}: "
            f"Beli Spot {aligned_qty} token (~${spot_needed:.2f}) & Short Perp {aligned_qty} token "
            f"(Margin: ~${margin_needed:.2f} @ {effective_leverage}x | Total Modal: ${allocated_capital_usdt:.2f})"
        )

        # 0. Seimbangkan saldo USDT antara dompet Spot & Dompet Futures jika diperlukan
        await self.client.balance_wallets_for_position(
            required_spot=spot_needed,
            required_swap=margin_needed
        )

        # 1. Atur leverage futures
        await self.client.set_leverage_for_symbol(
            perp_symbol=opportunity.perp_symbol,
            leverage=effective_leverage
        )

        # 1.5 Ambil live orderbook untuk FULL MAKER (Limit Post-Only) & JAMIN SPREAD POSITIF
        spot_order_type = "limit" if settings.USE_MAKER_ORDERS else "market"
        perp_order_type = "limit" if settings.USE_MAKER_ORDERS else "market"
        use_post_only = settings.USE_MAKER_ORDERS

        spot_entry_target_p = opportunity.spot_price
        perp_entry_target_p = opportunity.perp_price

        try:
            spot_ticker = await self.client.client.fetch_ticker(opportunity.spot_symbol)
            perp_ticker = await self.client.client.fetch_ticker(opportunity.perp_symbol)
            spot_bid = float(spot_ticker.get("bid") or spot_ticker.get("last") or opportunity.spot_price)
            perp_ask = float(perp_ticker.get("ask") or perp_ticker.get("last") or opportunity.perp_price)

            if settings.USE_MAKER_ORDERS:
                spot_entry_target_p = spot_bid
                perp_entry_target_p = perp_ask

            # Jamin Basis Spread Positif (Perp >= Spot):
            # Menjual Perp di harga yang lebih tinggi atau sama dengan harga beli Spot
            if settings.REQUIRE_POSITIVE_SPREAD:
                if perp_entry_target_p < spot_entry_target_p:
                    perp_entry_target_p = round(spot_entry_target_p * 1.0001, 6)
        except Exception as pe_err:
            log.debug(f"Live ticker maker pricing notice: {pe_err}")

        spread_init_pct = ((perp_entry_target_p - spot_entry_target_p) / spot_entry_target_p) * 100.0
        log.info(
            f"🎯 [Full Maker Entry] {base}: Mode Maker (Limit Post-Only) | "
            f"Spot Bid: ${spot_entry_target_p:,.4f} | Perp Ask: ${perp_entry_target_p:,.4f} | "
            f"Spread Terjamin: {spread_init_pct:+.3f}% (Positif)"
        )

        # 2. Eksekusi Kaki 1: Beli Spot sebagai MAKER (Limit Order)
        log.info(f"-> Eksekusi Kaki 1: Beli Spot {opportunity.spot_symbol} {aligned_qty} @ ${spot_entry_target_p:,.4f} (Maker)...")
        spot_res = await self.client.execute_spot_order(
            symbol=opportunity.spot_symbol,
            side="buy",
            amount=aligned_qty,
            order_type=spot_order_type,
            price=spot_entry_target_p,
            post_only=use_post_only
        )

        if not spot_res.success or spot_res.filled_amount <= 0:
            log.error(f"Gagal beli Spot {opportunity.spot_symbol}: {spot_res.error_message}. Membatalkan pembukaan posisi.")
            return None

        actual_qty = spot_res.filled_amount

        # 3. Eksekusi Kaki 2: Short Perp sebagai MAKER (sesuai jumlah spot yang terisi & presisi kontrak)
        perp_qty = actual_qty
        try:
            perp_qty = float(self.client.client.amount_to_precision(opportunity.perp_symbol, actual_qty))
        except Exception:
            pass

        log.info(f"-> Eksekusi Kaki 2: Short Perp {opportunity.perp_symbol} {perp_qty} @ ${perp_entry_target_p:,.4f} (Maker)...")
        perp_res = await self.client.execute_perp_order(
            symbol=opportunity.perp_symbol,
            side="sell",
            amount=perp_qty,
            order_type=perp_order_type,
            price=perp_entry_target_p,
            post_only=use_post_only
        )

        # Jika Kaki Perp Gagal -> Darurat Unwind: Jual kembali Spot agar portofolio tidak terpapar risiko arah
        if not perp_res.success:
            log.critical(
                f"[EMERGENCY UNWIND] Gagal Short Perp {opportunity.perp_symbol}: {perp_res.error_message}. "
                f"Melakukan auto-sell pada Spot {opportunity.spot_symbol} untuk melindungi modal!"
            )
            # Ambil saldo koin spot aktual yang bebas agar tidak ditolak karena selisih fee potong token
            sell_qty = actual_qty
            try:
                spot_bal = await self.client.client.fetch_balance({"type": "spot"})
                free_coin = float(spot_bal.get("free", {}).get(opportunity.base_asset, 0.0) or 0.0)
                if free_coin > 0:
                    formatted_free = float(self.client.client.amount_to_precision(opportunity.spot_symbol, free_coin))
                    sell_qty = min(actual_qty, formatted_free)
            except Exception as e:
                log.debug(f"Spot balance check notice on emergency unwind: {e}")

            await self.client.execute_spot_order(
                symbol=opportunity.spot_symbol,
                side="sell",
                amount=sell_qty,
                order_type="market"
            )
            return None

        # 4. Kedua kaki berhasil -> Catat Posisi Delta Neutral
        pos_id = f"DN_{base}_{uuid.uuid4().hex[:6]}"
        net_delta = round(spot_res.filled_amount - perp_res.filled_amount, 8)
        total_fees = spot_res.fee_amount + perp_res.fee_amount

        # Simpan jejak eksekusi order ke database
        try:
            db.record_order_execution(
                order_id=spot_res.order_id or f"spot-{uuid.uuid4().hex[:8]}",
                position_id=pos_id,
                market_type="spot",
                symbol=opportunity.spot_symbol,
                side="buy",
                requested_amount=aligned_qty,
                filled_amount=spot_res.filled_amount,
                price=spot_res.avg_price,
                fee_paid=spot_res.fee_amount,
                status="FILLED"
            )
            db.record_order_execution(
                order_id=perp_res.order_id or f"perp-{uuid.uuid4().hex[:8]}",
                position_id=pos_id,
                market_type="perp",
                symbol=opportunity.perp_symbol,
                side="short",
                requested_amount=perp_qty,
                filled_amount=perp_res.filled_amount,
                price=perp_res.avg_price,
                fee_paid=perp_res.fee_amount,
                status="FILLED"
            )
        except Exception as dbe:
            log.debug(f"[OrderExecutor] Gagal menyimpan order ke database: {dbe}")

        spot_leg = PositionLeg(
            market_type="spot",
            symbol=opportunity.spot_symbol,
            side="buy",
            amount=spot_res.filled_amount,
            entry_price=spot_res.avg_price,
            current_price=spot_res.avg_price,
            nominal_usdt=spot_res.filled_amount * spot_res.avg_price,
            fee_paid=spot_res.fee_amount
        )

        perp_leg = PositionLeg(
            market_type="perp",
            symbol=opportunity.perp_symbol,
            side="sell",
            amount=perp_res.filled_amount,
            entry_price=perp_res.avg_price,
            current_price=perp_res.avg_price,
            nominal_usdt=perp_res.filled_amount * perp_res.avg_price,
            fee_paid=perp_res.fee_amount
        )

        position = DeltaNeutralPosition(
            position_id=pos_id,
            base_asset=base,
            spot_leg=spot_leg,
            perp_leg=perp_leg,
            leverage=settings.LEVERAGE,
            funding_interval_hours=opportunity.funding_interval_hours,
            entry_time=datetime.utcnow(),
            net_delta=net_delta,
            total_fees_paid=total_fees,
            status="OPEN"
        )

        self.pos_mgr.add_position(position)

        # Rekam akumulasi pengalaman AGI untuk entri Full Maker & spread positif
        try:
            from core.historical_store import historical_store
            spread_entry_pct = ((perp_res.avg_price - spot_res.avg_price) / spot_res.avg_price) * 100.0
            historical_store.record_agi_experience(
                event_type="MAKER_ENTRY",
                base_asset=base,
                funding_rate=opportunity.current_funding_rate,
                harvest_usdt=0.0,
                net_pnl_usdt=-total_fees,
                holding_hours=0.0,
                was_bep_reached=False,
                ai_decision="FULL_MAKER_LIMIT_ENTRY",
                lesson_learned=f"Eksekusi Full Maker berhasil pada {base}. Spot Buy: ${spot_res.avg_price:,.4f}, Perp Short: ${perp_res.avg_price:,.4f}. Basis Spread Awal: {spread_entry_pct:+.3f}% (Positif). Total Fee Terhemat: ${total_fees:.4f} USDT.",
                pair_reputation_score=0.1
            )
        except Exception as agi_err:
            log.debug(f"AGI entry experience notice: {agi_err}")

        msg = (
            f"🚀 *[Delta-Neutral Terbuka (Full Maker)]*\n"
            f"Koin: `{base}`\n"
            f"Kuantitas: `{actual_qty}`\n"
            f"Spot Buy: `${spot_res.avg_price:,.4f}`\n"
            f"Perp Short: `${perp_res.avg_price:,.4f}`\n"
            f"Funding Rate: `{opportunity.current_funding_rate * 100:.4f}%/{opportunity.funding_interval_hours}h`\n"
            f"Est. Net APY: `{opportunity.net_apy_percent:.1f}%`\n"
            f"Delta Bersih: `{net_delta}`"
        )
        log.info(msg.replace("*", "").replace("`", ""))
        await notifier.send_message(msg)

        return position

    async def close_delta_neutral_position(
        self,
        position_id: str,
        reason: str = "MANUAL_OR_TARGET_REACHED"
    ) -> bool:
        """
        Menutup seluruh posisi secara sinkron (Tutup Perp Short + Jual Spot).
        """
        pos = self.pos_mgr.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            log.warning(f"Posisi {position_id} tidak ditemukan atau sudah tidak aktif.")
            return False

        log.info(f"[Tutup Posisi] Menutup posisi {position_id} ({pos.base_asset}). Alasan: {reason}")
        pos.status = "CLOSING"
        self.pos_mgr.update_position(pos)

        # 1. Tutup Posisi Short Futures terlebih dahulu (Beli kembali di perp)
        log.info(f"-> Menutup Short Perp {pos.perp_leg.symbol} sejumlah {pos.perp_leg.amount}...")
        perp_close_res = await self.client.execute_perp_order(
            symbol=pos.perp_leg.symbol,
            side="buy",
            amount=pos.perp_leg.amount,
            order_type="market",
            reduce_only=True
        )

        # 2. Jual Spot Token
        actual_spot_qty = pos.spot_leg.amount
        try:
            spot_bal = await self.client.client.fetch_balance({"type": "spot"})
            free_coin = float(spot_bal.get("free", {}).get(pos.base_asset, 0.0) or 0.0)
            if 0.0 < free_coin < actual_spot_qty:
                actual_spot_qty = free_coin
        except Exception:
            pass

        log.info(f"-> Menjual Spot {pos.spot_leg.symbol} sejumlah {actual_spot_qty}...")
        spot_close_res = await self.client.execute_spot_order(
            symbol=pos.spot_leg.symbol,
            side="sell",
            amount=actual_spot_qty,
            order_type="market"
        )

        # Simpan jejak order penutupan ke database
        try:
            db.record_order_execution(
                order_id=perp_close_res.order_id or f"perp-close-{uuid.uuid4().hex[:8]}",
                position_id=pos.position_id,
                market_type="perp",
                symbol=pos.perp_leg.symbol,
                side="buy_to_close",
                requested_amount=pos.perp_leg.amount,
                filled_amount=perp_close_res.filled_amount,
                price=perp_close_res.avg_price,
                fee_paid=perp_close_res.fee_amount,
                status="FILLED"
            )
            db.record_order_execution(
                order_id=spot_close_res.order_id or f"spot-sell-{uuid.uuid4().hex[:8]}",
                position_id=pos.position_id,
                market_type="spot",
                symbol=pos.spot_leg.symbol,
                side="sell",
                requested_amount=actual_spot_qty,
                filled_amount=spot_close_res.filled_amount,
                price=spot_close_res.avg_price,
                fee_paid=spot_close_res.fee_amount,
                status="FILLED"
            )
        except Exception as dbe:
            log.debug(f"[OrderExecutor] Gagal menyimpan order penutupan ke DB: {dbe}")

        # Update status
        pos.status = "CLOSED"
        pos.exit_reason = reason
        pos.closed_at = datetime.utcnow()
        exit_fees = perp_close_res.fee_amount + spot_close_res.fee_amount
        pos.total_fees_paid += exit_fees

        self.pos_mgr.update_position(pos)

        msg = (
            f"🛑 *[Delta-Neutral Ditutup]*\n"
            f"Koin: `{pos.base_asset}`\n"
            f"Alasan: `{reason}`\n"
            f"Total Fee Dibayar: `${pos.total_fees_paid:.4f}`\n"
            f"Total Funding Diperoleh: `${pos.cumulative_funding_received:.4f}`"
        )
        log.info(msg.replace("*", "").replace("`", ""))
        await notifier.send_message(msg)

        return True

order_executor = OrderExecutor()
