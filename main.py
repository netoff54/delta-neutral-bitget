import asyncio
import argparse
import sys
import os
import gc
from datetime import datetime, timezone
from typing import Dict, Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from config.settings import settings
from utils.logger import log, console, print_clean_table
from utils.dns_resolver import patch_dns
from core.bitget_client import bitget_client
from scanner.opportunity_finder import opportunity_finder
from execution.order_executor import order_executor
from execution.position_manager import position_manager
from execution.rebalancer import auto_rebalancer
from risk.margin_guard import margin_guard
from risk.funding_guard import funding_guard
from risk.compounding_manager import compounding_manager

def print_banner(dry_run: bool):
    """Menampilkan banner konsol profesional."""
    mode_text = "[bold yellow]SIMULASI (DRY-RUN)[/bold yellow]" if dry_run else "[bold red]LIVE TRADING (UANG ASLI)[/bold red]"
    strategy_text = "[bold green]MENGGUNAKAN SELURUH SALDO TRADING CAIR[/bold green]" if settings.USE_ALL_AVAILABLE_BALANCE else f"[green]${settings.INITIAL_SEED_CAPITAL_USDT:.2f} USDT[/green]"
    console.print(Panel(
        f"[bold cyan]>>> AUTONOMOUS DYNAMIC COMPOUNDING DELTA-NEUTRAL AGENT (BITGET) <<<[/bold cyan]\n"
        f"Mode: {mode_text} | Strategi Modal: {strategy_text} | Max Leverage: [magenta]{settings.LEVERAGE}x[/magenta]\n"
        f"Compounding: [bold green]AKTIF (Profit Diputar Kembali)[/bold green] | Auto-Rebalance: [yellow]AKTIF[/yellow] | Rotasi Peluang: [yellow]AKTIF[/yellow]\n"
        f"Proteksi Mutlak: [bold yellow]DANA DI FITUR BITGET EARN / TABUNGAN 100% TERLINDUNGI & TIDAK DISENTUH BOT[/bold yellow]",
        title="[bold green]Delta-Neutral Compounding Engine[/bold green]",
        subtitle="[dim]Powered by CCXT & Bitget V2 API[/dim]",
        box=box.DOUBLE
    ))

def display_combined_balance(combined: Dict[str, Any]):
    """Menampilkan status saldo gabungan multi-market (Spot, Futures, OTC, Earn) secara transparan."""
    tot = combined.get("total_combined_equity_usdt", 0.0)
    spot = combined.get("spot", {})
    futures = combined.get("futures", {})
    otc = combined.get("otc", {})
    earn = combined.get("earn", {})

    top_tokens = []
    for h in spot.get("holdings", [])[:3]:
        if h.get("coin") != "USDT":
            top_tokens.append(f"{h['coin']}: {h['amount']} (${h['value_usdt']:.2f})")
    tokens_str = f" [dim]({', '.join(top_tokens)})[/dim]" if top_tokens else ""

    console.print(Panel(
        f"[bold cyan]🌐 TOTAL KEKAYAAN BERSIH (COMBINED NET WORTH):[/bold cyan] [bold green]${tot:,.2f} USDT[/bold green]\n"
        f"  • [bold yellow]Pasar Spot:[/bold yellow] [green]${spot.get('equity_usdt', 0.0):,.2f} USDT[/green] "
        f"[dim](Kas Bebas: ${spot.get('free_usdt', 0.0):,.2f}{tokens_str})[/dim]\n"
        f"  • [bold magenta]Pasar Futures:[/bold magenta] [green]${futures.get('equity_usdt', 0.0):,.2f} USDT[/green] "
        f"[dim](Bebas: ${futures.get('free_usdt', 0.0):,.2f} | Margin Aktif: ${futures.get('margin_used_usdt', 0.0):,.2f} | uPnL: ${futures.get('unrealized_pnl_usdt', 0.0):+.4f})[/dim]\n"
        f"  • [bold blue]Akun OTC/Pendanaan:[/bold blue] [green]${otc.get('equity_usdt', 0.0):,.2f} USDT[/green] | "
        f"[bold yellow]Bitget Earn:[/bold yellow] [green]${earn.get('equity_usdt', 0.0):,.2f} USDT[/green] [bold green](100% Terisolasi)[/bold green]",
        title="[bold green]Multi-Market Combined Portfolio & Equity Intelligence[/bold green]",
        box=box.ROUNDED
    ))

def display_compounding_pool(
    total_liquid_usdt: float,
    spot_free: float = 0.0,
    swap_free: float = 0.0,
    otc_free: float = 0.0,
    active_capital_usdt: float = 0.0,
    active_positions_count: int = 0
):
    """Menampilkan status saldo trading cair, saldo OTC, compounding profit, dan proteksi saldo Bitget Earn."""
    summary = compounding_manager.get_summary()
    buffer_pct = settings.LIQUID_SAFETY_BUFFER_PERCENT
    buffer_usdt = round(total_liquid_usdt * buffer_pct, 4)
    usable_capital = max(0.0, round(total_liquid_usdt - buffer_usdt, 2))

    if settings.USE_ALL_AVAILABLE_BALANCE:
        otc_info = f" | OTC (Pendanaan): ${otc_free:,.2f}" if settings.INCLUDE_OTC_BALANCE else ""
        if active_positions_count > 0:
            status_modal = (
                f"[bold cyan]💼 MODAL AKTIF BEKERJA:[/bold cyan] [bold green]${active_capital_usdt:,.2f} USDT[/bold green] "
                f"[dim]({active_positions_count}/{settings.MAX_CONCURRENT_POSITIONS} slot posisi aktif berjalan)[/dim]\n"
                f"[bold yellow]💵 SISA KAS BEBAS DI DOMPET:[/bold yellow] [white]${total_liquid_usdt:,.2f} USDT[/white] "
                f"[dim](Spot: ${spot_free:,.2f} | Futures: ${swap_free:,.2f}{otc_info})[/dim]\n"
                f"  • [dim]Cadangan Pengaman Fee ({buffer_pct*100:.0f}%):[/dim] [dim yellow]${buffer_usdt:,.4f} USDT[/dim yellow]\n"
                f"  • [dim]Kas Bebas Setelah Cadangan ({100-buffer_pct*100:.0f}%):[/dim] [dim white]${usable_capital:,.2f} USDT[/dim white] "
                f"[italic dim](< Batas Min Order Exchange $5.00 -> Tidak membuka posisi baru tanpa penambahan dana)[/italic dim]"
            )
        else:
            status_modal = (
                f"[bold cyan]💼 TOTAL SALDO CAIR:[/bold cyan] [bold green]${total_liquid_usdt:,.2f} USDT[/bold green] "
                f"[dim](Spot: ${spot_free:,.2f} | Futures: ${swap_free:,.2f}{otc_info})[/dim]\n"
                f"  • [dim]Cadangan Pengaman Fee ({buffer_pct*100:.0f}%):[/dim] [dim yellow]${buffer_usdt:,.4f} USDT[/dim yellow]\n"
                f"[bold green]🚀 MODAL SIAP DITRADINGKAN ({100-buffer_pct*100:.0f}%):[/bold green] [bold white]${usable_capital:,.2f} USDT[/bold white] "
                f"[dim](Per Posisi: ${usable_capital / max(1, settings.MAX_CONCURRENT_POSITIONS):,.2f} USDT | Max Lev: {settings.LEVERAGE}x)[/dim]"
            )

        console.print(Panel(
            f"{status_modal}\n"
            f"[bold magenta]📈 PROFIT FUNDING FEE DIPUTAR (COMPOUNDED):[/bold magenta] [bold yellow]+${summary.total_profit_compounded:.4f} USDT[/bold yellow] "
            f"({summary.harvest_events_count}x siklus panen otomatis membesar modal)\n"
            f"[dim]----------------------------------------------------------------------[/dim]\n"
            f"[bold yellow]🛡️ PROTEKSI BITGET EARN (SAVINGS / STAKING):[/bold yellow] [bold green]100% TERISOLASI & AMAN[/bold green]\n"
            f"[dim italic]*Seluruh dana di fitur Bitget Earn (Simple Earn/Flexible/Locked) sama sekali tidak diakses, tidak di-redeem, dan tidak disentuh oleh bot.*[/dim italic]",
            title="[bold yellow]Portfolio Capital & Bitget Earn Isolation Guard[/bold yellow]",
            box=box.ROUNDED
        ))
    else:
        unmanaged_external = max(0.0, total_liquid_usdt - summary.current_compounded_capital)
        console.print(Panel(
            f"[bold green]🌱 MODAL POKOK AWAL:[/bold green] [bold white]${summary.initial_seed_capital:.2f} USDT[/bold white]\n"
            f"[bold cyan]📈 PROFIT HASIL EARN (DIPUTAR/COMPOUNDED):[/bold cyan] [bold yellow]+${summary.total_profit_compounded:.4f} USDT[/bold yellow] "
            f"({summary.harvest_events_count}x siklus panen diputar kembali)\n"
            f"[bold magenta]💼 TOTAL MODAL AKTIF SAAT INI:[/bold magenta] [bold green]${summary.current_compounded_capital:.4f} USDT[/bold green]\n"
            f"[dim]----------------------------------------------------------------------[/dim]\n"
            f"[bold yellow]🛡️ PROTEKSI BITGET EARN & SALDO LAIN:[/bold yellow] [bold green]AMAN (100% TERISOLASI)[/bold green]\n"
            f"[dim italic]*Saldo di luar modal bot (${unmanaged_external:.2f} USDT di Earn/Spot) tidak dapat diakses atau ditarik oleh bot.*[/dim italic]",
            title="[bold yellow]Dynamic Compounding Pool & Bitget Earn Guard[/bold yellow]",
            box=box.ROUNDED
        ))

def display_ai_brain_status():
    """Menampilkan status dan wawasan pembelajaran terkini dari Google Gemini AI Brain serta pelacak kuota 50%."""
    if not settings.ENABLE_AI_BRAIN or not settings.GEMINI_API_KEY:
        return
    try:
        from core.gemini_brain import gemini_brain
        from core.historical_store import historical_store
        insight = gemini_brain.get_latest_insight()
        mem_count = len(gemini_brain.memory)
        quota = gemini_brain.get_quota_status()
        reps = historical_store.get_all_pair_reputations()
        rep_summary = ", ".join([f"{k}: {v:+.2f}" for k, v in list(reps.items())[:4]]) or "Semua aset netral awal"

        console.print(Panel(
            f"[bold magenta]🧠 AI BRAIN ENGINE:[/bold magenta] [bold cyan]{settings.GEMINI_MODEL}[/bold cyan] "
            f"[dim]({mem_count} siklus pembelajaran tersimpan di disk)[/dim]\n"
            f"[bold green]📊 ALOKASI KUOTA 50% (REAL-TIME 2M):[/bold green] [bold white]{quota['calls_today']}/{quota['max_daily_calls']} calls/hari[/bold white] "
            f"[dim](Target: ~{quota['budget_percent']}% dari plafon resmi {quota['official_rpd']} RPD Google)[/dim]\n"
            f"[bold blue]📚 MEMORI REPUTASI KOIN 7-HARI:[/bold blue] [dim white]{rep_summary}[/dim white]\n"
            f"[bold yellow]💡 KEPUTUSAN & INSIGHT TAKTIS TERKINI:[/bold yellow]\n[italic white]{insight}[/italic white]",
            title="[bold magenta]Google Gemini AI Adaptive Real-Time Brain[/bold magenta]",
            box=box.ROUNDED
        ))
    except Exception as e:
        log.debug(f"AI Brain status display notice: {e}")

def display_pnl_timeframes():
    """Menampilkan tabel PnL portofolio multi-timeframe data REAL Bitget memanjang gaya Excel (Width 109, Anti-Wrap)."""
    try:
        pnl_data = db.get_all_pnl_timeframes()
        if not pnl_data:
            return

        sep = "+-----+--------------+--------------+---------------+----------+---------------+---------------+------------+"
        header = "| TF  | MODAL AWAL   | MODAL KINI   | NET PNL USDT  | PNL (%)  | FUNDING RIIL  | AVG / HARI    | ANNUAL APY |"

        lines = [
            "\n" + sep,
            f"|  REKAP PNL PORTOFOLIO MULTI-TIMEFRAME (DATA REAL BITGET API - ZERO MOCK){'':<32} |",
            sep,
            header,
            sep
        ]

        for label in ["1D", "1W", "1M", "2M", "4M", "1Y"]:
            data = pnl_data.get(label, {})
            if not data or not data.get("has_data", False):
                lines.append(f"| {label:<3} | {'Belum Ada':<12} | {'-':<12} | {'-':<13} | {'-':<8} | {'-':<13} | {'-':<13} | {'-':<10} |")
            else:
                start_eq = f"${data.get('start_equity_usdt', 0.0):,.2f}"
                curr_eq = f"${data.get('current_equity_usdt', 0.0):,.2f}"
                pnl_val = data.get("pnl_usdt", 0.0)
                pct_val = data.get("pnl_pct", 0.0)
                pnl_u = f"${pnl_val:+.4f}"
                pnl_p = f"{pct_val:+.2f}%"
                fund_h = f"+${data.get('funding_harvested_usdt', 0.0):.4f}"
                avg_d = f"${data.get('avg_daily_pnl_usdt', 0.0):+.4f}/h"
                ann_y = f"{data.get('annualized_yield_pct', 0.0):+.1f}%"
                lines.append(f"| {label:<3} | {start_eq:<12} | {curr_eq:<12} | {pnl_u:<13} | {pnl_p:<8} | {fund_h:<13} | {avg_d:<13} | {ann_y:<10} |")

        all_time = pnl_data.get("all_time", {})
        if all_time:
            curr_eq = f"${all_time.get('current_equity_usdt', 0.0):,.2f}"
            tot_fund = f"+${all_time.get('total_funding_harvested_usdt', 0.0):.4f}"
            lines.append(f"| {'ALL':<3} | {'-':<12} | {curr_eq:<12} | {'-':<13} | {'-':<8} | {tot_fund:<13} | {'-':<13} | {'-':<10} |")

        lines.append(sep)
        print_clean_table(lines)
    except Exception as e:
        log.debug(f"PnL timeframe display notice: {e}")

def display_opportunities(opportunities, top_n: int = 10):
    """Menampilkan tabel ranking koin terbaik Bitget memanjang gaya Excel (Width 114, Anti-Wrap)."""
    if not opportunities:
        return

    # Urutkan koin berdasarkan WAKTU KE 2.5% TERCEPAT (proyeksi yield mingguan tertinggi di paling atas)
    sorted_opps = sorted(
        opportunities,
        key=lambda opp: (
            (opp.current_funding_rate * (24.0 / max(1, opp.funding_interval_hours)) * 7.0)
        ),
        reverse=True
    )

    sep = "+----+-------+------+----------+----------+-----------+-----------+---------------+------------------------------+"
    header = "| RK | KOIN  | CYCL | RATE     | PROY 1W  | EST. BEP  | KE 2.5%   | FLIP HISTORIS | STATUS KELAYAKAN & ALASAN    |"

    ranking_lines = [
        "\n" + sep,
        f"|  HASIL SCANNING PASAR & RANKING PELUANG (BOBOT: 25% YIELD, 25% BEP, 30% STABIL, 20% SPREAD){'':<14} |",
        sep,
        header,
        sep
    ]
    for idx, opp in enumerate(sorted_opps[:top_n], 1):
        cycles_day = 24.0 / max(1, opp.funding_interval_hours)
        weekly_gross = (opp.current_funding_rate * cycles_day * 7.0) * 100.0

        # Hitung waktu BEP dalam satuan HARI & jam
        bep_h = abs(opp.break_even_hours)
        bep_d = round(bep_h / 24.0, 1)
        bep_str = f"{bep_d}h ({bep_h:.0f}j)" if bep_h < 999 else "-"

        # Hitung waktu menuju target 2.5% mingguan dalam satuan HARI & jam
        rate_per_hour = (opp.current_funding_rate * 100.0) / max(1, opp.funding_interval_hours)
        hours_to_2_5 = abs(round(2.5 / rate_per_hour, 1)) if rate_per_hour > 0 else 999.0
        days_to_2_5 = round(hours_to_2_5 / 24.0, 1)
        time_to_target_str = f"{days_to_2_5}h ({hours_to_2_5:.0f}j)" if hours_to_2_5 < 999 else "-"

        # Hitung riwayat flip dan rentang hari data Bitget
        st = getattr(opp, "historical_120d_stats", None)
        if st and st.sample_count > 0:
            hist_days = max(1, int(round(st.sample_count / cycles_day)))
            flip_cnt = st.negative_flip_count
            if flip_cnt == 0:
                flip_str = f"0x Flip ({hist_days}h)"
            else:
                flip_str = f"{flip_cnt}x Flip ({hist_days}h)"
        else:
            flip_str = "0x Flip (Bebas)"

        # Status kelayakan & Alasan Jelas
        if opp.is_eligible:
            status_and_reason = f"[LAYAK] Sprd {opp.basis_spread_percent:+.2f}% Vol ${opp.volume_24h_usdt/1e6:.1f}M"
        else:
            r_reason = (getattr(opp, "rejection_reason", "") or "").lower()
            if "flip" in r_reason or "negatif" in r_reason:
                status_and_reason = f"[TIDAK] Flip Minus ({getattr(st, 'negative_flip_count', 0)}x)"
            elif "volume" in r_reason:
                status_and_reason = f"[TIDAK] Vol ${opp.spot_volume_24h/1000:.0f}k < $50k"
            elif "spread" in r_reason:
                status_and_reason = f"[TIDAK] Spread ({opp.basis_spread_percent:+.3f}%)"
            elif "prediksi" in r_reason:
                status_and_reason = "[TIDAK] Prediksi Rate Negatif"
            else:
                raw_r = getattr(opp, "rejection_reason", "Filter Risiko")
                status_and_reason = f"[TIDAK] {raw_r[:20]}"

        rate_str = f"{opp.current_funding_rate*100:+.4f}%"
        gross_str = f"{weekly_gross:+.2f}%"
        int_str = f"{opp.funding_interval_hours}h"

        row = (
            f"| #{idx:<2} "
            f"| {opp.base_asset:<5} "
            f"| {int_str:<4} "
            f"| {rate_str:<8} "
            f"| {gross_str:<8} "
            f"| {bep_str:<9} "
            f"| {time_to_target_str:<9} "
            f"| {flip_str:<13} "
            f"| {status_and_reason:<28} |"
        )
        ranking_lines.append(row)

    ranking_lines.append(sep)
    print_clean_table(ranking_lines)

def display_active_positions():
    """Menampilkan telemetri portofolio posisi aktif gaya Excel (Width 108/105, Anti-Wrap & Zero Truncation)."""
    active_positions = position_manager.get_active_positions()
    if not active_positions:
        log.info("📊 [Portofolio] Belum ada posisi Delta-Neutral yang aktif saat ini. Modal cair siap dialokasikan.")
        return

    from utils.interval_helper import safe_hours_passed

    sep_overview = "+--------+--------------------------+--------------------------+---------------------+---------------------+"
    hdr_overview = "| KOIN   | ENTRY (SPOT / PERP)      | HARGA KINI & SPREAD      | STATUS & EST. BEP   | PROGRESS KE 2.5%    |"

    overview_lines = [
        "\n" + sep_overview,
        f"|  RINGKASAN POSISI DELTA-NEUTRAL BERJALAN (GROUND-TRUTH BITGET){'':<45} |",
        sep_overview,
        hdr_overview,
        sep_overview
    ]

    card_lines = []

    for pos in active_positions:
        holding_h = safe_hours_passed(pos.entry_time) if hasattr(pos, "entry_time") else 0.0
        spot_nominal = getattr(pos.spot_leg, "nominal_usdt", 0.0) or (pos.spot_leg.amount * pos.spot_leg.entry_price)
        perp_nominal = (getattr(pos.perp_leg, "nominal_usdt", 0.0) or (pos.perp_leg.amount * pos.perp_leg.entry_price))
        roe_val = getattr(pos, "current_roe_percent", 0.0)

        # Baseline Modal Awal ($64.0 default) & Saldo Ekuitas Riil Bitget
        baseline = compounding_manager.initial_seed if compounding_manager.initial_seed > 0 else 64.0
        port_equity = getattr(pos, "portfolio_equity_now", 0.0) or (spot_nominal + spot_nominal)
        port_net = getattr(pos, "portfolio_net_pnl_usdt", pos.net_pnl_usdt)
        is_port_bep = getattr(pos, "portfolio_bep_reached", False) or (port_net >= 0.0)

        # 1. Sekarang Udah Berapa Persen vs Modal Awal $64
        current_pct_vs_baseline = (port_net / baseline * 100.0) if baseline > 0 else 0.0

        # 2. Estimasi Kapan Nyampe BEP (Dalam Satuan HARI & Jam)
        rate_per_hour_usdt = 0.0
        if pos.last_funding_rate > 0 and pos.spot_leg.nominal_usdt > 0:
            rate_per_hour_usdt = (pos.last_funding_rate * pos.spot_leg.nominal_usdt) / max(1, pos.funding_interval_hours)

        if is_port_bep:
            kapan_bep_str = "SUDAH BEP LUNAS"
            status_bep_short = "[LUNAS BEP]"
            bep_card_desc = f"SUDAH BEP LUNAS (+${port_net:.4f} USDT di atas modal)"
        elif rate_per_hour_usdt > 0 and port_net < 0:
            hours_to_bep = abs(port_net) / rate_per_hour_usdt
            days_to_bep = abs(round(hours_to_bep / 24.0, 1))
            kapan_bep_str = f"{days_to_bep} hari ({abs(hours_to_bep):.1f} jam)"
            status_bep_short = f"[MENUJU] {days_to_bep} hari"
            bep_card_desc = f"{days_to_bep} hari ({abs(hours_to_bep):.1f} jam) pada rate funding saat ini"
        else:
            kapan_bep_str = "Menunggu Funding Positif"
            status_bep_short = "[WAIT SETTLE]"
            bep_card_desc = "Menunggu akumulasi settlement funding berikutnya"

        # 3. Estimasi Kapan Nyampe 2.5% per Minggu (Dalam Satuan HARI & Jam)
        target_weekly_profit_usdt = round(baseline * (settings.TARGET_WEEKLY_NET_YIELD_PERCENT / 100.0), 2)
        progress_to_target_pct = (port_net / target_weekly_profit_usdt * 100.0) if target_weekly_profit_usdt > 0 and port_net > 0 else 0.0
        profit_gap_usdt = max(0.0, target_weekly_profit_usdt - port_net)

        if port_net >= target_weekly_profit_usdt:
            kapan_target_str = "TARGET 2.5% TERCAPAI"
            progress_short = "+2.5% [TERCAPAI]"
            target_card_desc = f"TARGET 2.5% TERCAPAI (+${port_net:.4f} USDT)"
        elif rate_per_hour_usdt > 0 and profit_gap_usdt > 0:
            hours_to_target = profit_gap_usdt / rate_per_hour_usdt
            days_to_target = abs(round(hours_to_target / 24.0, 1))
            kapan_target_str = f"{days_to_target} hari ({abs(hours_to_target):.0f} jam)"
            progress_short = f"{current_pct_vs_baseline:+.2f}% ({days_to_target}h)"
            target_card_desc = f"{days_to_target} hari ({abs(hours_to_target):.0f} jam) menuju target +${target_weekly_profit_usdt:.2f} USDT"
        else:
            kapan_target_str = "Akumulasi Bunga"
            progress_short = f"{current_pct_vs_baseline:+.2f}%"
            target_card_desc = f"Akumulasi bunga (Sisa target: +${profit_gap_usdt:.4f} USDT)"

        # 4. Basis Spread Live
        basis_spread = ((pos.perp_leg.current_price - pos.spot_leg.current_price) / pos.spot_leg.current_price * 100.0) if pos.spot_leg.current_price > 0 else 0.0
        spread_status = "AMAN" if basis_spread >= 0 else "CONVERGE"

        # Overview row format
        entry_short = f"S:${pos.spot_leg.entry_price:,.4f} | P:${pos.perp_leg.entry_price:,.4f}"
        price_spread_short = f"${pos.spot_leg.current_price:,.4f}/${pos.perp_leg.current_price:,.4f} ({basis_spread:+.2f}%)"

        overview_row = (
            f"| {pos.base_asset:<6} "
            f"| {entry_short:<24} "
            f"| {price_spread_short:<24} "
            f"| {status_bep_short:<19} "
            f"| {progress_short:<19} |"
        )
        overview_lines.append(overview_row)

        # Detailed Excel Property Card (Width 105)
        sep_card = "+------------------------------+------------------------------------------------------------------------+"
        title_card = f"RINCIAN METRIK PORTOFOLIO DELTA-NEUTRAL: {pos.base_asset} (FOKUS 1 KOIN 1x ISOLATED)"
        header_card = "| PARAMETER / METRIK           | NILAI REAL & TELEMETRI GROUND-TRUTH                                    |"

        card_rows = [
            ("Koin & Mode Trading", f"{pos.base_asset} (Spot Leg Beli + Coin-Margined Perp Short 1x Isolated)"),
            ("Entry Spot (Beli)", f"{pos.spot_leg.amount:.2f} {pos.base_asset} @ ${pos.spot_leg.entry_price:,.4f} (Nominal: ${spot_nominal:.2f} USDT)"),
            ("Entry Perp (Short)", f"{pos.perp_leg.amount:.2f} {pos.base_asset} @ ${pos.perp_leg.entry_price:,.4f} (Nominal: ${perp_nominal:.2f} USDT)"),
            ("Harga Kini (Spot / Perp)", f"Spot: ${pos.spot_leg.current_price:,.4f} | Perp: ${pos.perp_leg.current_price:,.4f} (Spread: {basis_spread:+.3f}% [{spread_status}])"),
            ("Status BEP Akun", f"{'[SUDAH BEP]' if is_port_bep else '[MENUJU BEP]'} Selisih Impas: ${abs(port_net):.4f} USDT Net"),
            ("Estimasi Waktu ke BEP", bep_card_desc),
            ("Progress vs Modal Awal ($64)", f"{current_pct_vs_baseline:+.2f}% vs ${baseline:.2f} (Target Mingguan: +{settings.TARGET_WEEKLY_NET_YIELD_PERCENT:.2f}% / ~${target_weekly_profit_usdt:.2f})"),
            ("Waktu ke Target 2.5%", target_card_desc),
            ("Total Funding Fee Dipanen", f"+${pos.cumulative_funding_received:.4f} USDT (Tercatat Riil di Buku Besar Bitget)"),
            ("Tingkat Funding Terkini", f"{pos.last_funding_rate*100:+.4f}% / {pos.funding_interval_hours} jam (Est: +${rate_per_hour_usdt*pos.funding_interval_hours:.5f} USDT/siklus)"),
            ("Keamanan Margin & ROE", f"MMR Bitget: {pos.current_margin_ratio:.1%} (Aman <85%) | ROE Futures: {roe_val:+.2f}%"),
        ]

        card_lines.extend([
            "\n" + sep_card,
            f"|  {title_card:<100} |",
            sep_card,
            header_card,
            sep_card
        ])

        for label, val in card_rows:
            card_lines.append(f"| {label:<28} | {val:<70} |")

        card_lines.append(sep_card)

    overview_lines.append(sep_overview)
    print_clean_table(overview_lines)
    print_clean_table(card_lines)


from core.database import db

import threading

_health_server_started = False

def _run_dedicated_health_server(port: int):
    """
    Menjalankan HTTP server di thread terisolasi khusus agar port 10000
    SELALU merespons < 1ms tanpa pernah terhambat tugas berat trading atau sinkronisasi database.
    """
    import asyncio
    from aiohttp import web

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()

    async def handle_health(request):
        active_positions = position_manager.get_active_positions()
        pos_summary = [
            {
                "symbol": p.base_asset,
                "spot_qty": p.spot_leg.amount,
                "perp_contracts": p.perp_leg.amount,
                "unrealized_pnl": p.unrealized_pnl_usdt,
                "funding_received": p.cumulative_funding_received,
                "net_delta": p.net_delta,
                "interval_hours": p.funding_interval_hours,
                "mmr_percent": round(p.current_margin_ratio * 100.0, 2),
                "roe_percent": getattr(p, "current_roe_percent", 0.0),
                "is_bep_reached": p.is_bep_reached
            }
            for p in active_positions
        ]
        return web.json_response({
            "status": "healthy",
            "service": "Delta-Neutral Bitget Autonomous Agent",
            "database_engine": "PostgreSQL" if db.is_postgres else "SQLite",
            "active_positions": len(active_positions),
            "positions": pos_summary,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    async def handle_history(request):
        positions = db.get_positions_history(limit=20)
        orders = db.get_orders_history(limit=20)
        harvests = db.get_harvest_history(limit=20)
        total_profit = db.get_total_harvested_profit()
        return web.json_response({
            "database_engine": "PostgreSQL" if db.is_postgres else "SQLite",
            "total_harvested_profit_usdt": total_profit,
            "positions_count": len(positions),
            "recent_positions": positions,
            "recent_orders": orders,
            "recent_harvests": harvests,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    async def handle_agentic(request):
        mem = db.get_agentic_memory(limit=20)
        reps = db.get_all_pair_reputations()
        return web.json_response({
            "database_engine": "PostgreSQL" if db.is_postgres else "SQLite",
            "total_memories_stored": len(mem),
            "pair_reputations": reps,
            "recent_agentic_memories": mem,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    async def handle_balance(request):
        latest = db.get_latest_combined_balance()
        history = db.get_combined_balance_history(limit=20)
        growth = db.get_equity_growth_stats(days=7)
        return web.json_response({
            "database_engine": "PostgreSQL" if db.is_postgres else "SQLite",
            "latest_combined_balance": latest,
            "equity_growth_stats_7d": growth,
            "history_count": len(history),
            "recent_history": history,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    async def handle_pnl(request):
        timeframe_param = request.rel_url.query.get("tf", None)
        if timeframe_param:
            tf_map = {"1d": 1, "1w": 7, "1m": 30, "2m": 60, "4m": 120, "1y": 365, "7": 7, "30": 30, "60": 60, "120": 120, "365": 365}
            days = tf_map.get(timeframe_param.lower(), 7)
            pnl_data = db.get_pnl_for_timeframe(days)
            equity_curve = db.get_pnl_equity_curve(days=days)
            return web.json_response({
                "database_engine": "PostgreSQL" if db.is_postgres else "SQLite",
                "pnl": pnl_data,
                "equity_curve_count": len(equity_curve),
                "equity_curve": equity_curve[-50:],
                "note": "Data REAL dari Bitget API. Tidak ada data fiktif/mock.",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
        else:
            all_pnl = db.get_all_pnl_timeframes()
            total_harvest = db.get_total_harvested_profit()
            return web.json_response({
                "database_engine": "PostgreSQL" if db.is_postgres else "SQLite",
                "pnl_by_timeframe": all_pnl,
                "total_funding_harvested_all_time": total_harvest,
                "usage": "?tf=1d|1w|1m|1y untuk timeframe spesifik",
                "note": "Data REAL dari Bitget API. Tidak ada data fiktif/mock.",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/history", handle_history)
    app.router.add_get("/agentic", handle_agentic)
    app.router.add_get("/balance", handle_balance)
    app.router.add_get("/pnl", handle_pnl)

    try:
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", port)
        loop.run_until_complete(site.start())
        log.info(f"🌐 [Health Server] Dedicated isolated thread aktif di port {port} (Zero-latency instant response).")
        loop.run_forever()
    except Exception as e:
        log.error(f"[Health Server] Thread error: {e}")

async def start_health_check_server():
    """Memulai server HTTP mini di thread independen agar responsif 100% tanpa delay."""
    global _health_server_started
    if _health_server_started:
        return
    port = int(os.getenv("PORT", "10000"))
    t = threading.Thread(target=_run_dedicated_health_server, args=(port,), daemon=True)
    t.start()
    _health_server_started = True
    await asyncio.sleep(0.5)  # Beri waktu thread mengikat port

async def self_ping_loop():
    """Ping endpoint /health milik sendiri setiap 4 menit agar Render Free Tier tidak spin-down.
    Menggunakan RENDER_EXTERNAL_URL jika tersedia, fallback ke localhost.
    """
    import aiohttp
    port = int(os.getenv("PORT", "10000"))
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    ping_url = f"{render_url}/health" if render_url else f"http://localhost:{port}/health"
    log.info(f"🏓 [Self-Ping] Aktif — akan ping {ping_url} setiap 4 menit untuk mencegah spin-down.")
    try:
        await asyncio.sleep(30)  # tunggu health server benar-benar siap
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        log.debug(f"🏓 [Self-Ping] OK ({resp.status}) → {ping_url}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"🏓 [Self-Ping] Gagal: {e}")
            await asyncio.sleep(240)  # 4 menit
    except asyncio.CancelledError:
        pass

async def run_autonomous_loop(auto_trade: bool, manual_capital: float = None):
    """Loop otonom: Memindai pasar, memprediksi yield, auto-rebalance, dan memutar profit (compounding)."""
    # 1. Buka port HTTP Health Server terlebih dahulu agar Cloud (Render/Railway) langsung mendeteksi status LIVE
    health_runner = await start_health_check_server()

    # 1.1. Jalankan self-ping background task untuk mencegah Render Free Tier spin-down
    ping_task = asyncio.create_task(self_ping_loop())

    # 2. Inisialisasi koneksi exchange
    await bitget_client.initialize()

    # 2.1. Sinkronisasi posisi aktif langsung dari Bitget (Auto-Discovery Cloud/Restart)
    try:
        await position_manager.sync_active_positions_from_exchange(bitget_client)
    except Exception as se:
        log.warning(f"Initial exchange position sync notice: {se}")

    try:
        while True:
            try:
                if sys.stdout.isatty() and not os.getenv("RAILWAY_ENVIRONMENT") and not os.getenv("RENDER"):
                    console.clear()
                print_banner(settings.DRY_RUN)
                console.print(f"[dim]Waktu Pemeriksaan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]\n")

                # 0. Rekonsiliasi exchange & ambil saldo akun dan tampilkan Portfolio Pool & Proteksi Earn
                await position_manager.sync_active_positions_from_exchange(bitget_client)
                active_pos_list = position_manager.get_active_positions()
                active_pos_count = len(active_pos_list)

                # Hitung nilai modal aktual yang sedang aktif di posisi (Spot + Futures Margin)
                active_cap = 0.0
                for p in active_pos_list:
                    spot_val = getattr(p.spot_leg, "nominal_usdt", 0.0) or (p.spot_leg.entry_price * p.spot_leg.amount)
                    perp_margin = (getattr(p.perp_leg, "nominal_usdt", 0.0) or (p.perp_leg.entry_price * p.perp_leg.amount)) / max(1, getattr(p, "leverage", 1))
                    active_cap += (spot_val + perp_margin)

                bal = await bitget_client.fetch_balance()
                total_bal = float(bal.get("total", {}).get("USDT", 0.0) or bal.get("USDT", {}).get("total", 0.0) or 0.0)
                spot_free = float(bal.get("spot_free", 0.0))
                swap_free = float(bal.get("swap_free", 0.0))
                otc_free = float(bal.get("otc_free", 0.0))

                # Ambil saldo gabungan multi-market (Spot tokens bernilai USDT, Futures, OTC, Earn)
                try:
                    combined_bal = await bitget_client.fetch_all_combined_balances()
                    display_combined_balance(combined_bal)
                    db.record_combined_balance(combined_bal, active_positions_count=active_pos_count)
                except Exception as cbe:
                    log.warning(f"[MainLoop] Notice perolehan saldo gabungan: {cbe}")
                    combined_bal = {}

                # Hitung total ekuitas trading bersih portofolio (Spot + Futures, 100% tanpa Bitget Earn)
                spot_eq = float(combined_bal.get("spot", {}).get("equity_usdt", 0.0))
                futures_eq = float(combined_bal.get("futures", {}).get("equity_usdt", 0.0))
                otc_eq = float(combined_bal.get("otc", {}).get("equity_usdt", 0.0) if settings.INCLUDE_OTC_BALANCE else 0.0)
                total_trading_equity = spot_eq + futures_eq + otc_eq

                # ⭐ KUNCI: Sinkronisasi modal pokok dari total ekuitas trading REAL Bitget
                compounding_manager.sync_from_real_balance(total_trading_equity if total_trading_equity > 0 else total_bal)

                display_compounding_pool(
                    total_liquid_usdt=total_bal,
                    spot_free=spot_free,
                    swap_free=swap_free,
                    otc_free=otc_free,
                    active_capital_usdt=active_cap,
                    active_positions_count=active_pos_count
                )

                display_ai_brain_status()

                # 0.2 Tampilkan analisis PnL multi-timeframe (1D, 1W, 1M, 1Y) dari data REAL
                display_pnl_timeframes()

                # Rekam snapshot ekuitas & saldo portofolio ke database
                try:
                    summary = compounding_manager.get_summary()
                    db.record_portfolio_snapshot(
                        total_liquid_usdt=total_bal,
                        spot_free_usdt=spot_free,
                        swap_free_usdt=swap_free,
                        otc_free_usdt=otc_free,
                        active_positions_count=active_pos_count,
                        compounded_capital_usdt=summary.current_compounded_capital,
                        total_profit_harvested_usdt=summary.total_profit_compounded
                    )
                except Exception as dbe:
                    log.debug(f"[MainLoop] Portfolio snapshot record notice: {dbe}")

                # 1. Health & Risk Check pada posisi aktif
                log.info("Menjalankan pemeriksaan risiko (Margin Guard & Funding Guard)...")
                await margin_guard.check_positions_health()
                await funding_guard.check_positions_funding()

                # 2. Auto-Rebalancing Delta Drift
                await auto_rebalancer.rebalance_delta_drift()

                # Tampilkan posisi aktif
                display_active_positions()

                # 3. Pindai dan evaluasi seluruh pasar secara prediktif
                # Jika ada posisi aktif, evaluasi peluang & rotasi dinilai berdasarkan modal posisi aktif (~$63) yang akan di-unwind
                # Jika tidak ada posisi aktif, gunakan modal cair yang siap ditradingkan
                if manual_capital:
                    scan_capital = manual_capital
                elif active_pos_count > 0 and (active_cap >= 10.0 or total_trading_equity >= 10.0):
                    scan_capital = round(max(active_cap, total_trading_equity), 2)
                else:
                    scan_capital = compounding_manager.get_position_capital(total_bal)

                opportunities = await opportunity_finder.scan(target_nominal_usdt=scan_capital)
                display_opportunities(opportunities, top_n=10)

                # 3.5. Evaluasi Real-Time Ground-Truth oleh Gemini AGI (Alokasi 50% Kuota = 720 calls/hari)
                if settings.ENABLE_AI_BRAIN and getattr(settings, "AI_REALTIME_EVALUATION", True):
                    from core.gemini_brain import gemini_brain
                    cd_mins = None
                    if active_pos_list:
                        try:
                            cd_res = await bitget_client.get_funding_settlement_countdown(active_pos_list[0].perp_leg.symbol)
                            cd_mins = cd_res.get("minutes_until_settlement")
                        except Exception:
                            pass

                    agi_eval = await gemini_brain.evaluate_realtime_ground_truth(
                        active_positions=active_pos_list,
                        total_balance_usdt=total_bal,
                        spot_free_usdt=spot_free,
                        swap_free_usdt=swap_free,
                        top_opportunities=opportunities,
                        market_countdown_minutes=cd_mins
                    )

                    if agi_eval.get("action") == "EMERGENCY_UNWIND" and active_pos_list:
                        log.warning(f"🚨 [Gemini AGI Ground-Truth]: Memutuskan EMERGENCY UNWIND -> {agi_eval.get('tactical_guidance')}")
                        await funding_guard.emergency_close_position(active_pos_list[0], reason="AGI Ground-Truth Risk Alert")

                # 4. Rotasi Peluang Otomatis (Mencari taker lain dengan potensi yield lebih tinggi)
                await auto_rebalancer.evaluate_and_rotate(
                    opportunities=opportunities,
                    capital_per_position=scan_capital
                )

                # 5. Keputusan Eksekusi Pembukaan Posisi Baru (Hanya jika slot tersedia dan ada kas cair yang cukup)
                eligible_opportunities = [o for o in opportunities if o.is_eligible]
                max_allowed_positions = settings.MAX_CONCURRENT_POSITIONS
                liquid_entry_capital = manual_capital if manual_capital else compounding_manager.get_position_capital(total_bal)

                if auto_trade and eligible_opportunities:
                    if active_pos_count < max_allowed_positions:
                        if liquid_entry_capital < 10.0:
                            console.print(
                                f"\n[yellow]⚠️ Slot posisi tersedia ({active_pos_count}/{max_allowed_positions}), namun sisa kas bebas "
                                f"(${liquid_entry_capital:.2f} USDT) di bawah batas minimal order exchange ($10.00 USDT).[/yellow]"
                            )
                        else:
                            from core.gemini_brain import gemini_brain
                            best_opp = await gemini_brain.select_optimal_taker_agi(eligible_opportunities, liquid_entry_capital) or eligible_opportunities[0]
                            if not position_manager.get_position_by_base(best_opp.base_asset):
                                console.print(
                                    f"\n[bold green]🎯 Peluang Terbaik Terpilih AGI: {best_opp.base_asset} "
                                    f"(Skor: {best_opp.composite_performance_score:.1f}, Prediksi Next: {best_opp.predicted_next_funding_rate*100:.4f}%, "
                                    f"Net APY: {best_opp.net_apy_percent:.1f}%).\n"
                                    f"Mengeksekusi posisi Delta-Neutral dengan Alokasi Modal Cair: ${liquid_entry_capital:.2f} USDT "
                                    f"(Max {settings.LEVERAGE}x Leverage)...[/bold green]"
                                )
                                await order_executor.open_delta_neutral_position(
                                    opportunity=best_opp,
                                    allocated_capital_usdt=liquid_entry_capital
                                )
                    else:
                        console.print(
                            f"\n[yellow]Kapasitas modal trading terpakai penuh ({active_pos_count}/{max_allowed_positions} posisi aktif). "
                            f"Modal sedang bekerja memanen funding fee.[/yellow]"
                        )

                # Bersihkan memori RAM agar hemat biaya container di cloud
                gc.collect()

                # Sleep interval dengan pemantauan posisi aktif terisolasi
                console.print(f"\n[dim]Menunggu {settings.SCAN_INTERVAL_SECONDS} detik untuk pemindaian pasar berikutnya (pemantauan posisi aktif tiap {settings.MONITOR_INTERVAL_SECONDS} detik)...[/dim]")
                sleep_remaining = settings.SCAN_INTERVAL_SECONDS
                monitor_interval = getattr(settings, "MONITOR_INTERVAL_SECONDS", 60)
                while sleep_remaining > 0:
                    step = min(monitor_interval, sleep_remaining)
                    await asyncio.sleep(step)
                    sleep_remaining -= step
                    if sleep_remaining > 0 and position_manager.get_active_positions():
                        try:
                            await margin_guard.check_positions_health()
                            await funding_guard.check_positions_funding()
                        except Exception as guard_err:
                            log.warning(f"[Monitor Intermediate] Kendala pemantauan posisi: {guard_err}")

            except Exception as cycle_err:
                log.error(f"⚠️ [Loop Resiliency] Terjadi kendala siklus: {cycle_err}", exc_info=True)
                await asyncio.sleep(5)

    except asyncio.CancelledError:
        log.info("Autonomous loop dihentikan.")
    finally:
        ping_task.cancel()
        try:
            await ping_task
        except asyncio.CancelledError:
            pass
        if health_runner:
            try:
                await health_runner.cleanup()
            except Exception:
                pass
        await bitget_client.close()

async def main():
    parser = argparse.ArgumentParser(description="Autonomous Compounding Delta-Neutral Agent for Bitget")
    parser.add_argument("--scan-only", action="store_true", help="Hanya jalankan pemindaian satu kali tanpa membuka posisi")
    parser.add_argument("--dry-run", action="store_true", default=None, help="Paksa mode simulasi (tanpa eksekusi uang asli)")
    parser.add_argument("--live", action="store_true", help="Aktifkan eksekusi uang asli di Bitget")
    parser.add_argument("--auto-trade", action="store_true", help="Otomatis buka posisi ketika peluang yang memenuhi syarat ditemukan")
    parser.add_argument("--capital", type=float, default=None, help="Override modal posisi dalam USDT (default: dinamis menggunakan seluruh modal trading cair)")
    args = parser.parse_args()

    if args.live:
        settings.DRY_RUN = False
    elif args.dry_run is not None:
        settings.DRY_RUN = args.dry_run

    print_banner(settings.DRY_RUN)

    if args.scan_only:
        try:
            await bitget_client.initialize()
            bal = await bitget_client.fetch_balance()
            total_bal = float(bal.get("total", {}).get("USDT", 0.0) or bal.get("USDT", {}).get("total", 0.0) or (settings.TOTAL_MAX_CAPITAL_USDT or 0.0))
            spot_free = float(bal.get("spot_free", 0.0))
            swap_free = float(bal.get("swap_free", 0.0))
            otc_free = float(bal.get("otc_free", 0.0))
            display_compounding_pool(total_bal, spot_free, swap_free, otc_free)
            scan_cap = args.capital or compounding_manager.get_position_capital(total_bal)
            opportunities = await opportunity_finder.scan(target_nominal_usdt=scan_cap)
            display_opportunities(opportunities, top_n=15)
        finally:
            await bitget_client.close()
        return

    # Indestructible Supervisor Loop:
    # Memastikan bahwa bot tidak pernah berhenti (exit 1) di Render/Cloud.
    # Jika terjadi kendala fatal, supervisor menangkapnya, mencatatnya, dan me-restart loop otonom secara mulus.
    retry_delay = 5
    while True:
        try:
            await run_autonomous_loop(auto_trade=args.auto_trade, manual_capital=args.capital)
            break
        except asyncio.CancelledError:
            log.info("Bot dihentikan oleh sistem (Cancelled).")
            break
        except Exception as fatal_loop_err:
            log.critical(
                f"💥 [SUPERVISOR - Auto-Restart] Terjadi kendala fatal di loop otonom: {fatal_loop_err}",
                exc_info=True
            )
            log.info(f"🔄 [SUPERVISOR] Memulai ulang autonomous trading loop dalam {retry_delay} detik...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(60, retry_delay * 2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[bold red]Bot dihentikan oleh pengguna.[/bold red]")
        sys.exit(0)
    except Exception as fatal_e:
        log.critical(f"💥 [FATAL ROOT EXCEPTION]: {fatal_e}", exc_info=True)
        sys.exit(0)
