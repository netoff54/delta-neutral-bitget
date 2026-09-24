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
from utils.logger import log, console
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
    """
    Menampilkan tabel PnL portofolio untuk semua timeframe (1D, 1W, 1M, 1Y).
    Menggunakan data REAL dari Bitget yang tersimpan di database.
    Tidak ada data fiktif/mock sama sekali.
    """
    try:
        pnl_data = db.get_all_pnl_timeframes()

        table = Table(
            title="📈 Analisis PnL Portofolio Multi-Timeframe (Data REAL Bitget)",
            box=box.ROUNDED,
            header_style="bold cyan",
            expand=False
        )
        table.add_column("Timeframe", style="bold white", justify="center", no_wrap=True)
        table.add_column("Modal Awal", style="yellow", justify="right", no_wrap=True)
        table.add_column("Modal Kini", style="bold green", justify="right", no_wrap=True)
        table.add_column("PnL (USDT)", justify="right", no_wrap=True)
        table.add_column("PnL (%)", justify="right", no_wrap=True)
        table.add_column("Funding Harvested", style="green", justify="right", no_wrap=True)
        table.add_column("Avg/Hari", style="cyan", justify="right", no_wrap=True)
        table.add_column("Annualized", style="bold magenta", justify="right", no_wrap=True)

        for label in ["1D", "1W", "1M", "1Y"]:
            data = pnl_data.get(label, {})
            if not data:
                continue

            has_data = data.get("has_data", False)
            pnl_usdt = data.get("pnl_usdt", 0.0)
            pnl_pct = data.get("pnl_pct", 0.0)

            pnl_color = "green" if pnl_usdt >= 0 else "red"
            pct_color = "green" if pnl_pct >= 0 else "red"

            if not has_data:
                table.add_row(
                    label,
                    "[dim]Belum ada data[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]"
                )
            else:
                table.add_row(
                    f"[bold]{label}[/bold]",
                    f"${data.get('start_equity_usdt', 0.0):,.2f}",
                    f"[bold green]${data.get('current_equity_usdt', 0.0):,.2f}[/bold green]",
                    f"[{pnl_color}]${pnl_usdt:+.4f}[/{pnl_color}]",
                    f"[{pct_color}]{pnl_pct:+.2f}%[/{pct_color}]",
                    f"+${data.get('funding_harvested_usdt', 0.0):.4f}",
                    f"${data.get('avg_daily_pnl_usdt', 0.0):+.4f}",
                    f"[bold]{data.get('annualized_yield_pct', 0.0):+.1f}%[/bold]"
                )

        # Row all-time
        all_time = pnl_data.get("all_time", {})
        if all_time:
            table.add_row(
                "[bold yellow]All Time[/bold yellow]",
                "[dim]-[/dim]",
                f"[bold green]${all_time.get('current_equity_usdt', 0.0):,.2f}[/bold green]",
                "[dim]-[/dim]",
                "[dim]-[/dim]",
                f"[bold yellow]+${all_time.get('total_funding_harvested_usdt', 0.0):.4f}[/bold yellow]",
                "[dim]-[/dim]",
                "[dim]-[/dim]"
            )

        console.print(table)
    except Exception as e:
        log.debug(f"PnL timeframe display notice: {e}")

def display_opportunities(opportunities, top_n: int = 10):
    """Menampilkan tabel hasil pemindaian peluang dengan analisis kuantitatif 30 hari dari Bitget & biaya taker."""
    table = Table(
        title=f"Hasil Analisis Pasar & Prediksi Funding Rate (Top {top_n} Peluang - Bitget 30D Data)",
        box=box.ROUNDED,
        header_style="bold magenta",
        expand=False
    )

    table.add_column("Koin", style="cyan", justify="left", no_wrap=True)
    table.add_column("Int", style="magenta", justify="center", no_wrap=True)
    table.add_column("Rate/Siklus", style="green", justify="right", no_wrap=True)
    table.add_column("Next Rate", style="bright_green", justify="right", no_wrap=True)
    table.add_column("Konsistensi", style="yellow", justify="right", no_wrap=True)
    table.add_column("30D APY", style="bold green", justify="right", no_wrap=True)
    table.add_column("30D Flip", style="bright_white", justify="center", no_wrap=True)
    table.add_column("Taker Impas", style="bright_yellow", justify="right", no_wrap=True)
    table.add_column("Net APY", style="bold green", justify="right", no_wrap=True)
    table.add_column("Skor", style="bold cyan", justify="right", no_wrap=True)
    table.add_column("Status / Kelayakan", justify="left", no_wrap=True)

    for opp in opportunities[:top_n]:
        if opp.is_eligible:
            status_text = "[bold green][OK] LAYAK[/bold green]"
        else:
            reason = opp.rejection_reason or "Tidak lolos"
            if "Modal" in reason and "Min Capital" in reason:
                status_text = "[dim red][X] Min Cap ($10)[/dim red]"
            elif "Nominal kaki" in reason or "minimal order" in reason:
                status_text = "[dim red][X] Min Order[/dim red]"
            elif "Volume 24h" in reason:
                status_text = "[dim red][X] Vol Rendah[/dim red]"
            elif "Waktu impas" in reason:
                status_text = "[dim red][X] Impas > 72h[/dim red]"
            elif "Net APY" in reason:
                status_text = "[dim red][X] APY < 15%[/dim red]"
            elif "Funding rate bernilai negatif" in reason:
                status_text = "[dim red][X] Rate Negatif[/dim red]"
            else:
                status_text = f"[dim red][X] {reason[:18]}[/dim red]"

        stats = opp.historical_30d_stats or opp.historical_7d_stats
        y30d_str = f"{stats.thirty_day_apy_pct:+.1f}%" if stats and stats.sample_count > 0 else "[dim]-[/dim]"
        flip_str = f"[green]{stats.thirty_day_flip_count}x[/green]" if stats and stats.thirty_day_flip_count == 0 else (f"[red]{stats.thirty_day_flip_count}x[/red]" if stats else "[dim]-[/dim]")
        taker_be_str = f"{stats.taker_fee_recovery_hours:.1f}h" if stats and stats.taker_fee_recovery_hours < 999 else f"{opp.break_even_hours:.1f}h"

        table.add_row(
            opp.base_asset,
            f"{opp.funding_interval_hours}h",
            f"{opp.current_funding_rate * 100:.4f}%",
            f"{opp.predicted_next_funding_rate * 100:.4f}%",
            f"{opp.consistency_score_percent:.0f}%",
            y30d_str,
            flip_str,
            taker_be_str,
            f"{opp.net_apy_percent:.1f}%",
            f"{opp.composite_performance_score:.1f}",
            status_text
        )

    console.print(table)

def display_active_positions():
    """Menampilkan tabel posisi aktif saat ini dengan pemantauan riil Funding Fee, Net PnL, dan status BEP."""
    active_positions = position_manager.get_active_positions()
    if not active_positions:
        console.print("[italic dim]Belum ada posisi Delta-Neutral yang aktif saat ini.[/italic dim]\n")
        return

    table = Table(
        title=f"Posisi Delta-Neutral Aktif ({len(active_positions)}) - Status BEP & Net PnL Riil",
        box=box.ROUNDED,
        header_style="bold cyan",
        expand=False
    )

    table.add_column("Koin", style="bold yellow", justify="left", no_wrap=True)
    table.add_column("Ukuran Posisi", style="white", justify="right", no_wrap=True)
    table.add_column("Spot (Beli/Kini)", style="green", justify="right", no_wrap=True)
    table.add_column("Perp (Jual/Kini)", style="magenta", justify="right", no_wrap=True)
    table.add_column("Funding Panen", style="bold green", justify="right", no_wrap=True)
    table.add_column("Unrealized PnL", style="white", justify="right", no_wrap=True)
    table.add_column("Net PnL", style="bold white", justify="right", no_wrap=True)
    table.add_column("Status BEP", justify="center", no_wrap=True)
    table.add_column("Margin Ratio", style="bold red", justify="right", no_wrap=True)
    table.add_column("Durasi", style="cyan", justify="right", no_wrap=True)

    from utils.interval_helper import safe_hours_passed

    for pos in active_positions:
        pnl_color = "green" if pos.net_pnl_usdt >= 0 else "red"
        unreal_color = "green" if pos.unrealized_pnl_usdt >= 0 else "yellow"
        bep_badge = "[bold green]BEP Tercapai[/bold green]" if pos.is_bep_reached else f"[bold yellow]Menuju BEP ({pos.net_pnl_usdt:+.4f})[/bold yellow]"
        holding_h = safe_hours_passed(pos.entry_time) if hasattr(pos, "entry_time") else 0.0

        margin_color = "red" if pos.current_margin_ratio >= 0.85 else ("yellow" if pos.current_margin_ratio >= 0.50 else "green")
        margin_str = f"[{margin_color}]{pos.current_margin_ratio:.1%} (Max 95%)[/{margin_color}]"

        spot_nominal = getattr(pos.spot_leg, "nominal_usdt", 0.0) or (pos.spot_leg.amount * pos.spot_leg.entry_price)

        table.add_row(
            pos.base_asset,
            f"{pos.spot_leg.amount:.2f} {pos.base_asset} (~${spot_nominal:.1f})",
            f"${pos.spot_leg.entry_price:,.4f} / ${pos.spot_leg.current_price:,.4f}",
            f"${pos.perp_leg.entry_price:,.4f} / ${pos.perp_leg.current_price:,.4f}",
            f"+${pos.cumulative_funding_received:.4f}",
            f"[{unreal_color}]${pos.unrealized_pnl_usdt:+.4f}[/{unreal_color}]",
            f"[{pnl_color}]${pos.net_pnl_usdt:+.4f}[/{pnl_color}]",
            bep_badge,
            margin_str,
            f"{holding_h:.1f} jam"
        )

    console.print(table)

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
            tf_map = {"1d": 1, "1w": 7, "1m": 30, "1y": 365, "7": 7, "30": 30, "365": 365}
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
                        log.info(f"🏓 [Self-Ping] OK ({resp.status}) → {ping_url}")
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
