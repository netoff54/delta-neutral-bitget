# Panduan Deploy Bot Delta-Neutral ke Railway (24/7 Cloud)

Panduan lengkap untuk menjalankan bot di **Railway.app** agar berjalan 24 jam nonstop tanpa perlu menyalakan laptop.

---

## 1. Keuntungan Menjalankan di Railway

1. **Online 24/7 Nonstop**: Bot tetap berjalan memanen funding fee meskipun laptop Anda dimatikan, ditutup, atau kehabisan baterai.
2. **Koneksi Stabil & Low Latency**: Server Railway berada di data center global yang terhubung langsung ke Bitget tanpa gangguan pemblokiran DNS ISP lokal.
3. **Gratis / Hemat Biaya**: Railway menyediakan free trial credit setiap bulan yang cukup untuk menjalankan 1 container bot ringan.
4. **Notifikasi Otomatis**: Hasil panen fee dan rotasi posisi dapat dikirim langsung ke Telegram Anda.

---

## 2. Langkah-Langkah Deploy ke Railway

### Langkah A: Upload Kode ke GitHub (Private Repository)
1. Buka [GitHub.com](https://github.com) dan buat repository baru (pilih **Private** agar kode Anda aman).
2. Di laptop Anda (folder proyek `d:\delta neutral bitget`), jalankan perintah git di terminal:
   ```bash
   git init
   git add .
   git commit -m "Initial commit delta-neutral bot"
   git branch -M main
   git remote add origin https://github.com/USERNAME-ANDA/NAMA-REPO-ANDA.git
   git push -u origin main
   ```

---

### Langkah B: Buat Proyek di Railway
1. Buka [Railway.app](https://railway.app) dan login dengan akun GitHub Anda.
2. Klik tombol **"+ New Project"**.
3. Pilih **"Deploy from GitHub repo"**.
4. Pilih repository bot yang baru saja Anda buat.
5. Railway akan otomatis mendeteksi [`Dockerfile`](Dockerfile) dan mulai membangun container.

---

### Langkah C: Masukkan Environment Variables (API Keys)
Di dashboard Railway, klik service bot Anda, lalu buka tab **"Variables"** dan tambahkan variabel berikut:

| Nama Variabel | Contoh Nilai | Penjelasan |
| :--- | :--- | :--- |
| `BITGET_API_KEY` | `bg_xxxxxxxx` | API Key Bitget Anda |
| `BITGET_API_SECRET` | `xxxxxxxxxxxx` | API Secret Bitget Anda |
| `BITGET_API_PASSPHRASE` | `password_api` | Passphrase API Bitget Anda |
| `DRY_RUN` | `false` | `false` untuk Live Trading (Uang Asli), `true` untuk Simulasi |
| `USE_ALL_AVAILABLE_BALANCE` | `true` | Menggunakan seluruh modal trading cair di Bitget |
| `INCLUDE_OTC_BALANCE` | `true` | Mengikutsertakan saldo Akun OTC/Pendanaan |
| `PROTECT_BITGET_EARN` | `true` | **Wajib `true`**: Menjamin uang di Bitget Earn tidak disentuh |
| `LEVERAGE` | `2` | Leverage maksimal 2x |
| `TELEGRAM_ENABLED` | `false` | Set `true` jika ingin notifikasi ke Telegram |
| `TELEGRAM_BOT_TOKEN` | `123456:ABC...` | Token Bot Telegram Anda (opsional) |
| `TELEGRAM_CHAT_ID` | `987654321` | Chat ID Telegram Anda (opsional) |

---

### Langkah D: Tambahkan Persistent Volume (Penting!)
Agar riwayat posisi (`positions.json`) dan akumulasi profit compounding (`compounding_state.json`) tidak hilang saat container restart:
1. Di dashboard service Railway Anda, klik tab **"Settings"** atau klik kanan service $\rightarrow$ pilih **"Add Volume"**.
2. Beri nama volume (misal: `data-storage`).
3. Set **Mount Path**:
   ```text
   /app/data
   ```
4. Klik **Save**.

---

## 3. Memantau Bot

* **Live Logs**: Klik tab **"Logs"** di Railway untuk melihat tabel scan pasar, funding rate real-time, dan status eksekusi secara langsung.
* **Restart / Stop**: Anda bisa mematikan bot kapan saja hanya dengan menekan tombol **Pause** atau **Delete Service** di dashboard Railway tanpa perlu membuka laptop.
