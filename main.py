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
from utils.logger import log, console, print_clean_table, log_separator
from utils.dns_resolver import patch_dns
from core.bitget_client import bitget_client
from scanner.opportunity_finder import opportunity_finder
from execution.order_executor import order_executor
from execution.position_manager import position_manager
from execution.rebalancer import auto_rebalancer
from risk.margin_guard import margin_guard
from risk.funding_guard import funding_guard
from risk.compounding_manager import compounding_manager, monthly_tp_manager
from risk.yield_vault import yield_vault

def print_banner(dry_run: bool):
    """Menampilkan banner konsol profesional ringkas (Width 76)."""
    mode_text = "[bold yellow]SIMULASI[/bold yellow]" if dry_run else "[bold red]LIVE TRADING (UANG ASLI)[/bold red]"
    console.print(Panel(
        f"🤖 [bold cyan]AUTONOMOUS COMPOUNDING DELTA-NEUTRAL AGENT (BITGET)[/bold cyan]\n"
        f"🎯 Mode: {mode_text} | Leverage: [magenta]{settings.LEVERAGE}x (Isolated Short)[/magenta]\n"
        f"📈 Compounding: [bold green]AKTIF[/bold green] | Rebalance: [yellow]AKTIF[/yellow] | Filter Min Vol: [bold green]$10k[/bold green]\n"
        f"🛡️ Proteksi: [bold green]Bitget Earn / Tabungan 100% Aman Terisolasi[/bold green]",
        title="[bold green]Delta-Neutral Compounding Engine[/bold green]",
        subtitle="[dim]Powered by CCXT & Bitget V2 API[/dim]",
        width=76,
        box=box.ROUNDED
    ))

def display_combined_balance(combined: Dict[str, Any]):
    """Menampilkan status saldo gabungan multi-market ringkas (Width 76)."""
    tot = combined.get("total_combined_equity_usdt", 0.0)
    spot = combined.get("spot", {})
    futures = combined.get("futures", {})
    otc = combined.get("otc", {})
    earn = combined.get("earn", {})

    top_tokens = []
    for h in spot.get("holdings", [])[:2]:
        if h.get("coin") != "USDT":
            top_tokens.append(f"{h['coin']}: {h['amount']:.2f}")
    tokens_str = f" ({', '.join(top_tokens)})" if top_tokens else ""

    console.print(Panel(
        f"🌐 [bold cyan]TOTAL EKUITAS BERSIH:[/bold cyan] [bold green]${tot:,.2f} USDT[/bold green]\n"
        f"  • 🟡 [yellow]Spot:[/yellow] [green]${spot.get('equity_usdt', 0.0):,.2f}[/green] [dim](Kas Bebas: ${spot.get('free_usdt', 0.0):,.2f}{tokens_str})[/dim]\n"
        f"  • 🟣 [magenta]Futures:[/magenta] [green]${futures.get('equity_usdt', 0.0):,.2f}[/green] [dim](Margin: ${futures.get('margin_used_usdt', 0.0):,.2f} | uPnL: ${futures.get('unrealized_pnl_usdt', 0.0):+.4f})[/dim]\n"
        f"  • 🔵 [blue]OTC:[/blue] [green]${otc.get('equity_usdt', 0.0):,.2f}[/green] | 🛡️ [yellow]Earn:[/yellow] [green]${earn.get('equity_usdt', 0.0):,.2f}[/green] [bold green](100% Terisolasi)[/bold green]",
        title="[bold green]Multi-Market Portfolio Intelligence[/bold green]",
        width=76,
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
    """Menampilkan status modal awal $64, penyerapan deposit baru, modal aktif, dan proteksi Earn (Width 76)."""
    summary = compounding_manager.get_summary()
    baseline = summary.initial_seed_capital if summary.initial_seed_capital > 0 else 64.0
    buffer_pct = settings.LIQUID_SAFETY_BUFFER_PERCENT
    buffer_usdt = round(total_liquid_usdt * buffer_pct, 4)
    usable_capital = max(0.0, round(total_liquid_usdt - buffer_usdt, 2))

    otc_info = f" | OTC: ${otc_free:,.2f}" if settings.INCLUDE_OTC_BALANCE else ""
    if active_positions_count > 0:
        status_modal = (
            f"💼 [bold cyan]MODAL AKTIF:[/bold cyan] [bold green]${active_capital_usdt:,.2f} USDT[/bold green] [dim]({active_positions_count}/{settings.MAX_CONCURRENT_POSITIONS} posisi aktif)[/dim]\n"
            f"💵 [bold yellow]KAS BEBAS:[/bold yellow] [white]${total_liquid_usdt:,.2f} USDT[/white] [dim](Spot: ${spot_free:,.2f} | Fut: ${swap_free:,.2f}{otc_info})[/dim]\n"
            f"  • [dim]Cadangan Fee ({buffer_pct*100:.0f}%): ${buffer_usdt:,.4f} | Kas Siap: ${usable_capital:,.2f} USDT[/dim]"
        )
    else:
        status_modal = (
            f"💼 [bold cyan]TOTAL SALDO CAIR:[/bold cyan] [bold green]${total_liquid_usdt:,.2f} USDT[/bold green] [dim](Spot: ${spot_free:,.2f} | Fut: ${swap_free:,.2f}{otc_info})[/dim]\n"
            f"🚀 [bold green]MODAL SIAP DITRADINGKAN ({100-buffer_pct*100:.0f}%):[/bold green] [bold white]${usable_capital:,.2f} USDT[/bold white] [dim](Max Lev: {settings.LEVERAGE}x)[/dim]"
        )

    console.print(Panel(
        f"🌱 [bold green]MODAL AWAL BASELINE:[/bold green] [bold white]${baseline:.2f} USDT[/bold white] [dim](Setoran Pokok Awal)[/dim]\n"
        f"📥 [bold blue]PENAMBAHAN DEPOSIT:[/bold blue] [bold cyan]Auto-Detect Aktif[/bold cyan] [dim](Setoran baru otomatis diserap)[/dim]\n"
        f"{status_modal}\n"
        f"📈 [bold magenta]PROFIT COMPOUNDING:[/bold magenta] [bold yellow]+${summary.total_profit_compounded:.4f} USDT[/bold yellow] [dim]({summary.harvest_events_count}x panen diputar)[/dim]\n"
        f"🛡️ [bold yellow]PROTEKSI BITGET EARN:[/bold yellow] [bold green]100% TERISOLASI & AMAN[/bold green]",
        title="[bold yellow]Dynamic Capital & Compounding Guard[/bold yellow]",
        width=76,
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
    """Menampilkan tabel PnL portofolio multi-timeframe real Bitget dengan pewarnaan penuh."""
    try:
        pnl_data = db.get_all_pnl_timeframes()
        if not pnl_data:
            return

        sep = "[dim]+-----+------------+------------+---------------+----------+---------------+------------+[/dim]"
        header = "| [bold cyan]TF [/bold cyan] | [bold yellow]MODAL AWAL [/bold yellow] | [bold yellow]MODAL KINI [/bold yellow] | [bold cyan]NET PNL USDT   [/bold cyan] | [bold green]PNL (%) [/bold green] | [bold yellow]FUNDING RIIL   [/bold yellow] | [bold green]ANNUAL APY [/bold green] |"
        title = "[bold cyan]|  REKAP PNL PORTOFOLIO MULTI-TIMEFRAME (DATA REAL BITGET API - ZERO MOCK)              |[/bold cyan]"

        lines = [
            "\n" + sep,
            title,
            sep,
            header,
            sep
        ]

        for label in ["1D", "1W", "1M", "2M", "4M", "1Y"]:
            data = pnl_data.get(label, {})
            if not data or not data.get("has_data", False):
                lines.append(f"| [cyan]{label:<3}[/cyan] | {'Belum Ada':>10} | {'-':>10} | {'-':>13} | {'-':>8} | {'-':>13} | {'-':>10} |")
            else:
                start_eq = f"${data.get('start_equity_usdt', 0.0):,.2f}"
                curr_eq = f"${data.get('current_equity_usdt', 0.0):,.2f}"
                pnl_val = data.get("pnl_usdt", 0.0)
                pct_val = data.get("pnl_pct", 0.0)
                pnl_u = f"${pnl_val:+.4f}"
                pnl_p = f"{pct_val:+.2f}%"
                fund_h = f"+${data.get('funding_harvested_usdt', 0.0):.4f}"
                ann_y = f"{data.get('annualized_yield_pct', 0.0):+.1f}%"
                is_pos = pnl_val >= 0.0

                c_tf   = f"[cyan]{label:<3}[/cyan]"
                c_seq  = f"[white]{start_eq:>10}[/white]"
                c_ceq  = f"[bold white]{curr_eq:>10}[/bold white]"
                c_pnlu = f"[bold green]{pnl_u:>13}[/bold green]" if is_pos else f"[bold red]{pnl_u:>13}[/bold red]"
                c_pnlp = f"[bold green]{pnl_p:>8}[/bold green]" if is_pos else f"[bold red]{pnl_p:>8}[/bold red]"
                c_fund = f"[bold yellow]{fund_h:>13}[/bold yellow]"
                c_apy  = f"[bold green]{ann_y:>10}[/bold green]" if is_pos else f"[bold red]{ann_y:>10}[/bold red]"

                lines.append(f"| {c_tf} | {c_seq} | {c_ceq} | {c_pnlu} | {c_pnlp} | {c_fund} | {c_apy} |")

        all_time = pnl_data.get("all_time", {})
        if all_time:
            curr_eq = f"${all_time.get('current_equity_usdt', 0.0):,.2f}"
            tot_fund = f"+${all_time.get('total_funding_harvested_usdt', 0.0):.4f}"
            lines.append(f"| [bold cyan]{'ALL':<3}[/bold cyan] | {' - ':>10} | [bold white]{curr_eq:>10}[/bold white] | {' - ':>13} | {' - ':>8} | [bold yellow]{tot_fund:>13}[/bold yellow] | {' - ':>10} |")

        lines.append(sep)
        print_clean_table(lines)
    except Exception as e:
        log.debug(f"PnL timeframe display notice: {e}")

def display_opportunities(opportunities, top_n: int = 10):
    """Menampilkan tabel ranking koin terbaik Bitget memanjang ringkas penuh warna (Width 86)."""
    if not opportunities:
        return

    # Urutkan koin berdasarkan skor komposit performa kuantitatif (bobot 25% Rate, 25% BEP, 25% speed 2.5%, 15% spread, 10% flip/vol)
    sorted_opps = sorted(
        opportunities,
        key=lambda opp: (
            opp.is_eligible,
            getattr(opp, "composite_performance_score", 0.0),
            opp.net_apy_percent
        ),
        reverse=True
    )

    sep = "[dim]+----+-------+-----------+---------+-------+-------+------------+--------------------+[/dim]"
    header = "| [bold cyan]RK [/bold cyan] | [bold yellow]KOIN [/bold yellow] | [bold cyan]RATE/INT  [/bold cyan] | [bold green]1W YLD  [/bold green] | [bold cyan]BEP   [/bold cyan] | [bold magenta]KE2.5 [/bold magenta] | [bold blue]BEBAS FLIP [/bold blue] | [bold cyan]STATUS KELAYAKAN   [/bold cyan] |"
    title = "[bold cyan]|  HASIL SCANNING & RANKING PELUANG (BOBOT: 25% RATE, 25% BEP, 25% KE 2.5%)          |[/bold cyan]"

    ranking_lines = [
        "\n" + sep,
        title,
        sep,
        header,
        sep
    ]
    for idx, opp in enumerate(sorted_opps[:top_n], 1):
        cycles_day = 24.0 / max(1, opp.funding_interval_hours)
        weekly_gross = (opp.current_funding_rate * cycles_day * 7.0) * 100.0

        # Hitung waktu BEP dalam satuan HARI
        bep_h = abs(opp.break_even_hours)
        bep_d = round(bep_h / 24.0, 1)
        bep_str = f"{bep_d}h" if bep_h < 999 else " - "

        # Hitung waktu menuju target 2.5% mingguan dalam satuan HARI
        rate_per_hour = (opp.current_funding_rate * 100.0) / max(1, opp.funding_interval_hours)
        hours_to_2_5 = abs(round(2.5 / rate_per_hour, 1)) if rate_per_hour > 0 else 999.0
        days_to_2_5 = round(hours_to_2_5 / 24.0, 1)
        time_to_target_str = f"{days_to_2_5}h" if hours_to_2_5 < 999 else " - "

        # Hitung riwayat flip bebas (hingga 365 hari / 1 tahun atau sejak listing untuk koin baru)
        st = getattr(opp, "historical_120d_stats", None)
        flip_free = getattr(st, "flip_free_days", 365.0) if st else 365.0
        days_data = getattr(st, "days_of_data", 365.0) if st else 365.0
        flip_count = getattr(st, "negative_flip_count", 0) if st else 0

        if flip_count == 0:
            if days_data >= 360:
                flip_str = "365h(Bebas)"
            else:
                flip_str = f"{int(days_data)}h(Baru)"
        else:
            if flip_free >= 30:
                flip_str = f"{int(flip_free)}h(Aman)"
            else:
                flip_str = f"{int(flip_free)}h({flip_count}x)"

        # Status kelayakan & Alasan Jelas
        is_ok = opp.is_eligible
        if is_ok:
            status_text = f"Sp{opp.basis_spread_percent:+.2f}%"
            c_stat = f"[bold green]🟢 LAYAK[/bold green] [green]{status_text:<9}[/green]"
        else:
            r_reason = (getattr(opp, "rejection_reason", "") or "").lower()
            if "flip" in r_reason or "negatif" in r_reason:
                status_text = f"Flip({flip_count}x)"
            elif "volume" in r_reason:
                status_text = "Vol <$10k"
            elif "spread" in r_reason:
                status_text = "Sprd Neg"
            elif "prediksi" in r_reason:
                status_text = "Pred Neg"
            else:
                raw_r = getattr(opp, "rejection_reason", "Filter")
                status_text = f"{raw_r[:8]}"
            c_stat = f"[bold red]🔴 TIDAK[/bold red] [red]{status_text:<9}[/red]"

        rate_str = f"{opp.current_funding_rate*100:+.3f}%/{opp.funding_interval_hours}h"
        gross_str = f"{weekly_gross:+.2f}%"

        c_idx   = f"[bold cyan]#{idx:<2}[/bold cyan]"
        c_coin  = f"[bold yellow]{opp.base_asset:<5}[/bold yellow]"
        c_rate  = f"[green]{rate_str:<9}[/green]" if is_ok else f"[red]{rate_str:<9}[/red]"
        c_yld   = f"[bold green]{gross_str:>7}[/bold green]" if is_ok else f"[red]{gross_str:>7}[/red]"
        c_bep   = f"[cyan]{bep_str:>5}[/cyan]"
        c_ke25  = f"[bold magenta]{time_to_target_str:>5}[/bold magenta]"
        c_flip  = f"[bold blue]{flip_str:^10}[/bold blue]"

        row = f"| {c_idx} | {c_coin} | {c_rate} | {c_yld} | {c_bep} | {c_ke25} | {c_flip} | {c_stat} |"
        ranking_lines.append(row)

    ranking_lines.append(sep)
    print_clean_table(ranking_lines)

def display_active_positions():
    """Menampilkan telemetri portofolio posisi aktif gaya Excel penuh warna (Width 88-89, Anti-Wrap & Zero Truncation)."""
    active_positions = position_manager.get_active_positions()
    if not active_positions:
        log.info("📊 [Portofolio] Belum ada posisi Delta-Neutral yang aktif saat ini. Modal cair siap dialokasikan.")
        return

    from utils.interval_helper import safe_hours_passed

    sep_overview = "[dim]+--------+-------------------------+-------------------------+-------------------------+[/dim]"
    hdr_overview = "| [bold yellow]KOIN   [/bold yellow] | [bold cyan]ENTRY (SPOT / PERP)     [/bold cyan] | [bold yellow]HARGA KINI & SPREAD     [/bold yellow] | [bold magenta]STATUS BEP & PROGRESS   [/bold magenta] |"
    title_overview = "[bold cyan]|  RINGKASAN POSISI DELTA-NEUTRAL BERJALAN (GROUND-TRUTH BITGET)                       |[/bold cyan]"

    overview_lines = [
        "\n" + sep_overview,
        title_overview,
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
            bep_card_desc = f"SUDAH BEP LUNAS (+${port_net:.4f} USDT)"
        elif rate_per_hour_usdt > 0 and port_net < 0:
            hours_to_bep = abs(port_net) / rate_per_hour_usdt
            days_to_bep = abs(round(hours_to_bep / 24.0, 1))
            kapan_bep_str = f"{days_to_bep} hari ({abs(hours_to_bep):.1f} jam)"
            status_bep_short = f"BEP:{days_to_bep}h"
            bep_card_desc = f"{days_to_bep} hari ({abs(hours_to_bep):.1f}j) pd rate ini"
        else:
            kapan_bep_str = "Menunggu Settle"
            status_bep_short = "Wait Settle"
            bep_card_desc = "Menunggu settlement funding berikutnya"

        # 3. Estimasi Kapan Nyampe 2.5% per Minggu (Dalam Satuan HARI & Jam)
        target_weekly_profit_usdt = round(baseline * (settings.TARGET_WEEKLY_NET_YIELD_PERCENT / 100.0), 2)
        progress_to_target_pct = (port_net / target_weekly_profit_usdt * 100.0) if target_weekly_profit_usdt > 0 and port_net > 0 else 0.0
        profit_gap_usdt = max(0.0, target_weekly_profit_usdt - port_net)

        if port_net >= target_weekly_profit_usdt:
            kapan_target_str = "TARGET 2.5% TERCAPAI"
            progress_short = "+2.5% [LUNAS]"
            target_card_desc = f"TARGET 2.5% TERCAPAI (+${port_net:.4f} USDT)"
        elif rate_per_hour_usdt > 0 and profit_gap_usdt > 0:
            hours_to_target = profit_gap_usdt / rate_per_hour_usdt
            days_to_target = abs(round(hours_to_target / 24.0, 1))
            kapan_target_str = f"{days_to_target} hari ({abs(hours_to_target):.0f} jam)"
            progress_short = f"{current_pct_vs_baseline:+.2f}% ({days_to_target}h)"
            target_card_desc = f"{days_to_target} hari ({abs(hours_to_target):.0f}j) ke target +${target_weekly_profit_usdt:.2f}"
        else:
            kapan_target_str = "Akumulasi Bunga"
            progress_short = f"{current_pct_vs_baseline:+.2f}%"
            target_card_desc = f"Akumulasi bunga (Sisa: +${profit_gap_usdt:.4f} USDT)"

        # 4. Basis Spread Live
        basis_spread = ((pos.perp_leg.current_price - pos.spot_leg.current_price) / pos.spot_leg.current_price * 100.0) if pos.spot_leg.current_price > 0 else 0.0
        spread_status = "AMAN" if basis_spread >= 0 else "CONV"

        # Overview row format (Total width 88)
        entry_short = f"S:${pos.spot_leg.entry_price:.4f} P:${pos.perp_leg.entry_price:.4f}"
        price_spread_short = f"${pos.spot_leg.current_price:.4f}/${pos.perp_leg.current_price:.4f} ({basis_spread:+.2f}%)"
        status_prog_short = f"{status_bep_short} | {progress_short}"

        overview_row = (
            f"| [bold yellow]{pos.base_asset:<6}[/bold yellow] "
            f"| [cyan]{entry_short:<23}[/cyan] "
            f"| [yellow]{price_spread_short:<23}[/yellow] "
            f"| [bold magenta]{status_prog_short:<23}[/bold magenta] |"
        )
        overview_lines.append(overview_row)

        # Detailed Excel Property Card (Width 89)
        sep_card = "[dim]+----------------------------+----------------------------------------------------------+[/dim]"
        title_card = f"[bold cyan]|  RINCIAN METRIK: {pos.base_asset} (FOKUS 1 KOIN 1x ISOLATED){'':<43} |[/bold cyan]"
        header_card = "| [bold cyan]PARAMETER / METRIK          [/bold cyan] | [bold yellow]NILAI REAL & TELEMETRI GROUND-TRUTH                        [/bold yellow] |"

        card_rows = [
            ("Koin & Mode Trading", f"{pos.base_asset} (Spot Beli + Short Perp 1x Isolated)", "bold yellow"),
            ("Entry Spot (Beli)", f"{pos.spot_leg.amount:.2f} @ ${pos.spot_leg.entry_price:.4f} (${spot_nominal:.2f} USDT)", "green"),
            ("Entry Perp (Short)", f"{pos.perp_leg.amount:.2f} @ ${pos.perp_leg.entry_price:.4f} (${perp_nominal:.2f} USDT)", "magenta"),
            ("Harga & Basis Spread", f"S:${pos.spot_leg.current_price:.4f} P:${pos.perp_leg.current_price:.4f} ({basis_spread:+.2f}% [{spread_status}])", "bold green"),
            ("Status BEP Akun", f"{'[SUDAH BEP]' if is_port_bep else '[MENUJU BEP]'} Impas: ${abs(port_net):.4f} USDT Net", "yellow"),
            ("Estimasi Waktu ke BEP", bep_card_desc[:56], "cyan"),
            ("Progress vs Modal ($64)", f"{current_pct_vs_baseline:+.2f}% vs ${baseline:.2f} (Target 1W: +{settings.TARGET_WEEKLY_NET_YIELD_PERCENT:.1f}%)", "magenta"),
            ("Waktu ke Target 2.5%", target_card_desc[:56], "bold magenta"),
            ("Funding Fee Dipanen", f"+${pos.cumulative_funding_received:.4f} USDT (Ground-Truth Bitget)", "bold green"),
            ("Tingkat Funding", f"{pos.last_funding_rate*100:+.4f}%/{pos.funding_interval_hours}h (+${rate_per_hour_usdt*pos.funding_interval_hours:.4f}/siklus)", "green"),
            ("Margin & ROE Futures", f"MMR: {pos.current_margin_ratio:.1%} (Aman <85%) | ROE: {roe_val:+.2f}%", "cyan"),
        ]

        card_lines.extend([
            "\n" + sep_card,
            title_card,
            sep_card,
            header_card,
            sep_card
        ])

        for label, val, v_color in card_rows:
            card_lines.append(f"| [cyan]{label:<26}[/cyan] | [{v_color}]{val:<56}[/{v_color}] |")

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
                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S WIB')
                log_separator(f"SIKLUS SCAN BARU — {now_str}")
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

                # ⭐ KUNCI: Sinkronisasi modal dari saldo REAL Bitget
                # Fix #1: Pass vault_locked agar deposit detection tidak false positive setelah TP
                _vault_locked = getattr(yield_vault, "total_locked_usdt", 0.0)
                compounding_manager.sync_from_real_balance(
                    real_liquid_usdt=total_trading_equity if total_trading_equity > 0 else total_bal,
                    vault_locked_usdt=_vault_locked
                )

                # ⭐ Fix #3: Auto-reinvest dormant TP (jika tidak ditarik dalam 7 hari → compound kembali)
                try:
                    monthly_tp_manager.check_and_auto_reinvest(
                        vault=yield_vault,
                        compounding_manager_ref=compounding_manager,
                        db=db
                    )
                except Exception as _ar_err:
                    log.debug(f"[AutoReinvest] Notice: {_ar_err}")

                # ⭐ MONTHLY TP: Evaluasi siklus 30-hari Take Profit 10% (Capital Floor Guard 1.5%)
                try:
                    _tp_baseline = compounding_manager.initial_seed if compounding_manager.initial_seed > 0 else 64.0
                    _tp_total_balance = total_trading_equity if total_trading_equity > 0 else total_bal
                    _tp_result = monthly_tp_manager.evaluate_and_execute(
                        total_balance_usdt=_tp_total_balance,
                        baseline_capital_usdt=_tp_baseline,
                        db=db,
                        vault=yield_vault
                    )
                    # Fix #1: Jika TP berhasil dieksekusi, update accounting compounding
                    if _tp_result.get("executed") and _tp_result.get("amount_usdt", 0) > 0:
                        compounding_manager.deduct_vaulted_profit(_tp_result["amount_usdt"])
                except Exception as _tp_err:
                    log.debug(f"[MonthlyTP] evaluate_and_execute notice: {_tp_err}")

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
