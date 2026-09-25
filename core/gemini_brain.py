import os
import json
import asyncio
import aiohttp
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from config.settings import settings
from utils.logger import log
from core.models import DeltaNeutralPosition, Opportunity
from core.historical_store import historical_store

class GeminiBrain:
    """
    Artificial General Intelligence (AGI) Brain untuk Arbitrase Delta-Neutral Bitget.
    Ditenagai oleh Google Gemini API (gemini-3.6-flash).
    
    Fitur Utama:
    1. Continuous Learning & Knowledge Base:
       Setiap pembayaran funding fee dianalisis secara kualitatif & kuantitatif.
       Pembelajaran dan heuristik baru diakumulasikan ke data/ai_learning_memory.json
       dan disuntikkan ke prompt masa depan (Few-Shot Contextual Learning).
    2. Zero-Tolerance Negative Risk Guardian:
       Memantau fluktuasi rate menit-ke-menit dan pre-settlement 5 menit.
       Jika rate berbalik minus (< 0.0%), AI memerintahkan pemotongan posisi seketika.
    3. Intelligent Taker Selector:
       Memilih taker baru terbaik di pasar secara holistik berdasarkan konsistensi,
       skor prediksi, likuiditas, dan pengalaman masa lalu.
    4. Dynamic Minute-by-Minute Yield Projection:
       Memproyeksikan estimasi dividen funding fee berikutnya sesuai interval dinamis koin (1h/4h/8h).
    5. Real-Time Ground-Truth Decision Engine (50% Quota Allocation = 720 calls/hari):
       Mengevaluasi telemetri pasar riil (basis spread, drift, rate velocity) setiap 2 menit.
    """

    FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash"]

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        memory_file: str = "data/ai_learning_memory.json"
    ):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.primary_model = model or settings.GEMINI_MODEL or "gemini-3.6-flash"
        self.memory_path = Path(memory_file)
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory: List[Dict[str, Any]] = []
        self.latest_insight: str = "AI AGI Brain aktif, siap menganalisis pasar & siklus funding secara dinamis."
        self.daily_calls_count: int = 0
        self.last_call_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.max_daily_calls: int = getattr(settings, "AI_MAX_DAILY_CALLS", 720)
        self.load_memory()

    def load_memory(self):
        """Memuat riwayat pembelajaran dan akumulasi pengalaman dari disk atau database."""
        loaded_from_disk = False
        if self.memory_path.exists():
            try:
                with open(self.memory_path, "r", encoding="utf-8") as f:
                    self.memory = json.load(f)
                if self.memory:
                    loaded_from_disk = True
                    log.info(f"[GeminiBrain] Berhasil memuat {len(self.memory)} pengalaman pembelajaran AGI dari disk.")
            except Exception as e:
                log.warning(f"[GeminiBrain] Gagal memuat memori AI dari disk: {e}")
                self.memory = []

        # Fallback ke database jika file di container cloud kosong (Render redeployment / restart)
        if not loaded_from_disk or not self.memory:
            try:
                from core.database import db
                db_mem = db.get_agentic_memory(limit=150)
                if db_mem:
                    self.memory = [
                        {
                            "timestamp": m.get("timestamp_utc"),
                            "event_type": m.get("event_type"),
                            "base_asset": m.get("base_asset"),
                            "current_funding_rate": m.get("funding_rate"),
                            "harvest_profit_usdt": m.get("harvest_usdt"),
                            "net_pnl_usdt": m.get("net_pnl_usdt"),
                            "holding_hours": m.get("holding_hours"),
                            "was_bep_reached": bool(m.get("was_bep_reached")),
                            "decision": m.get("ai_decision"),
                            "lesson_learned": m.get("lesson_learned"),
                            "tactical_rule": m.get("tactical_rule"),
                            "insight": m.get("lesson_learned") or m.get("ai_decision")
                        }
                        for m in reversed(db_mem)
                    ]
                    log.info(f"🧠 [GeminiBrain] Berhasil memulihkan {len(self.memory)} memori evolusi AGI langsung dari Database!")
            except Exception as dbe:
                log.debug(f"[GeminiBrain] Database memory recovery notice: {dbe}")

        if self.memory:
            last = self.memory[-1]
            self.latest_insight = last.get("insight") or last.get("lesson_learned") or self.latest_insight

    def save_memory(self):
        """Menyimpan akumulasi pengalaman AGI ke disk."""
        try:
            trimmed = self.memory[-150:] # Simpan hingga 150 entri memori historis
            with open(self.memory_path, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, indent=2, default=str)
        except Exception as e:
            log.error(f"[GeminiBrain] Gagal menyimpan memori AI: {e}")

    def get_accumulated_knowledge_context(self) -> str:
        """
        Merangkum pengalaman masa lalu & indeks reputasi koin dari SQLite menjadi konteks Few-Shot.
        Ini memungkinkan AI mengingat performa koin-koin sebelumnya secara akurat.
        """
        try:
            reputations = historical_store.get_all_pair_reputations()
        except Exception:
            reputations = {}

        rep_lines = []
        for coin, score in list(reputations.items())[:5]:
            status = "SANGAT BAIK" if score >= 0.7 else ("BAIK" if score >= 0.4 else "BERISIKO")
            rep_lines.append(f"- Reputasi {coin}: Skor {score:+.2f} ({status})")

        harvest_memories = [m for m in self.memory if m.get("event_type") == "HARVEST_LEARNING"][-5:]
        context_lines = []
        if rep_lines:
            context_lines.append("Indeks Reputasi Koin Berdasarkan Riwayat Nyata:")
            context_lines.extend(rep_lines)

        if harvest_memories:
            context_lines.append("\nRiwayat Pembelajaran & Panen Terkini:")
            for idx, m in enumerate(harvest_memories, 1):
                coin = m.get("base_asset", "?")
                rate = m.get("current_funding_rate", 0.0) * 100
                profit = m.get("harvest_profit_usdt", 0.0)
                pnl = m.get("net_pnl_usdt", 0.0)
                lesson = m.get("lesson_learned", m.get("insight", ""))[:120]
                context_lines.append(f"{idx}. [{coin}] Rate: {rate:+.4f}%, Profit: +${profit:.4f}, Net PnL: ${pnl:+.4f} | Catatan: {lesson}")

        # Tambahkan Analisis PnL Multi-Timeframe (1D, 1W, 1M, 1Y) Langsung dari Database
        try:
            from core.database import db
            all_pnl = db.get_all_pnl_timeframes()
            pnl_1d = all_pnl.get("1D", {})
            pnl_1w = all_pnl.get("1W", {})
            pnl_1m = all_pnl.get("1M", {})
            all_time = all_pnl.get("all_time", {})

            context_lines.append(
                f"\nPerforma PnL Portofolio Multi-Timeframe (Data Real Bitget):\n"
                f"- 1D: PnL ${pnl_1d.get('pnl_usdt', 0.0):+.4f} ({pnl_1d.get('pnl_pct', 0.0):+.2f}%) | Harvest: +${pnl_1d.get('funding_harvested_usdt', 0.0):.4f}\n"
                f"- 1W: PnL ${pnl_1w.get('pnl_usdt', 0.0):+.4f} ({pnl_1w.get('pnl_pct', 0.0):+.2f}%) | Harvest: +${pnl_1w.get('funding_harvested_usdt', 0.0):.4f}\n"
                f"- 1M: PnL ${pnl_1m.get('pnl_usdt', 0.0):+.4f} ({pnl_1m.get('pnl_pct', 0.0):+.2f}%)\n"
                f"- All-Time Total Funding Dipanen: +${all_time.get('total_funding_harvested_usdt', 0.0):.4f} USDT"
            )
        except Exception:
            pass

        # Tambahkan Telemetri Saldo Gabungan Multi-Market dari Database
        try:
            from core.database import db
            latest_bal = db.get_latest_combined_balance()
            growth_stats = db.get_equity_growth_stats(days=7)
            if latest_bal:
                context_lines.append(
                    f"\nStatus Saldo Gabungan Portofolio (Seluruh Pasar Bitget):\n"
                    f"- Total Kekayaan Bersih (Net Worth): ${latest_bal.get('total_combined_equity_usdt', 0.0):.2f} USDT\n"
                    f"  * Dompet Spot: ${latest_bal.get('spot_equity_usdt', 0.0):.2f} USDT\n"
                    f"  * Dompet Futures: ${latest_bal.get('futures_equity_usdt', 0.0):.2f} USDT (Margin: ${latest_bal.get('futures_margin_used_usdt', 0.0):.2f})\n"
                    f"  * Dompet OTC: ${latest_bal.get('otc_funding_equity_usdt', 0.0):.2f} USDT | Earn: 100% Aman Terlindungi\n"
                    f"- Stabilitas Delta-Neutral 7D: {growth_stats.get('stability_rating', 'STABLE')} "
                    f"(Perubahan Net Worth: {growth_stats.get('growth_pct', 0.0):+.2f}% / ${growth_stats.get('net_change_usdt', 0.0):+.4f} USDT)"
                )
        except Exception:
            pass

        # Prinsip Eksekusi Kuantitatif Profesional
        context_lines.append(
            f"\nPrinsip Utama Delta-Neutral Profesional:\n"
            f"- Eksekusi: Full Maker (Limit Post-Only) untuk fee termurah & basis spread selalu positif (perp >= spot).\n"
            f"- Status BEP: Wajib menutup lunas 100% dari 4 biaya (Spot Beli + Jual, Futures Buka + Tutup).\n"
            f"- Rotasi: Wajib surplus minimal 5% nilai BEP (< 1 bulan) dan WAJIB minimal 1% (>= 1 bulan) + 6 Syarat Ketat."
        )

        return "\n".join(context_lines) if context_lines else "Belum ada riwayat pembelajaran sebelumnya. Ini adalah siklus awal."

    def get_quota_status(self) -> Dict[str, Any]:
        """Mengembalikan status kuota dan alokasi 50% kapasitas harian Gemini AGI."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_call_date:
            self.daily_calls_count = 0
            self.last_call_date = today
        return {
            "calls_today": self.daily_calls_count,
            "max_daily_calls": self.max_daily_calls,
            "official_rpd": 1500,
            "budget_percent": round((self.daily_calls_count / max(1, self.max_daily_calls)) * 48.0, 1)
        }

    async def call_gemini(self, prompt: str, system_instruction: Optional[str] = None) -> Optional[str]:
        """Mengirim permintaan ke Google Gemini API dengan mekanisme fallback model dan batasan 50% kuota."""
        if not self.api_key:
            log.debug("[GeminiBrain] API key Gemini belum terpasang.")
            return None

        # Reset dan periksa kuota harian (maksimal 720 panggilan/hari = 48% dari limit 1.500 RPD)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_call_date:
            self.daily_calls_count = 0
            self.last_call_date = today

        if self.daily_calls_count >= self.max_daily_calls:
            log.warning(
                f"[GeminiBrain] Batas anggaran 50% kuota harian ({self.max_daily_calls} calls/hari) "
                f"telah tercapai ({self.daily_calls_count}/{self.max_daily_calls}). Menahan panggilan agar hemat selamanya."
            )
            return None

        self.daily_calls_count += 1

        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 400
            }
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        models_to_try = [self.primary_model] + [m for m in self.FALLBACK_MODELS if m != self.primary_model]

        async with aiohttp.ClientSession() as session:
            for model in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
                try:
                    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates and "content" in candidates[0]:
                                return candidates[0]["content"]["parts"][0]["text"].strip()
                        else:
                            err_text = await resp.text()
                            log.debug(f"[GeminiBrain] Model {model} status {resp.status}: {err_text[:120]}")
                except Exception as e:
                    log.debug(f"[GeminiBrain] Error calling model {model}: {e}")

        return None

    async def evaluate_harvest_and_learn(
        self,
        pos: DeltaNeutralPosition,
        harvest_amount_usdt: float,
        current_rate: float
    ) -> Dict[str, Any]:
        """
        Pembelajaran Dinamis Mandiri:
        Dipanggil setiap kali pembayaran funding fee cair dari exchange (jam dinamis 1h/4h/8h).
        Gemini merefleksikan hasil panen, merumuskan aturan baru, dan menyimpannya ke memori.
        """
        now = datetime.utcnow()
        holding_hours = (now - pos.entry_time).total_seconds() / 3600.0
        interval_hours = getattr(pos, "funding_interval_hours", 8) or 8

        knowledge_context = self.get_accumulated_knowledge_context()

        prompt = (
            f"Anda adalah Artificial General Intelligence (AGI) Manajer Portofolio Delta-Neutral Bitget.\n\n"
            f"{knowledge_context}\n\n"
            f"Evaluasi Pembayaran Funding Terkini:\n"
            f"- Aset Taker: {pos.base_asset} (Interval Pembayaran: {interval_hours} jam sekali)\n"
            f"- Nominal Kaki: ${pos.perp_leg.nominal_usdt:.2f} USDT (Leverage {pos.leverage}x)\n"
            f"- Funding Rate yang Cair: {current_rate * 100:+.4f}% / {interval_hours}h\n"
            f"- Pemasukan Riil Buku Besar: +${harvest_amount_usdt:.5f} USDT\n"
            f"- Akumulasi Total Funding Diterima: ${pos.cumulative_funding_received:.4f} USDT\n"
            f"- Total Biaya Transaksi Round-Trip: ${pos.total_fees_paid:.4f} USDT\n"
            f"- Live Unrealized PnL: ${pos.unrealized_pnl_usdt:+.4f} USDT\n"
            f"- Net PnL Bersih: ${pos.net_pnl_usdt:+.4f} USDT\n"
            f"- Status BEP: {'SUDAH BEP (PROFIT)' if pos.is_bep_reached else 'BELUM BEP (MENUJU IMPAS)'}\n"
            f"- Durasi Holding: {holding_hours:.1f} jam\n\n"
            f"Tugas Pembelajaran AGI:\n"
            f"1. Refleksi & Insight: Analisis apakah yield koin ini konsisten memuaskan.\n"
            f"2. Pelajaran Strategis (Key Lesson): 1 aturan heuristik baru yang dipelajari dari data ini.\n"
            f"3. Rekomendasi Tindakan: (HOLD_PANEN / PERSIAPKAN_ROTASI / WASPADA_VOLATILITAS)."
        )

        sys_inst = (
            "Anda adalah AI kuantitatif profesional arbitrase delta-neutral. "
            "Gunakan penalaran matematis tajam, protektif terhadap modal, dan berikan evaluasi padat dalam bahasa Indonesia."
        )
        ai_response = await self.call_gemini(prompt, system_instruction=sys_inst)

        if not ai_response:
            ai_response = (
                f"Rekomendasi: HOLD_PANEN. Pembayaran funding +${harvest_amount_usdt:.4f} USDT berjalan konsisten. "
                f"Net PnL ${pos.net_pnl_usdt:+.4f}. Pertahankan posisi selama rate di atas 0.0%."
            )

        self.latest_insight = ai_response
        memory_entry = {
            "timestamp": now.isoformat(),
            "event_type": "HARVEST_LEARNING",
            "base_asset": pos.base_asset,
            "interval_hours": interval_hours,
            "harvest_profit_usdt": harvest_amount_usdt,
            "current_funding_rate": current_rate,
            "net_pnl_usdt": pos.net_pnl_usdt,
            "is_bep_reached": pos.is_bep_reached,
            "holding_hours": round(holding_hours, 2),
            "lesson_learned": ai_response,
            "insight": ai_response
        }
        self.memory.append(memory_entry)
        self.save_memory()

        # Rekam ke database SQLite AGI Experience & Pair Reputation
        try:
            historical_store.record_agi_experience(
                event_type="HARVEST",
                base_asset=pos.base_asset,
                funding_rate=current_rate,
                harvest_usdt=harvest_amount_usdt,
                net_pnl_usdt=pos.net_pnl_usdt,
                holding_hours=holding_hours,
                was_bep_reached=pos.is_bep_reached,
                ai_decision="HOLD_PANEN",
                lesson_learned=ai_response,
                tactical_rule=ai_response[:100],
                pair_reputation_score=0.1
            )
        except Exception as e:
            log.debug(f"Gagal rekam AGI experience ke SQLite: {e}")

        log.info(f"🧠 [Gemini AGI Continuous Learning]:\n{ai_response}")
        return memory_entry

    async def select_optimal_taker_agi(
        self,
        opportunities: List[Opportunity],
        capital_usdt: float
    ) -> Optional[Opportunity]:
        """
        Seleksi Taker Cerdas AGI:
        Membandingkan peluang teratas pasar Bitget dengan memadukan skor kuantitatif,
        analisis historis 7 hari, biaya amortisasi taker, dan memori reputasi masa lalu.
        """
        eligible = [o for o in opportunities if o.is_eligible]
        if not eligible:
            return None

        if len(eligible) == 1:
            return eligible[0]

        top_candidates = eligible[:5]
        knowledge_context = self.get_accumulated_knowledge_context()

        candidates_summary = []
        for o in top_candidates:
            h120d_info = ""
            stats = o.historical_120d_stats or o.historical_60d_stats or o.historical_30d_stats or o.historical_7d_stats
            if stats:
                h120d_info = (
                    f" | 120D Yield: {stats.one_twenty_day_cumulative_yield_pct:+.2f}% (120D APY: {stats.one_twenty_day_apy_pct:.1f}%) | "
                    f"120D Flips: {stats.one_twenty_day_flip_count}x | 60D Yield: {stats.sixty_day_cumulative_yield_pct:+.2f}% | "
                    f"30D Yield: {stats.thirty_day_cumulative_yield_pct:+.2f}% | 7D Yield: {stats.seven_day_cumulative_yield_pct:+.2f}% | "
                    f"Taker Impas: {stats.taker_fee_recovery_hours:.1f}h | Skor Historis: {stats.historical_quality_score:.1f}"
                )
            
            try:
                rep = historical_store.get_pair_reputation(o.base_asset)
                rep_str = f" | Reputasi AI: {rep['reputation_score']:+.2f}"
            except Exception:
                rep_str = ""

            candidates_summary.append(
                f"- {o.base_asset}: Rate {o.current_funding_rate*100:.4f}%/{o.funding_interval_hours}h | "
                f"Net APY: {o.net_apy_percent:.1f}% | Prediksi Next: {o.predicted_next_funding_rate*100:.4f}% | "
                f"Konsistensi: {o.consistency_score_percent:.0f}% | Tren: {o.funding_trend} | "
                f"Vol Perp: ${o.perp_volume_24h/1e6:.1f}M"
                f"{h120d_info}{rep_str}"
            )

        prompt = (
            f"Anda adalah Chief Investment Officer (CIO) Delta-Neutral Quantitative Hedge Fund Bitget.\n\n"
            f"{knowledge_context}\n\n"
            f"Modal Tersedia: ${capital_usdt:.2f} USDT (Leverage {settings.LEVERAGE}x)\n"
            f"Kandidat Pasar Teratas (Dilengkapi Analisis Data Asli Bitget 120 Hari (4 Bulan) & Biaya Taker):\n"
            + "\n".join(candidates_summary) + "\n\n"
            f"Prinsip Keputusan Delta-Neutral Profesional:\n"
            f"1. Utamakan koin dengan 120-Day (4 Bulan) Cumulative Yield positif tinggi dan Zero/Minimal Flip (<= 3x rate negatif dalam 120 hari).\n"
            f"2. Perhatikan interval waktu pembayaran funding fee (1 jam, 4 jam, atau 8 jam). Koin dengan interval 1h atau 4h memberikan frekuensi panen dividen lebih sering per hari (24x/hari untuk 1h, 6x/hari untuk 4h) sehingga mempercepat tercapainya impas biaya taker.\n"
            f"3. Pastikan biaya taker (spot + perp) cepat terbayar dari dividen funding (Taker Impas < 48 jam).\n"
            f"4. Hindari koin dengan tren Decay tajam atau riwayat reputasi masa lalu yang buruk.\n\n"
            f"Tugas: Tentukan SATU koin terbaik yang paling konsisten positif, aman dari risiko flip, dan berdaya hasil tinggi.\n"
            f"Format jawaban: 'PILIH: [KOIN]' diikuti alasan 1 kalimat berbasis data historis 120 hari & biaya taker."
        )

        ai_response = await self.call_gemini(prompt)
        if ai_response:
            for o in top_candidates:
                if f"PILIH: {o.base_asset}" in ai_response.upper() or o.base_asset in ai_response.upper():
                    log.info(f"🎯 [Gemini AGI Taker Selection]: Memilih {o.base_asset} -> {ai_response}")
                    return o

        # Fallback ke ranking kuantitatif tertinggi jika API tidak menyebutkan koin spesifik
        return top_candidates[0]

    def generate_funding_projection(
        self,
        pos: DeltaNeutralPosition,
        current_rate: float,
        interval_hours: int = 8
    ) -> Dict[str, Any]:
        """
        Menghitung proyeksi pendapatan funding fee dinamis menit-ke-menit
        berdasarkan rate pasar teraktual dan interval pembayaran koin.
        """
        nominal_short = pos.perp_leg.nominal_usdt
        est_payout_next = round(nominal_short * current_rate, 5)
        cycles_per_day = 24.0 / max(1, interval_hours)
        est_daily_usdt = round(est_payout_next * cycles_per_day, 4)
        projected_apy = round(current_rate * cycles_per_day * 365.0 * 100.0, 1)

        return {
            "base_asset": pos.base_asset,
            "interval_hours": interval_hours,
            "current_rate_percent": round(current_rate * 100, 4),
            "projected_next_payout_usdt": est_payout_next,
            "projected_daily_usdt": est_daily_usdt,
            "projected_annual_apy_percent": projected_apy,
            "is_rate_healthy": current_rate > 0.0
        }

    async def evaluate_pre_settlement_risk(
        self,
        pos: DeltaNeutralPosition,
        projected_rate: float,
        seconds_until_settlement: float
    ) -> Dict[str, Any]:
        """
        Zero-Tolerance Pre-Settlement Check (5 menit sebelum jam settlement).
        Jika rate < 0.0, AI segera memicu penutupan darurat.
        """
        minutes_left = round(seconds_until_settlement / 60.0, 1)
        # ZERO TOLERANCE: Jika di bawah atau sama dengan 0.0%, langsung bahaya!
        is_negative = projected_rate < 0.0

        if not is_negative and projected_rate > 0:
            return {
                "action": "SAFE",
                "risk_level": "LOW",
                "message": f"Funding rate aman ({projected_rate * 100:+.4f}% dalam {minutes_left}m)."
            }

        prompt = (
            f"PERINGATAN ZERO-TOLERANCE NEGATIVE FUNDING (PRA-SETTLEMENT):\n"
            f"Koin: {pos.base_asset}\n"
            f"Waktu settlement: {minutes_left} menit lagi.\n"
            f"Projected Funding Rate: {projected_rate * 100:+.4f}%\n"
            f"Ambang Batas: 0.0% (Zero tolerance: rate negatif dilarang keras)\n"
            f"Apakah posisi harus langsung ditutup seketika untuk mencegah pemotongan saldo? Jawab: 'TUTUP_SEKARANG'."
        )

        ai_response = await self.call_gemini(prompt)
        return {
            "action": "EMERGENCY_EXIT",
            "risk_level": "CRITICAL",
            "message": ai_response or f"Funding rate negatif ({projected_rate*100:+.4f}% < 0.0%). Exit darurat zero-tolerance!"
        }

    async def evaluate_rotation_candidate(
        self,
        current_pos: DeltaNeutralPosition,
        candidate_opp: Opportunity
    ) -> bool:
        """Konfirmasi kualitatif AI sebelum rotasi modal."""
        if not current_pos.is_bep_reached and current_pos.net_pnl_usdt <= 0:
            log.info(f"[GeminiBrain] Menolak rotasi: {current_pos.base_asset} belum mencapai BEP.")
            return False

        knowledge_context = self.get_accumulated_knowledge_context()
        holding_hours = (datetime.utcnow() - current_pos.entry_time).total_seconds() / 3600.0
        days_held = holding_hours / 24.0

        spot_capital = getattr(current_pos.spot_leg, "nominal_usdt", 0.0) or (current_pos.spot_leg.entry_price * current_pos.spot_leg.amount)
        perp_capital = (getattr(current_pos.perp_leg, "nominal_usdt", 0.0) or (current_pos.perp_leg.entry_price * current_pos.perp_leg.amount)) / max(1, getattr(current_pos, "leverage", 1))
        bep_capital = spot_capital + perp_capital
        surplus_pct = (current_pos.net_pnl_usdt / bep_capital * 100.0) if bep_capital > 0 else 0.0

        prompt = (
            f"Anda adalah Chief Investment Officer (CIO) Delta-Neutral Quantitative AGI.\n\n"
            f"{knowledge_context}\n\n"
            f"EVALUASI ROTASI PELUANG:\n"
            f"Posisi Lama: {current_pos.base_asset}\n"
            f"- Modal BEP Posisi: ${bep_capital:.2f} USDT\n"
            f"- Durasi Holding: {days_held:.1f} hari ({holding_hours:.1f} jam)\n"
            f"- Net PnL di Atas BEP: +${current_pos.net_pnl_usdt:.4f} USDT ({surplus_pct:+.2f}% dari modal BEP)\n"
            f"- Status Tenggat 1 Bulan: {'LEWAT 1 BULAN (Syarat Minimal Surplus 1%)' if days_held >= 30.0 else 'DALAM 1 BULAN (Target Surplus 5%)'}\n\n"
            f"Kandidat Baru: {candidate_opp.base_asset}\n"
            f"- Net APY: {candidate_opp.net_apy_percent:.1f}% (Prediksi Rate: {candidate_opp.predicted_next_funding_rate*100:+.4f}%/{candidate_opp.funding_interval_hours}h)\n"
            f"- Konsistensi Rate: {candidate_opp.consistency_score_percent:.0f}% | Tren: {candidate_opp.funding_trend}\n"
            f"- Estimasi BEP Baru: {candidate_opp.break_even_hours:.1f} jam\n\n"
            f"Aturan Rotasi:\n"
            f"1. Wajib BEP telah menutup 100% dari 4 biaya (Spot Beli + Jual, Futures Buka + Tutup).\n"
            f"2. Surplus modal BEP: Wajib >= 5% jika < 1 bulan, dan WAJIB >= 1% jika >= 1 bulan.\n"
            f"3. Peluang baru harus memiliki APY jauh lebih tinggi dan BEP cepat.\n\n"
            f"Tugas: Apakah rotasi disetujui? Jawab 'SETUJUI' atau 'TOLAK' diikuti alasan analitis 1 kalimat."
        )

        ai_res = await self.call_gemini(prompt)
        if not ai_res:
            return True
        approved = "SETUJUI" in ai_res.upper()
        log.info(f"🧠 [Gemini AGI - Keputusan Rotasi]: {ai_res}")
        return approved

    async def evaluate_realtime_ground_truth(
        self,
        active_positions: List[DeltaNeutralPosition],
        total_balance_usdt: float,
        spot_free_usdt: float,
        swap_free_usdt: float,
        top_opportunities: List[Opportunity],
        market_countdown_minutes: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Evaluasi telemetri pasar real-time setiap 2 menit menggunakan model Gemini (Alokasi 50% kuota).
        Membaca data riil di lapangan (spread basis, drift harga spot vs perp, rate velocity, countdown)
        dan menghasilkan keputusan taktis adaptif.
        """
        if not active_positions:
            top_info = []
            for o in top_opportunities[:3]:
                vol_24h = getattr(o, "volume_24h_usdt", o.spot_volume_24h + o.perp_volume_24h)
                top_info.append(
                    f"- {o.base_asset}: Rate {o.current_funding_rate*100:+.4f}%/{o.funding_interval_hours}h, "
                    f"Net APY: {o.net_apy_percent:.1f}%, Vol24h: ${vol_24h:,.0f}"
                )
            market_summary = "\n".join(top_info) if top_info else "Tidak ada kandidat memenuhi syarat saat ini."

            prompt = (
                f"TELEMETRI PASAR REAL-TIME (TIDAK ADA POSISI AKTIF):\n"
                f"Saldo Cair: Spot ${spot_free_usdt:.2f}, Futures ${swap_free_usdt:.2f}, Total: ${total_balance_usdt:.2f} USDT.\n"
                f"Kandidat Peluang Teratas:\n{market_summary}\n\n"
                f"Berikan arahan strategis dalam format JSON murni:\n"
                f'{{"action": "OBSERVE_MARKET", "tactical_guidance": "penjelasan 1 kalimat", "market_condition": "NORMAL", "confidence": 0.95}}'
            )
            raw = await self.call_gemini(prompt)
            if raw:
                try:
                    clean = raw.strip().replace("```json", "").replace("```", "").strip()
                    res = json.loads(clean)
                    if "tactical_guidance" in res:
                        self.latest_insight = res["tactical_guidance"]
                    return res
                except Exception:
                    self.latest_insight = raw[:120]
            return {
                "action": "OBSERVE_MARKET",
                "tactical_guidance": "Memindai pasar real-time tiap 2 menit untuk peluang konsisten.",
                "market_condition": "NORMAL",
                "confidence": 1.0
            }

        # Ada posisi aktif: bangun telemetri data lapangan lengkap
        pos = active_positions[0]
        spot_drift = ((pos.spot_leg.current_price - pos.spot_leg.entry_price) / max(1e-6, pos.spot_leg.entry_price)) * 100
        perp_drift = ((pos.perp_leg.current_price - pos.perp_leg.entry_price) / max(1e-6, pos.perp_leg.entry_price)) * 100
        basis_spread = spot_drift - perp_drift

        try:
            rep = historical_store.get_pair_reputation(pos.base_asset)
            rep_text = f"Reputasi AI: {rep['reputation_score']:+.2f} (Panen Sukses: {rep['successful_harvests']}x, Exit Darurat: {rep['emergency_exits']}x)"
        except Exception:
            rep_text = "Reputasi AI: Netral"

        top_alts = []
        for o in top_opportunities[:3]:
            if o.base_asset != pos.base_asset:
                top_alts.append(
                    f"{o.base_asset} (Rate: {o.current_funding_rate*100:+.4f}%/{o.funding_interval_hours}h, "
                    f"Net APY: {o.net_apy_percent:.1f}%)"
                )
        alts_str = ", ".join(top_alts) if top_alts else "Tidak ada alternatif lebih baik"
        memory_ctx = self.get_accumulated_knowledge_context()

        prompt = (
            f"TELEMETRI REAL-TIME DATA LAPANGAN (BITGET DELTA-NEUTRAL):\n"
            f"1. Posisi Aktif: {pos.base_asset} (Leverage: {pos.leverage}x)\n"
            f"   - Spot: Masuk ${pos.spot_leg.entry_price:,.4f} -> Riil ${pos.spot_leg.current_price:,.4f} (Drift: {spot_drift:+.2f}%)\n"
            f"   - Perp Short: Masuk ${pos.perp_leg.entry_price:,.4f} -> Riil ${pos.perp_leg.current_price:,.4f} (Drift: {perp_drift:+.2f}%)\n"
            f"   - Basis Spread Divergence: {basis_spread:+.2f}%\n"
            f"   - Funding Rate Terkini: {pos.last_funding_rate * 100:+.4f}%\n"
            f"   - Proyeksi Dividen Siklus Berikutnya: +${pos.projected_next_funding_payout:.5f} USDT\n"
            f"   - Akumulasi Funding Diterima: +${pos.cumulative_funding_received:.4f} USDT | Total Biaya: ${pos.total_fees_paid:.4f} USDT\n"
            f"   - Net PnL Riil: ${pos.net_pnl_usdt:+.4f} USDT ({'BEP TERCAPAI' if pos.is_bep_reached else 'MENUJU BEP'})\n"
            f"   - Margin Ratio: {pos.current_margin_ratio*100:.1f}% (Batas Bahaya: 75%)\n"
            f"   - Countdown Menuju Settlement: {market_countdown_minutes if market_countdown_minutes is not None else '?'} menit\n"
            f"   - Rekam Jejak Koin: {rep_text}\n"
            f"2. Pasar Alternatif: {alts_str}\n"
            f"3. Konteks Pengalaman Historis:\n{memory_ctx}\n\n"
            f"Instruksi: Sebagai Chief Risk Officer AI, tentukan keputusan taktis paling bijak saat ini. "
            f"Balas HANYA format JSON valid:\n"
            f'{{"action": "HOLD_AND_HARVEST" | "TIGHTEN_EXIT" | "PROACTIVE_REBALANCE" | "ROTATE" | "EMERGENCY_UNWIND", '
            f'"tactical_guidance": "alasan 1-2 kalimat berbasis data telemetri", '
            f'"market_condition": "OPTIMAL" | "VOLATILE" | "RATE_DECAYING", "confidence": 0.95}}'
        )

        raw = await self.call_gemini(
            prompt,
            system_instruction="Anda adalah AGI Portfolio Risk Manager Delta-Neutral di exchange Bitget. Analisis data lapangan dengan bijak untuk memaksimumkan yield dividen dan meminimalkan risiko."
        )

        decision = {
            "action": "HOLD_AND_HARVEST",
            "tactical_guidance": f"Mempertahankan posisi {pos.base_asset} untuk memanen dividen funding rate berikutnya.",
            "market_condition": "OPTIMAL",
            "confidence": 1.0
        }

        if raw:
            try:
                clean = raw.strip().replace("```json", "").replace("```", "").strip()
                parsed = json.loads(clean)
                decision.update(parsed)
                if "tactical_guidance" in decision:
                    self.latest_insight = decision["tactical_guidance"]

                if decision.get("action") not in ["HOLD_AND_HARVEST", "OBSERVE_MARKET"]:
                    self.memory.append({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event_type": "REALTIME_TACTICAL_DECISION",
                        "base_asset": pos.base_asset,
                        "decision": decision.get("action"),
                        "insight": decision.get("tactical_guidance")
                    })
                    self.save_memory()

                    try:
                        historical_store.record_agi_experience(
                            event_type=f"TACTICAL_{decision.get('action')}",
                            base_asset=pos.base_asset,
                            funding_rate=pos.last_funding_rate,
                            harvest_usdt=0.0,
                            net_pnl_usdt=pos.net_pnl_usdt,
                            holding_hours=(datetime.now(timezone.utc) - pos.entry_time.replace(tzinfo=timezone.utc) if pos.entry_time.tzinfo is None else pos.entry_time).total_seconds() / 3600.0,
                            was_bep_reached=pos.is_bep_reached,
                            ai_decision=decision.get("action"),
                            lesson_learned=decision.get("tactical_guidance"),
                            pair_reputation_score=-0.2 if decision.get("action") == "EMERGENCY_UNWIND" else 0.0
                        )
                    except Exception:
                        pass
            except Exception as e:
                log.debug(f"[GeminiBrain] Error parsing JSON ground truth: {e}")
                self.latest_insight = raw[:120]

        return decision

    def get_latest_insight(self) -> str:
        """Mengambil wawasan terkini untuk UI konsol & cloud dashboard."""
        return self.latest_insight

gemini_brain = GeminiBrain()
