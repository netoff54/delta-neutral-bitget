# Autonomous Delta-Neutral Funding Rate Harvesting Agent (Bitget)

Bot trading otonom berbasis Python untuk mengeksekusi strategi **Delta-Neutral Funding Rate Arbitrage** di exchange **Bitget**. Bot secara otomatis memindai peluang yield funding rate di seluruh pasar futures, menghitung rincian biaya transaksi (*round-trip fees*), mengukur estimasi waktu impas (*break-even*), mengeksekusi posisi Spot Long + Perp Short secara sinkron dengan $\Delta = 0$, serta memonitor rasio margin dan pembalikan funding rate secara mandiri.

---

## Fitur Utama

1. **Pemindai Pasar Real-Time (Scanner)**:
   * Memindai lebih dari 800 kontrak USDT-M Futures dan mencocokkannya dengan pasar Spot Bitget.
   * Memfilter koin berdasarkan likuiditas volume 24 jam (Spot & Futures).
   * Memeriksa stabilitas historis funding rate untuk menghindari lonjakan sesaat.

2. **Kalkulator Fee & Break-Even Presisi**:
   * Menghitung total *Round-Trip Fee (RTF)*: Fee Masuk Spot + Fee Masuk Perp + Fee Keluar Spot + Fee Keluar Perp + *Slippage Buffer*.
   * Mengukur jam impas (*Break-Even Hours*) untuk memastikan biaya transaksi dapat tertutup dalam rentang waktu yang wajar.
   * Menghitung *Net APY* bersih tahunan setelah amortisasi biaya trading.

3. **Eksekusi Atomik & Kalibrasi Presisi ($\Delta = 0$)**:
   * Menyelaraskan ukuran lot Spot dan Futures menggunakan *step size* pasar terkecil agar tidak ada sisa token yang tidak ter-hedge.
   * Proteksi **Auto-Unwind Darurat**: Jika kaki Perp gagal terbuka setelah Spot terisi, bot langsung menjual kembali Spot ke USDT untuk mencegah eksposur arah.

4. **Risk Guard Terintegrasi**:
   * **Margin Guard**: Memantau pergerakan harga pada kaki Short, memproyeksikan harga likuidasi, dan memberikan peringatan dini jika margin ratio $\ge 75\%$.
   * **Funding Guard**: Memantau setiap siklus funding rate. Jika funding rate berubah menjadi negatif, bot memicu *emergency exit* untuk mencegah kerugian.
   * **Anti-DNS Hijack**: Dilengkapi resolver cerdas untuk menembus pemblokiran ISP lokal tanpa perlu setup proxy manual.

---

## Struktur Proyek

```
delta neutral bitget/
├── config/
│   ├── settings.py           # Manajemen konfigurasi & environment variable
│   └── __init__.py
├── core/
│   ├── bitget_client.py      # Wrapper CCXT Async untuk Bitget (Spot & USDT-M Perp)
│   ├── models.py             # Data model Pydantic (Opportunity, Position, OrderResult)
│   └── __init__.py
├── analytics/
│   ├── fee_calculator.py     # Logika kalkulasi RTF, Break-Even, dan Net APY
│   └── __init__.py
├── scanner/
│   ├── opportunity_finder.py # Scanner otomatis pasar Spot vs Futures
│   └── __init__.py
├── execution/
│   ├── order_executor.py     # Eksekutor dual-leg sinkron Spot Buy + Perp Short
│   ├── position_manager.py   # State manager posisi aktif (persisten ke JSON)
│   └── __init__.py
├── risk/
│   ├── margin_guard.py       # Pengawas margin ratio & pencegah likuidasi
│   ├── funding_guard.py      # Pengawas funding rate negatif & pencatat yield
│   └── __init__.py
├── utils/
│   ├── dns_resolver.py       # DoH resolver untuk bypass ISP DNS block
│   ├── logger.py             # Logging Rich console & file
│   ├── notifier.py           # Notifikasi Telegram async
│   └── __init__.py
├── data/
│   └── positions.json        # Database lokal posisi aktif
├── main.py                   # CLI Dashboard & Autonomous Loop
├── requirements.txt          # Dependensi library Python
├── .env.example              # Template konfigurasi
└── README.md
```

---

## Panduan Penggunaan

### 1. Persiapan Environment
Pastikan dependensi telah terpasang:
```bash
python -m pip install -r requirements.txt
```

### 2. Konfigurasi API (.env)
Salin `.env.example` menjadi `.env`:
```bash
cp .env.example .env
```
Isi kredensial API Bitget Anda di dalam `.env`:
```env
BITGET_API_KEY=api_key_anda
BITGET_API_SECRET=api_secret_anda
BITGET_API_PASSPHRASE=passphrase_anda

# Set False jika siap menggunakan saldo riil
DRY_RUN=true

# Alokasi Modal
MAX_CAPITAL_PER_POSITION_USDT=100.0
TOTAL_MAX_CAPITAL_USDT=500.0
LEVERAGE=2
```

### 3. Menjalankan Bot

#### A. Mode Pemindaian Saja (Scan Only)
Melihat ranking peluang yield, kalkulasi fee, dan status kelayakan saat ini tanpa membuka posisi:
```bash
python main.py --scan-only
```

#### B. Mode Simulasi Otonom (Dry-Run Auto-Trade)
Menjalankan loop otonom dengan uang simulasi (paper trading). Bot akan memindai pasar secara berkala, mengeksekusi order simulasi saat ada peluang bagus, dan memonitor margin/funding:
```bash
python main.py --dry-run --auto-trade
```

#### C. Mode Live Trading (Uang Asli)
Mengeksekusi transaksi nyata di akun Bitget Anda:
```bash
python main.py --live --auto-trade --capital 100
```
*(Gunakan modal kecil terlebih dahulu untuk menguji alur kerja)*

---

## Parameter Manajemen Risiko

* **Batas Jam Impas (`MAX_BREAK_EVEN_HOURS`)**: Default `72.0` jam (3 hari). Peluang dengan waktu impas lebih dari 3 hari akan ditolak.
* **Minimal Net APY (`MIN_NET_APY_PERCENT`)**: Default `15.0%`. Bot menolak koin dengan yield bersih di bawah target ini.
* **Volume 24h Minimum (`MIN_24H_VOLUME_USDT`)**: Default `$1,000,000` di kedua pasar (Spot & Perp) untuk meminimalkan *slippage*.
* **Emergency Funding Exit (`EMERGENCY_EXIT_FUNDING_RATE`)**: Jika funding rate berbalik $\le -0.01\%$, posisi otomatis ditutup untuk menghentikan pembayaran biaya funding ke trader long.
