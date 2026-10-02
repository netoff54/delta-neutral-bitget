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
    MAX_CONCURRENT_POSITIONS: int = Field(default=1, ge=1, le=1, description="Kunci mutlak 1 koin fokus agar modal trading terkonsentrasi maksimal")
    LIQUID_SAFETY_BUFFER_PERCENT: float = Field(default=0.02, description="Buffer keamanan 2% agar saldo tidak minus karena potongan fee")
    PROTECT_BITGET_EARN: bool = Field(default=True, description="Proteksi mutlak dana di fitur Bitget Earn (Savings/Staking) agar tidak disentuh")
    # CATATAN: SIMULATED_* hanya untuk keperluan test unit, TIDAK digunakan saat DRY_RUN=False
    SIMULATED_BALANCE_USDT: float = Field(default=0.0, description="[TEST ONLY] Saldo simulasi trading - TIDAK digunakan saat live")
    SIMULATED_OTC_BALANCE_USDT: float = Field(default=0.0, description="[TEST ONLY] Saldo OTC simulasi - TIDAK digunakan saat live")
    MAX_CAPITAL_PER_POSITION_USDT: Optional[float] = Field(default=None, description="Manual override alokasi per posisi (None = alokasi dinamis seluruh modal cair)")
    # Baseline Modal Awal: Ditetapkan $64.0 USDT (sesuai setoran user), akan otomatis menyesuaikan jika user menambah saldo
    INITIAL_SEED_CAPITAL_USDT: float = Field(default=64.0, description="Baseline modal awal pokok ($64.0 USDT default, auto-adjust saat ada deposit baru)")
    TOTAL_MAX_CAPITAL_USDT: Optional[float] = Field(default=None, description="Batas atas total modal (opsional)")
    LEVERAGE: int = Field(default=1, ge=1, le=1, description="Futures short leverage terkunci mutlak 1x")
    MIN_24H_VOLUME_USDT: float = Field(default=10000.0, description="Minimum 24h volume for liquidity check ($10k)")

    # Fee Structure
    SPOT_TAKER_FEE: float = Field(default=0.001, description="Spot taker fee rate (0.1%)")
    SPOT_MAKER_FEE: float = Field(default=0.001, description="Spot maker fee rate (0.1%)")
    PERP_TAKER_FEE: float = Field(default=0.0006, description="Perp taker fee rate (0.06%)")
    PERP_MAKER_FEE: float = Field(default=0.0002, description="Perp maker fee rate (0.02%)")
    SLIPPAGE_BUFFER: float = Field(default=0.0005, description="Anticipated slippage buffer (0.05%)")

    # Strategy & Predictive Scoring - Target 2.5% Bersih per Minggu
    TARGET_WEEKLY_NET_YIELD_PERCENT: float = Field(default=2.5, description="Target keuntungan bersih mingguan (2.5% net per minggu)")
    HISTORICAL_ANALYSIS_DAYS: int = Field(default=365, description="Horizon analisis riwayat funding rate (1 tahun / 365 hari, atau sejak awal koin jika koin baru)")
    MIN_NET_APY_PERCENT: float = Field(default=35.0, description="Minimum acceptable net APY (35% APY agar koin prospektif tidak terbuang)")
    MAX_BREAK_EVEN_HOURS: float = Field(default=72.0, description="Maximum hours allowed to break even")
    MIN_CONSECUTIVE_POSITIVE_FUNDING: int = Field(default=2, description="Consecutive positive funding cycles required")
    PREDICTIVE_HORIZON_CYCLES: int = Field(default=10, description="Jumlah siklus historis untuk evaluasi performa")

    # Opportunity Rotation & Rebalancing
    AUTO_REBALANCE_DELTA: bool = Field(default=True, description="Otomatis rebalance delta jika ada mismatch")
    AUTO_ROTATE_OPPORTUNITIES: bool = Field(default=True, description="Otomatis rotasi modal ke pasangan baru dengan performa lebih tinggi")
    MIN_ROTATION_APY_DIFF: float = Field(default=12.0, description="Minimal selisih Net APY (%) untuk memicu rotasi modal")
    MIN_HOLDING_HOURS_BEFORE_ROTATION: float = Field(default=4.0, description="Minimal jam holding sebelum boleh dirotasi (1 siklus 4h)")
    # Target profit surplus 0.3% di atas BEP modal posisi untuk kelincahan rotasi mingguan
    MIN_PROFIT_SURPLUS_PERCENT: float = Field(default=0.003, description="Target profit surplus minimal di atas BEP (0.3% dari nilai modal) untuk memicu rotasi")
    MIN_PROFIT_SURPLUS_AFTER_DEADLINE_PERCENT: float = Field(default=0.001, description="Wajib surplus minimal di atas BEP (0.1% dari nilai modal) setelah 1 minggu")
    MAX_HOLDING_DEADLINE_DAYS: float = Field(default=7.0, description="Tenggat waktu evaluasi holding mingguan (7 hari)")
    MIN_PROFIT_BEFORE_ROTATION_USDT: float = Field(default=0.1, description="[FALLBACK] Minimal profit nominal dalam USDT")

    # Full Maker (Limit Post-Only) & Positive Spread Controls
    USE_MAKER_ORDERS: bool = Field(default=True, description="Gunakan Full Maker (Limit Post-Only) untuk buka posisi agar fee murah & spread positif")
    REQUIRE_POSITIVE_SPREAD: bool = Field(default=True, description="Wajib basis spread positif (perp price >= spot price saat masuk, spot price >= perp price saat keluar)")

    # Yield Vault (Profit Protection - Jangan Sentuh Uang Hasil Earn)
    VAULT_LOCK_PROFITS: bool = Field(default=True, description="Kunci seluruh profit hasil earn agar tidak dipakai trading")

    # Monthly Take Profit Reserve (5% dari total saldo Bitget setiap 30 hari)
    MONTHLY_TP_ENABLED: bool = Field(default=True, description="Aktifkan fitur penyisihan Take Profit otomatis setiap 30 hari rolling")
    MONTHLY_TP_PERCENT: float = Field(default=0.05, description="Persentase total saldo Bitget yang disisihkan ke brankas setiap 30 hari (default: 5%)")
    MONTHLY_TP_CYCLE_DAYS: int = Field(default=30, description="Durasi siklus evaluasi Take Profit dalam hari (default: 30 hari rolling)")
    MONTHLY_TP_MIN_SURPLUS_PERCENT: float = Field(default=0.01, description="Modal pokok wajib surplus minimal 1% di atas baseline setelah TP dieksekusi (mencegah modal balik ke titik awal)")

    # Risk Controls (Maksimal ROE & MMR tidak boleh melewati 85%)
    MARGIN_CALL_THRESHOLD: float = Field(default=0.75, description="Ambang batas peringatan margin ratio / MMR Bitget (75%)")
    AUTO_CLOSE_MARGIN_RATIO: float = Field(default=0.85, description="Ambang batas margin ratio / MMR Bitget untuk auto-close darurat (85%)")
    FUTURES_MAX_LOSS_PERCENT: float = Field(default=0.85, description="Batas maksimal kerugian unrealized futures (-85% margin) untuk batalkan delta neutral")
    FUTURES_MAX_ROE_LOSS_PERCENT: float = Field(default=85.0, description="Batas maksimal kerugian ROE Futures Bitget (-85%) untuk membatalkan delta neutral")
    REQUIRE_POSITIVE_SPREAD: bool = Field(default=True, description="Pastikan basis spread tetap positif (Perp >= Spot di semua jenis transaksi)")
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
