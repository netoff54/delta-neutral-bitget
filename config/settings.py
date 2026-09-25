from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional
import os

class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Bitget API Credentials
    BITGET_API_KEY: str = Field(default="", description="Bitget API Key")
    BITGET_API_SECRET: str = Field(default="", description="Bitget API Secret")
    BITGET_API_PASSPHRASE: str = Field(default="", description="Bitget API Passphrase")

    # Execution Mode - Default FALSE (gunakan data REAL dari Bitget, BUKAN simulasi)
    DRY_RUN: bool = Field(default=False, description="Dry-run simulation mode - SELALU False di produksi")

    # Capital Allocation & Account Balance Strategy
    USE_ALL_AVAILABLE_BALANCE: bool = Field(default=True, description="Gunakan seluruh saldo trading cair yang ada di Bitget")
    INCLUDE_OTC_BALANCE: bool = Field(default=True, description="Gunakan saldo Akun OTC/Pendanaan melalui transfer internal")
    MAX_CONCURRENT_POSITIONS: int = Field(default=1, ge=1, le=5, description="Jumlah maksimal pasangan aktif sekaligus (default 1 untuk alokasi maksimal)")
    LIQUID_SAFETY_BUFFER_PERCENT: float = Field(default=0.02, description="Buffer keamanan 2% agar saldo tidak minus karena potongan fee")
    PROTECT_BITGET_EARN: bool = Field(default=True, description="Proteksi mutlak dana di fitur Bitget Earn (Savings/Staking) agar tidak disentuh")
    # CATATAN: SIMULATED_* hanya untuk keperluan test unit, TIDAK digunakan saat DRY_RUN=False
    SIMULATED_BALANCE_USDT: float = Field(default=0.0, description="[TEST ONLY] Saldo simulasi trading - TIDAK digunakan saat live")
    SIMULATED_OTC_BALANCE_USDT: float = Field(default=0.0, description="[TEST ONLY] Saldo OTC simulasi - TIDAK digunakan saat live")
    MAX_CAPITAL_PER_POSITION_USDT: Optional[float] = Field(default=None, description="Manual override alokasi per posisi (None = alokasi dinamis seluruh modal cair)")
    # INITIAL_SEED_CAPITAL_USDT tidak lagi digunakan - modal selalu dari saldo REAL Bitget
    INITIAL_SEED_CAPITAL_USDT: float = Field(default=0.0, description="[DEPRECATED] Tidak lagi digunakan - modal dari saldo REAL Bitget")
    TOTAL_MAX_CAPITAL_USDT: Optional[float] = Field(default=None, description="Batas atas total modal (opsional)")
    LEVERAGE: int = Field(default=1, ge=1, le=2, description="Futures short leverage (Maksimal 1x)")
    MIN_24H_VOLUME_USDT: float = Field(default=100000.0, description="Minimum 24h volume for liquidity check ($100k)")

    # Fee Structure
    SPOT_TAKER_FEE: float = Field(default=0.001, description="Spot taker fee rate (0.1%)")
    SPOT_MAKER_FEE: float = Field(default=0.001, description="Spot maker fee rate (0.1%)")
    PERP_TAKER_FEE: float = Field(default=0.0006, description="Perp taker fee rate (0.06%)")
    PERP_MAKER_FEE: float = Field(default=0.0002, description="Perp maker fee rate (0.02%)")
    SLIPPAGE_BUFFER: float = Field(default=0.0005, description="Anticipated slippage buffer (0.05%)")

    # Strategy & Predictive Scoring
    HISTORICAL_ANALYSIS_DAYS: int = Field(default=120, description="Horizon analisis riwayat funding rate (4 bulan / 120 hari untuk keamanan maksimal)")
    MIN_NET_APY_PERCENT: float = Field(default=15.0, description="Minimum acceptable net APY (%)")
    MAX_BREAK_EVEN_HOURS: float = Field(default=72.0, description="Maximum hours allowed to break even")
    MIN_CONSECUTIVE_POSITIVE_FUNDING: int = Field(default=2, description="Consecutive positive funding cycles required")
    PREDICTIVE_HORIZON_CYCLES: int = Field(default=10, description="Jumlah siklus historis untuk evaluasi performa")

    # Opportunity Rotation & Rebalancing
    AUTO_REBALANCE_DELTA: bool = Field(default=True, description="Otomatis rebalance delta jika ada mismatch")
    AUTO_ROTATE_OPPORTUNITIES: bool = Field(default=True, description="Otomatis rotasi modal ke pasangan baru dengan performa lebih tinggi")
    MIN_ROTATION_APY_DIFF: float = Field(default=12.0, description="Minimal selisih Net APY (%) untuk memicu rotasi modal")
    MIN_HOLDING_HOURS_BEFORE_ROTATION: float = Field(default=16.0, description="Minimal jam holding sebelum boleh dirotasi")
    # Target profit surplus 5% dari modal portofolio/BEP sebelum 1 bulan, dan WAJIB 1% setelah 1 bulan
    MIN_PROFIT_SURPLUS_PERCENT: float = Field(default=0.05, description="Target profit surplus minimal di atas BEP (5% dari nilai BEP modal posisi) sebelum 1 bulan")
    MIN_PROFIT_SURPLUS_AFTER_DEADLINE_PERCENT: float = Field(default=0.01, description="Wajib surplus minimal di atas BEP (1% dari nilai BEP modal posisi) setelah melewati tenggat 1 bulan")
    MAX_HOLDING_DEADLINE_DAYS: float = Field(default=30.0, description="Tenggat waktu maksimal holding (1 bulan / 30 hari)")
    MIN_PROFIT_BEFORE_ROTATION_USDT: float = Field(default=0.5, description="[FALLBACK] Minimal profit nominal dalam USDT")

    # Full Maker (Limit Post-Only) & Positive Spread Controls
    USE_MAKER_ORDERS: bool = Field(default=True, description="Gunakan Full Maker (Limit Post-Only) untuk buka posisi agar fee murah & spread positif")
    REQUIRE_POSITIVE_SPREAD: bool = Field(default=True, description="Wajib basis spread positif (perp price >= spot price) saat buka posisi")

    # Yield Vault (Profit Protection - Jangan Sentuh Uang Hasil Earn)
    VAULT_LOCK_PROFITS: bool = Field(default=True, description="Kunci seluruh profit hasil earn agar tidak dipakai trading")

    # Risk Controls
    MARGIN_CALL_THRESHOLD: float = Field(default=0.80, description="Ambang batas peringatan margin ratio / MMR Bitget (80%)")
    AUTO_CLOSE_MARGIN_RATIO: float = Field(default=0.90, description="Ambang batas margin ratio / MMR Bitget untuk auto-close darurat (90%)")
    HARD_TAKE_PROFIT_PERCENT: float = Field(default=0.05, description="Target Hard Take Profit otomatis (% dari modal posisi)")
    HARD_STOP_LOSS_PERCENT: float = Field(default=0.05, description="Batas Hard Stop Loss otomatis (% dari modal posisi)")
    EMERGENCY_EXIT_FUNDING_RATE: float = Field(default=0.0, description="Zero tolerance untuk funding rate negatif (< 0.0% langsung exit)")
    EXIT_NEGATIVE_CYCLES_COUNT: int = Field(default=1, description="Negative funding cycle count to trigger exit")

    # Telegram Alerts
    TELEGRAM_ENABLED: bool = Field(default=False)
    TELEGRAM_BOT_TOKEN: Optional[str] = Field(default=None)
    TELEGRAM_CHAT_ID: Optional[str] = Field(default=None)

    # AI Adaptive Brain (Google Gemini)
    GEMINI_API_KEY: Optional[str] = Field(default=None, description="API Key Google Gemini")
    GEMINI_MODEL: str = Field(default="gemini-3.6-flash", description="Model Gemini untuk AI Brain")
    ENABLE_AI_BRAIN: bool = Field(default=True, description="Aktifkan AI Adaptive Brain untuk evaluasi posisi & rotasi")
    AI_CONTINUOUS_LEARNING: bool = Field(default=True, description="Simpan dan kembangkan memori pembelajaran AGI berkelanjutan")
    
    # Pre-settlement Funding Watcher
    PRE_SETTLEMENT_CHECK_MINUTES: int = Field(default=5, description="Jendela waktu pra-settlement (5 menit)")

    # Polling intervals (seconds)
    SCAN_INTERVAL_SECONDS: int = Field(default=120, description="Interval between opportunity scans (2 mins = 720 cycles/day = ~50% Gemini daily quota)")
    MONITOR_INTERVAL_SECONDS: int = Field(default=60, description="Interval between position health checks (1 min)")
    AI_MAX_DAILY_CALLS: int = Field(default=720, description="Maksimal panggilan AI harian (48% dari kuota 1500 RPD Google)")
    AI_REALTIME_EVALUATION: bool = Field(default=True, description="Evaluasi telemetri pasar real-time setiap siklus 2 menit")

    # PnL Multi-Timeframe Analytics
    PNL_TIMEFRAMES_DAYS: list = Field(default=[1, 7, 30, 60, 120, 365], description="Timeframe PnL dalam hari: [1D, 1W, 1M, 2M, 4M, 1Y]")

    # Database Configuration (SQLite default / PostgreSQL cloud persistence)
    DATABASE_URL: Optional[str] = Field(
        default=None,
        description="Database connection URL (None/sqlite://... for local SQLite, or postgresql://... for cloud PostgreSQL)"
    )

settings = BotSettings()
