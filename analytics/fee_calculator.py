import math
from typing import Dict, Any, Optional
from config.settings import settings
from core.models import FeeBreakdown

class FeeCalculator:
    """
    Kalkulator presisi untuk menghitung biaya transaksi, break-even, dan yield bersih
    pada strategi Delta-Neutral Funding Rate Arbitrage di Bitget.
    """

    def __init__(
        self,
        spot_taker_fee: float = settings.SPOT_TAKER_FEE,
        spot_maker_fee: float = settings.SPOT_MAKER_FEE,
        perp_taker_fee: float = settings.PERP_TAKER_FEE,
        perp_maker_fee: float = settings.PERP_MAKER_FEE,
        slippage_buffer: float = settings.SLIPPAGE_BUFFER
    ):
        self.spot_taker_fee = spot_taker_fee
        self.spot_maker_fee = spot_maker_fee
        self.perp_taker_fee = perp_taker_fee
        self.perp_maker_fee = perp_maker_fee
        self.slippage_buffer = slippage_buffer

    def calculate_fees(
        self,
        nominal_value_usdt: float,
        is_maker_entry: bool = False,
        is_maker_exit: bool = False
    ) -> FeeBreakdown:
        """
        Menghitung rincian biaya transaksi masuk dan keluar (round-trip).
        """
        # Entry fees
        spot_entry_rate = self.spot_maker_fee if is_maker_entry else self.spot_taker_fee
        perp_entry_rate = self.perp_maker_fee if is_maker_entry else self.perp_taker_fee
        spot_entry = nominal_value_usdt * spot_entry_rate
        perp_entry = nominal_value_usdt * perp_entry_rate

        # Exit fees
        spot_exit_rate = self.spot_maker_fee if is_maker_exit else self.spot_taker_fee
        perp_exit_rate = self.perp_maker_fee if is_maker_exit else self.perp_taker_fee
        spot_exit = nominal_value_usdt * spot_exit_rate
        perp_exit = nominal_value_usdt * perp_exit_rate

        # Slippage buffer (dihitung untuk 2 arah eksekusi spot + perp)
        slippage_cost = nominal_value_usdt * (self.slippage_buffer * 2)

        total_round_trip = spot_entry + perp_entry + spot_exit + perp_exit + slippage_cost
        total_percent = (total_round_trip / nominal_value_usdt) * 100.0

        return FeeBreakdown(
            spot_entry_fee=round(spot_entry, 4),
            perp_entry_fee=round(perp_entry, 4),
            spot_exit_fee=round(spot_exit, 4),
            perp_exit_fee=round(perp_exit, 4),
            slippage_buffer=round(slippage_cost, 4),
            total_round_trip_fee=round(total_round_trip, 4),
            total_fee_percent=round(total_percent, 4)
        )

    def evaluate_opportunity(
        self,
        funding_rate: float,
        nominal_value_usdt: float,
        funding_interval_hours: int = 8,
        basis_spread_percent: float = 0.0,
        is_maker: bool = False
    ) -> Dict[str, Any]:
        """
        Mengevaluasi apakah suatu peluang funding rate layak diambil
        berdasarkan estimasi waktu impas dan Net APY.
        """
        fee_breakdown = self.calculate_fees(
            nominal_value_usdt=nominal_value_usdt,
            is_maker_entry=is_maker,
            is_maker_exit=is_maker
        )

        cycles_per_day = 24.0 / funding_interval_hours
        cycles_per_year = cycles_per_day * 365.0

        gross_cycle_yield_pct = funding_rate * 100.0
        gross_apy_pct = funding_rate * cycles_per_year * 100.0

        # Jika funding rate <= 0, tidak ada peluang untuk short perps
        if funding_rate <= 0:
            return {
                "fee_breakdown": fee_breakdown,
                "gross_cycle_yield_percent": round(gross_cycle_yield_pct, 4),
                "gross_apy_percent": 0.0,
                "net_apy_percent": 0.0,
                "break_even_cycles": 9999,
                "break_even_hours": 99999.0,
                "is_eligible": False,
                "rejection_reason": "Funding rate bernilai negatif atau nol"
            }

        # Hitung siklus impas (Break-even cycles)
        # Menghitung berapa kali pembayaran funding rate untuk menutup total biaya round-trip
        fee_pct = fee_breakdown.total_fee_percent / 100.0
        
        # Keuntungan dari basis spread (jika perp lebih mahal dari spot, saat exit harga konvergen)
        effective_cost = max(0.0001, fee_pct - (basis_spread_percent / 100.0))
        
        break_even_cycles = math.ceil(effective_cost / funding_rate)
        break_even_hours = break_even_cycles * funding_interval_hours

        # Net APY dihitung dengan asumsi horizon holding 30 hari
        assumed_holding_days = 30
        total_cycles_in_holding = assumed_holding_days * cycles_per_day
        gross_return_in_holding = funding_rate * total_cycles_in_holding
        net_return_in_holding = gross_return_in_holding - fee_pct
        net_apy_pct = (net_return_in_holding / assumed_holding_days) * 365.0 * 100.0

        # Kriteria kelayakan
        is_eligible = True
        rejection_reason = None

        if break_even_hours > settings.MAX_BREAK_EVEN_HOURS:
            is_eligible = False
            rejection_reason = f"Waktu impas ({break_even_hours:.1f} jam) melebihi batas toleransi ({settings.MAX_BREAK_EVEN_HOURS} jam)"
        elif net_apy_pct < settings.MIN_NET_APY_PERCENT:
            is_eligible = False
            rejection_reason = f"Net APY ({net_apy_pct:.1f}%) di bawah batas minimum ({settings.MIN_NET_APY_PERCENT}%)"

        return {
            "fee_breakdown": fee_breakdown,
            "gross_cycle_yield_percent": round(gross_cycle_yield_pct, 4),
            "gross_apy_percent": round(gross_apy_pct, 2),
            "net_apy_percent": round(net_apy_pct, 2),
            "break_even_cycles": break_even_cycles,
            "break_even_hours": round(break_even_hours, 1),
            "is_eligible": is_eligible,
            "rejection_reason": rejection_reason
        }

fee_calculator = FeeCalculator()
