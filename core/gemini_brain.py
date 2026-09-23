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
    AI Adaptive Brain berbasis Google Gemini API (gemini-3.6-flash).
    
    Tugas & Tanggung Jawab:
    1. Continuous Learning: Menganalisis hasil setiap siklus pembayaran funding fee
       dan menyimpan memori pembelajaran strategis ke data/ai_learning_memory.json.
    2. Pre-settlement Risk Watcher: Mengevaluasi risiko perubahan drastis funding rate
       5 menit sebelum jam settlement.
    3. Rotation Advisor: Memberikan konfirmasi kualitatif sebelum bot merotasi modal
       ke pasangan koin baru di pasar.
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
        self.latest_insight: str = "AI Brain siap memantau siklus funding."
        self.load_memory()

    def load_memory(self):
        """Memuat riwayat pembelajaran AI dari penyimpanan lokal."""
        if not self.memory_path.exists():
            self.memory = []
            return
        try:
            with open(self.memory_path, "r", encoding="utf-8") as f:
                self.memory = json.load(f)
            if self.memory:
                last = self.memory[-1]
                self.latest_insight = last.get("insight", self.latest_insight)
            log.info(f"[GeminiBrain] Berhasil memuat {len(self.memory)} catatan memori pembelajaran AI.")
        except Exception as e:
            log.warning(f"[GeminiBrain] Gagal memuat memori AI: {e}")
            self.memory = []

    def save_memory(self):
        """Menyimpan riwayat pembelajaran AI ke disk."""
        try:
            # Batasi memori hingga 100 entri terbaru agar hemat memori & disk
            trimmed = self.memory[-100:]
            with open(self.memory_path, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, indent=2, default=str)
        except Exception as e:
            log.error(f"[GeminiBrain] Gagal menyimpan memori AI: {e}")

    async def call_gemini(self, prompt: str, system_instruction: Optional[str] = None) -> Optional[str]:
        """Mengirim permintaan ke Google Gemini API dengan mekanisme model fallback otomatis."""
        if not self.api_key:
            log.debug("[GeminiBrain] API key Gemini belum dikonfigurasi.")
            return None

        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 300
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
                                text = candidates[0]["content"]["parts"][0]["text"].strip()
                                return text
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
        Pembelajaran Dinamis: Dipanggil setiap kali pembayaran funding fee masuk.
        Menganalisis efisiensi yield posisi, status BEP, dan menentukan aksi strategis adaptif.
        """
        now = datetime.utcnow()
        holding_hours = (now - pos.entry_time).total_seconds() / 3600.0

        prompt = (
            f"Anda adalah otak AI Arbitrase Delta-Neutral Bitget.\n"
            f"Posisi aktif: {pos.base_asset}\n"
            f"- Nominal Kaki: ${pos.perp_leg.nominal_usdt:.2f} USDT (Leverage {pos.leverage}x)\n"
            f"- Funding Rate baru saja cair: {current_rate * 100:+.4f}%/8h\n"
            f"- Profit funding fee yang baru diterima: +${harvest_amount_usdt:.5f} USDT\n"
            f"- Akumulasi Total Funding Diterima: ${pos.cumulative_funding_received:.4f} USDT\n"
            f"- Total Biaya Transaksi: ${pos.total_fees_paid:.4f} USDT\n"
            f"- Live Unrealized PnL: ${pos.unrealized_pnl_usdt:+.4f} USDT\n"
            f"- Net PnL Bersih: ${pos.net_pnl_usdt:+.4f} USDT\n"
            f"- Status BEP (Impas): {'SUDAH BEP (PROFIT)' if pos.is_bep_reached else 'BELUM BEP (MASIH PROSES BALIK MODAL FEE)'}\n"
            f"- Durasi Holding: {holding_hours:.1f} jam\n\n"
            f"Berikan analisis singkat 2-3 kalimat dalam bahasa Indonesia:\n"
            f"1. Rekomendasi tindakan (Pilih satu: HOLD / EXTEND_HARVEST / PREPARE_ROTATION).\n"
            f"2. Alasan rasional dan poin evaluasi pembelajaran."
        )

        sys_inst = "Anda adalah eksekutif kuantitatif AI ahli strategi Delta-Neutral. Berikan rekomendasi tegas, matematis, dan berfokus pada proteksi profit."
        ai_response = await self.call_gemini(prompt, system_instruction=sys_inst)

        if not ai_response:
            ai_response = (
                f"Rekomendasi: HOLD. Akumulasi funding fee +${pos.cumulative_funding_received:.4f} USDT terus mendekati BEP. "
                f"Pertahankan posisi selama funding rate tetap positif."
            )

        self.latest_insight = ai_response
        memory_entry = {
            "timestamp": now.isoformat(),
            "event_type": "HARVEST_LEARNING",
            "base_asset": pos.base_asset,
            "harvest_profit_usdt": harvest_amount_usdt,
            "current_funding_rate": current_rate,
            "net_pnl_usdt": pos.net_pnl_usdt,
            "is_bep_reached": pos.is_bep_reached,
            "holding_hours": round(holding_hours, 2),
            "insight": ai_response
        }
        self.memory.append(memory_entry)
        self.save_memory()

        log.info(f"🧠 [Gemini AI Brain - Insight]:\n{ai_response}")
        return memory_entry

    async def evaluate_pre_settlement_risk(
        self,
        pos: DeltaNeutralPosition,
        projected_rate: float,
        seconds_until_settlement: float
    ) -> Dict[str, Any]:
        """
        Evaluasi Cepat Pra-Settlement (5 menit sebelum pembayaran funding).
        Mendeteksi jika funding rate berbalik menjadi minus atau anjlok tajam sebelum dipotong.
        """
        minutes_left = round(seconds_until_settlement / 60.0, 1)
        is_risky = projected_rate <= settings.EMERGENCY_EXIT_FUNDING_RATE

        if not is_risky and projected_rate > 0:
            return {
                "action": "SAFE",
                "risk_level": "LOW",
                "message": f"Funding rate aman ({projected_rate * 100:+.4f}% dalam {minutes_left}m)."
            }

        prompt = (
            f"PERINGATAN PRA-SETTLEMENT:\n"
            f"Koin: {pos.base_asset}\n"
            f"Waktu menuju pemotongan/pembayaran funding: {minutes_left} menit lagi.\n"
            f"Funding Rate yang diproyeksikan: {projected_rate * 100:+.4f}%/8h\n"
            f"Ambang Batas Emergency: {settings.EMERGENCY_EXIT_FUNDING_RATE * 100:+.4f}%\n"
            f"Sebagai pihak Short Perp di strategi Delta-Neutral, apakah rate ini merugikan dan harus diexit darurat sebelum dipotong? "
            f"Jawab singkat 1 kalimat (EXIT_SEKARANG atau TAHAN_PANEN)."
        )

        ai_response = await self.call_gemini(prompt)
        should_exit = is_risky or (ai_response and "EXIT_SEKARANG" in ai_response.upper())

        return {
            "action": "EMERGENCY_EXIT" if should_exit else "WATCH",
            "risk_level": "HIGH" if should_exit else "MEDIUM",
            "message": ai_response or f"Funding rate {projected_rate*100:+.4f}% di bawah batas aman."
        }

    async def evaluate_rotation_candidate(
        self,
        current_pos: DeltaNeutralPosition,
        candidate_opp: Opportunity
    ) -> bool:
        """
        Memberikan konfirmasi kualitatif AI sebelum rebalancer merotasi posisi ke koin baru.
        Return True jika disetujui untuk rotasi, False jika sebaiknya tetap hold koin lama.
        """
        # Posisi lama belum BEP -> Pastikan AI tidak memperbolehkan rotasi
        if not current_pos.is_bep_reached and current_pos.net_pnl_usdt <= 0:
            log.info(f"[GeminiBrain] Menolak rotasi: {current_pos.base_asset} belum mencapai BEP.")
            return False

        prompt = (
            f"EVALUASI ROTASI PELUANG:\n"
            f"Posisi Lama: {current_pos.base_asset} (Sudah BEP, Net PnL: ${current_pos.net_pnl_usdt:+.4f})\n"
            f"Kandidat Baru: {candidate_opp.base_asset} (Net APY: {candidate_opp.net_apy_percent:.1f}%, "
            f"Trend: {candidate_opp.funding_trend}, Konsistensi: {candidate_opp.consistency_score_percent:.0f}%)\n"
            f"Apakah rotasi ke {candidate_opp.base_asset} disarankan dengan mempertimbangkan biaya taker baru? "
            f"Jawab hanya 'SETUJUI' atau 'TOLAK' diikuti alasan singkat 1 kalimat."
        )

        ai_res = await self.call_gemini(prompt)
        if not ai_res:
            return True # Fallback ke aturan matematis bawaan jika API offline

        approved = "SETUJUI" in ai_res.upper()
        log.info(f"🧠 [Gemini AI Brain - Keputusan Rotasi]: {ai_res}")
        return approved

    def get_latest_insight(self) -> str:
        """Mengambil insight terkini untuk ditampilkan di konsol / dashboard."""
        return self.latest_insight

gemini_brain = GeminiBrain()
