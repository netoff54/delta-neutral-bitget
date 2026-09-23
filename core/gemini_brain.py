import os
import json
import asyncio
import aiohttp
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

from config.settings import settings
from utils.logger import log
from core.models import DeltaNeutralPosition, Opportunity

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
    """

    FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-flash-latest", "gemini-3.7-flash"]

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
        self.load_memory()

    def load_memory(self):
        """Memuat riwayat pembelajaran dan akumulasi pengalaman dari disk."""
        if not self.memory_path.exists():
            self.memory = []
            return
        try:
            with open(self.memory_path, "r", encoding="utf-8") as f:
                self.memory = json.load(f)
            if self.memory:
                last = self.memory[-1]
                self.latest_insight = last.get("insight", self.latest_insight)
            log.info(f"[GeminiBrain] Berhasil memuat {len(self.memory)} pengalaman pembelajaran AGI dari disk.")
        except Exception as e:
            log.warning(f"[GeminiBrain] Gagal memuat memori AI: {e}")
            self.memory = []

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
        Merangkum pengalaman masa lalu menjadi konteks Few-Shot untuk prompt Gemini.
        Ini memungkinkan AI mengingat performa koin-koin sebelumnya.
        """
        if not self.memory:
            return "Belum ada riwayat pembelajaran sebelumnya. Ini adalah siklus awal."

        harvest_memories = [m for m in self.memory if m.get("event_type") == "HARVEST_LEARNING"][-5:]
        if not harvest_memories:
            return "Belum ada riwayat panen funding fee sebelumnya."

        context_lines = ["Riwayat Pembelajaran & Pengalaman Masa Lalu:"]
        for idx, m in enumerate(harvest_memories, 1):
            coin = m.get("base_asset", "?")
            rate = m.get("current_funding_rate", 0.0) * 100
            profit = m.get("harvest_profit_usdt", 0.0)
            pnl = m.get("net_pnl_usdt", 0.0)
            lesson = m.get("lesson_learned", m.get("insight", ""))[:120]
            context_lines.append(f"{idx}. [{coin}] Rate: {rate:+.4f}%, Profit: +${profit:.4f}, Net PnL: ${pnl:+.4f} | Catatan: {lesson}")

        return "\n".join(context_lines)

    async def call_gemini(self, prompt: str, system_instruction: Optional[str] = None) -> Optional[str]:
        """Mengirim permintaan ke Google Gemini API dengan mekanisme fallback model."""
        if not self.api_key:
            log.debug("[GeminiBrain] API key Gemini belum terpasang.")
            return None

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

        log.info(f"🧠 [Gemini AGI Continuous Learning]:\n{ai_response}")
        return memory_entry

    async def select_optimal_taker_agi(
        self,
        opportunities: List[Opportunity],
        capital_usdt: float
    ) -> Optional[Opportunity]:
        """
        Seleksi Taker Cerdas AGI:
        Membandingkan peluang teratas pasar Bitget dengan memadukan skor kuantitatif
        dan memori pengalaman masa lalu untuk memilih koin yang paling konsisten & aman.
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
            candidates_summary.append(
                f"- {o.base_asset}: Rate {o.current_funding_rate*100:.4f}%/{o.funding_interval_hours}h | "
                f"Net APY: {o.net_apy_percent:.1f}% | Prediksi Next: {o.predicted_next_funding_rate*100:.4f}% | "
                f"Konsistensi: {o.consistency_score_percent:.0f}% | Tren: {o.funding_trend} | "
                f"Vol Perp: ${o.perp_volume_24h/1e6:.1f}M"
            )

        prompt = (
            f"Anda adalah AGI Pengambil Keputusan Seleksi Koin Delta-Neutral.\n\n"
            f"{knowledge_context}\n\n"
            f"Modal Tersedia: ${capital_usdt:.2f} USDT (Leverage 2x)\n"
            f"Kandidat Pasar Teratas:\n" + "\n".join(candidates_summary) + "\n\n"
            f"Tugas: Tentukan SATU koin terbaik yang paling konsisten positif, memiliki likuiditas aman, "
            f"dan meminimalkan risiko pembalikan funding rate negatif.\n"
            f"Format jawaban: 'PILIH: [KOIN]' diikuti alasan 1 kalimat."
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

        prompt = (
            f"EVALUASI ROTASI PELUANG AGI:\n"
            f"Posisi Lama: {current_pos.base_asset} (Sudah BEP, Net PnL: ${current_pos.net_pnl_usdt:+.4f})\n"
            f"Kandidat Baru: {candidate_opp.base_asset} (Net APY: {candidate_opp.net_apy_percent:.1f}%, "
            f"Tren: {candidate_opp.funding_trend}, Konsistensi: {candidate_opp.consistency_score_percent:.0f}%)\n"
            f"Apakah rotasi disetujui? Jawab 'SETUJUI' atau 'TOLAK' diikuti alasan 1 kalimat."
        )

        ai_res = await self.call_gemini(prompt)
        if not ai_res:
            return True
        approved = "SETUJUI" in ai_res.upper()
        log.info(f"🧠 [Gemini AGI - Keputusan Rotasi]: {ai_res}")
        return approved

    def get_latest_insight(self) -> str:
        """Mengambil wawasan terkini untuk UI konsol & cloud dashboard."""
        return self.latest_insight

gemini_brain = GeminiBrain()
