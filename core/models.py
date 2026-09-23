from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

class FeeBreakdown(BaseModel):
    spot_entry_fee: float = Field(..., description="Biaya beli spot")
    perp_entry_fee: float = Field(..., description="Biaya buka short futures")
    spot_exit_fee: float = Field(..., description="Estimasi biaya jual spot")
    perp_exit_fee: float = Field(..., description="Estimasi biaya tutup short futures")
    slippage_buffer: float = Field(..., description="Buffer estimasi slippage eksekusi")
    total_round_trip_fee: float = Field(..., description="Total biaya round trip dalam USDT")
    total_fee_percent: float = Field(..., description="Total biaya round trip dalam persentase modal (%)")

class Opportunity(BaseModel):
    base_asset: str = Field(..., description="Contoh: BTC, ETH, SOL")
    spot_symbol: str = Field(..., description="Contoh: BTC/USDT")
    perp_symbol: str = Field(..., description="Contoh: BTC/USDT:USDT")
    
    spot_price: float
    perp_price: float
    basis_spread_percent: float = Field(..., description="Selisih harga perp vs spot (%)")
    
    current_funding_rate: float = Field(..., description="Funding rate saat ini (desimal, misal 0.0005 = 0.05%)")
    next_funding_time: Optional[datetime] = None
    funding_interval_hours: int = 8
    historical_funding_rates: List[float] = Field(default_factory=list)
    
    spot_volume_24h: float
    perp_volume_24h: float
    
    fee_breakdown: FeeBreakdown
    gross_cycle_yield_percent: float = Field(..., description="Yield per siklus funding (%)")
    gross_apy_percent: float = Field(..., description="Gross APY tahunan (%)")
    net_apy_percent: float = Field(..., description="Net APY tahunan setelah dikurangi amortisasi fee (%)")
    
    # Predictive & Historical Performance Fields
    predicted_next_funding_rate: float = Field(default=0.0, description="Prediksi funding rate siklus berikutnya")
    historical_mean_rate: float = Field(default=0.0, description="Rata-rata funding rate 10 siklus terakhir")
    historical_std_rate: float = Field(default=0.0, description="Volatilitas/standar deviasi funding rate")
    consistency_score_percent: float = Field(default=100.0, description="Persentase siklus dengan funding rate positif (%)")
    funding_trend: str = Field(default="STABLE", description="'UP', 'DOWN', 'STABLE'")
    composite_performance_score: float = Field(default=0.0, description="Skor ranking komposit berbasis data")

    # 7-Day In-Depth Historical Telemetry & Taker Quality
    historical_7d_stats: Optional["HistoricalFundingStats"] = None

    break_even_cycles: int = Field(..., description="Jumlah siklus funding untuk balik modal fee")
    break_even_hours: float = Field(..., description="Jam yang dibutuhkan untuk mencapai break-even")
    
    is_eligible: bool = Field(default=False)
    rejection_reason: Optional[str] = None
    scanned_at: datetime = Field(default_factory=datetime.utcnow)

class HistoricalFundingStats(BaseModel):
    seven_day_cumulative_yield_pct: float = Field(default=0.0, description="Total akumulasi yield funding dalam 7 hari (%)")
    seven_day_apy_pct: float = Field(default=0.0, description="Annualized APY berdasarkan 7 hari (%)")
    positive_consistency_pct: float = Field(default=100.0, description="Persentase siklus positif 7 hari (%)")
    negative_flip_count: int = Field(default=0, description="Berapa kali rate <= 0% dalam 7 hari")
    rate_std_dev: float = Field(default=0.0, description="Standar deviasi / volatilitas rate 7 hari")
    decay_trend: str = Field(default="STABLE", description="'ACCELERATING', 'DECAYING', 'STABLE'")
    taker_fee_recovery_cycles: int = Field(default=1, description="Estimasi siklus rata-rata untuk menutup biaya taker")
    taker_fee_recovery_hours: float = Field(default=8.0, description="Estimasi jam untuk menutup biaya taker")
    historical_quality_score: float = Field(default=0.0, description="Skor kualitas kuantitatif komposit 7 hari (0-100)")
    sample_count: int = Field(default=0, description="Jumlah data settlement funding yang dianalisis")

class TakerSnapshot(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    base_asset: str
    spot_symbol: str
    perp_symbol: str
    spot_price: float
    perp_price: float
    basis_spread_percent: float
    current_funding_rate: float
    predicted_next_rate: float
    funding_interval_hours: int = 8
    spot_taker_fee_pct: float
    perp_taker_fee_pct: float
    round_trip_fee_pct: float
    break_even_cycles: int
    break_even_hours: float
    composite_score: float
    is_eligible: bool
    rejection_reason: Optional[str] = None

class PositionLeg(BaseModel):
    market_type: str = Field(..., description="'spot' atau 'perp'")
    symbol: str
    side: str = Field(..., description="'buy' atau 'sell'")
    amount: float = Field(..., description="Kuantitas base token")
    entry_price: float
    current_price: float
    nominal_usdt: float
    unrealized_pnl: float = 0.0
    fee_paid: float = 0.0

class DeltaNeutralPosition(BaseModel):
    position_id: str
    base_asset: str
    spot_leg: PositionLeg
    perp_leg: PositionLeg
    leverage: int = 2
    funding_interval_hours: int = 8
    
    entry_time: datetime = Field(default_factory=datetime.utcnow)
    net_delta: float = Field(..., description="Delta bersih (harus mendekati 0)")
    
    cumulative_funding_received: float = 0.0
    funding_payments_count: int = 0
    total_fees_paid: float = 0.0
    
    # Real-time PnL & BEP Tracking
    realized_funding_usdt: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    net_pnl_usdt: float = 0.0
    is_bep_reached: bool = False
    last_funding_rate: float = 0.0
    projected_next_funding_payout: float = 0.0
    
    current_margin_ratio: float = 0.0
    liquidation_price: Optional[float] = None
    
    status: str = Field(default="OPEN", description="'OPEN', 'REBALANCING', 'CLOSING', 'CLOSED'")
    exit_reason: Optional[str] = None
    closed_at: Optional[datetime] = None

class OrderExecutionResult(BaseModel):
    success: bool
    market_type: str
    symbol: str
    side: str
    requested_amount: float
    filled_amount: float = 0.0
    avg_price: float = 0.0
    fee_amount: float = 0.0
    fee_currency: str = "USDT"
    order_id: Optional[str] = None
    error_message: Optional[str] = None

class YieldVaultRecord(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    position_id: str
    base_asset: str
    amount_usdt: float
    funding_rate: float
    notes: str = "Harvested funding fee"

class YieldVaultSummary(BaseModel):
    total_harvested_usdt: float = 0.0
    locked_reserve_usdt: float = 0.0
    records_count: int = 0
    last_updated: datetime = Field(default_factory=datetime.utcnow)
