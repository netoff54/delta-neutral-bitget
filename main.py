import asyncio
import argparse
import sys
import os
import gc
from datetime import datetime, timezone

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

def display_compounding_pool(total_liquid_usdt: float, spot_free: float = 0.0, swap_free: float = 0.0, otc_free: float = 0.0):
    """Menampilkan status saldo trading cair, saldo OTC, compounding profit, dan proteksi saldo Bitget Earn."""
    summary = compounding_manager.get_summary()
    usable_capital = compounding_manager.get_position_capital(total_liquid_usdt)

    if settings.USE_ALL_AVAILABLE_BALANCE:
        otc_info = f" | OTC (Pendanaan): ${otc_free:,.2f}" if settings.INCLUDE_OTC_BALANCE else ""
        console.print(Panel(
            f"[bold cyan]💼 TOTAL SALDO CAIR (SPOT + FUTURES + OTC):[/bold cyan] [bold green]${total_liquid_usdt:,.2f} USDT[/bold green] "
            f"[dim](Spot: ${spot_free:,.2f} | Futures: ${swap_free:,.2f}{otc_info})[/dim]\n"
            f"[bold green]🚀 MODAL SIAP DITRADINGKAN (BUFFER 2%):[/bold green] [bold white]${usable_capital:,.2f} USDT[/bold white] "
            f"[dim](Per Posisi: ${usable_capital / max(1, settings.MAX_CONCURRENT_POSITIONS):,.2f} USDT | Max Lev: {settings.LEVERAGE}x)[/dim]\n"
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
        insight = gemini_brain.get_latest_insight()
        mem_count = len(gemini_brain.memory)
        quota = gemini_brain.get_quota_status()
        console.print(Panel(
            f"[bold magenta]🧠 AI BRAIN ENGINE:[/bold magenta] [bold cyan]{settings.GEMINI_MODEL}[/bold cyan] "
            f"[dim]({mem_count} siklus pembelajaran tersimpan di disk)[/dim]\n"
            f"[bold green]📊 ALOKASI KUOTA 50% (REAL-TIME 2M):[/bold green] [bold white]{quota['calls_today']}/{quota['max_daily_calls']} calls/hari[/bold white] "
            f"[dim](Target: ~{quota['budget_percent']}% dari plafon resmi {quota['official_rpd']} RPD Google)[/dim]\n"
            f"[bold yellow]💡 KEPUTUSAN & INSIGHT TAKTIS TERKINI:[/bold yellow]\n[italic white]{insight}[/italic white]",
            title="[bold magenta]Google Gemini AI Adaptive Real-Time Brain[/bold magenta]",
            box=box.ROUNDED
        ))
    except Exception as e:
        log.debug(f"AI Brain status display notice: {e}")

def display_opportunities(opportunities, top_n: int = 10):
    """Menampilkan tabel hasil pemindaian peluang dengan analisis prediktif."""
    table = Table(
        title=f"Hasil Analisis Pasar & Prediksi Funding Rate (Top {top_n} Peluang)",
        box=box.ROUNDED,
        header_style="bold magenta"
    )

    table.add_column("Koin", style="cyan", justify="left")
    table.add_column("Interval", style="magenta", justify="center")
    table.add_column("Rate / Siklus", style="green", justify="right")
    table.add_column("Prediksi Next", style="bright_green", justify="right")
    table.add_column("Konsistensi", style="yellow", justify="right")
    table.add_column("Tren", style="white", justify="center")
    table.add_column("Net APY", style="bold green", justify="right")
    table.add_column("Impas (BE)", style="bright_yellow", justify="right")
    table.add_column("Skor Data", style="bold cyan", justify="right")
    table.add_column("Vol 24h (Spot/Perp)", style="dim", justify="right")
    table.add_column("Status / Kelayakan", justify="left")

    for opp in opportunities[:top_n]:
        status_text = (
            "[bold green][OK] LAYAK[/bold green]"
            if opp.is_eligible
            else f"[dim red][X] {opp.rejection_reason}[/dim red]"
        )

        trend_color = "green" if opp.funding_trend == "UP" else ("red" if opp.funding_trend == "DOWN" else "yellow")

        table.add_row(
            opp.base_asset,
            f"{opp.funding_interval_hours}h",
            f"{opp.current_funding_rate * 100:.4f}%",
            f"{opp.predicted_next_funding_rate * 100:.4f}%",
            f"{opp.consistency_score_percent:.0f}%",
            f"[{trend_color}]{opp.funding_trend}[/{trend_color}]",
            f"{opp.net_apy_percent:.1f}%",
            f"{opp.break_even_hours:.1f}h",
            f"{opp.composite_performance_score:.1f}",
            f"${opp.spot_volume_24h/1e6:.1f}M / ${opp.perp_volume_24h/1e6:.1f}M",
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
        box=box.HEAVY_EDGE,
        header_style="bold cyan"
    )

    table.add_column("ID Posisi", style="dim", justify="left")
    table.add_column("Koin", style="bold yellow", justify="left")
    table.add_column("Kuantitas", style="white", justify="right")
    table.add_column("Spot (Entry/Now)", style="green", justify="right")
    table.add_column("Perp (Entry/Now)", style="magenta", justify="right")
    table.add_column("Real Funding", style="bold green", justify="right")
    table.add_column("Unrealized PnL", style="white", justify="right")
    table.add_column("Net PnL", style="bold white", justify="right")
    table.add_column("Status BEP", justify="center")
    table.add_column("Proyeksi Next", style="bright_cyan", justify="right")
    table.add_column("Margin Ratio", style="bold red", justify="right")
    table.add_column("Status", style="bold green", justify="center")

    for pos in active_positions:
        pnl_color = "green" if pos.net_pnl_usdt >= 0 else "red"
        unreal_color = "green" if pos.unrealized_pnl_usdt >= 0 else "yellow"
        bep_badge = "[bold green]✅ BEP Tercapai[/bold green]" if pos.is_bep_reached else f"[bold yellow]⏳ Menuju BEP ({pos.net_pnl_usdt:+.4f})[/bold yellow]"
        interval = getattr(pos, "funding_interval_hours", 8) or 8
        proj_badge = f"+${pos.projected_next_funding_payout:.4f}\n[dim]({interval}h)[/dim]" if pos.projected_next_funding_payout > 0 else "[dim]-[/dim]"

        table.add_row(
            pos.position_id,
            pos.base_asset,
            f"{pos.spot_leg.amount:.4f}",
            f"${pos.spot_leg.entry_price:,.2f} / ${pos.spot_leg.current_price:,.2f}",
            f"${pos.perp_leg.entry_price:,.2f} / ${pos.perp_leg.current_price:,.2f}",
            f"+${pos.cumulative_funding_received:.4f}",
            f"[{unreal_color}]${pos.unrealized_pnl_usdt:+.4f}[/{unreal_color}]",
            f"[{pnl_color}]${pos.net_pnl_usdt:+.4f}[/{pnl_color}]",
            bep_badge,
            proj_badge,
            f"{pos.current_margin_ratio:.1%}",
            pos.status
        )

    console.print(table)

async def start_health_check_server():
    """Server HTTP mini untuk Render/Cloud Health Check agar bot terdeteksi LIVE dan tidak tidur."""
    try:
        from aiohttp import web
        app = web.Application()

        async def handle_health(request):
            active_cnt = len(position_manager.get_active_positions())
            return web.json_response({
                "status": "healthy",
                "service": "Delta-Neutral Bitget Autonomous Agent",
                "active_positions": active_cnt,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

        app.router.add_get("/", handle_health)
        app.router.add_get("/health", handle_health)

        port = int(os.getenv("PORT", "10000"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info(f"🌐 [Health Server] Mini HTTP server aktif di port {port} (Render Cloud health check).")
        return runner
    except Exception as e:
        log.warning(f"Tidak dapat memulai health check server: {e}")
        return None

async def run_autonomous_loop(auto_trade: bool, manual_capital: float = None):
    """Loop otonom: Memindai pasar, memprediksi yield, auto-rebalance, dan memutar profit (compounding)."""
    await bitget_client.initialize()
    health_runner = await start_health_check_server()

    try:
        while True:
            if sys.stdout.isatty() and not os.getenv("RAILWAY_ENVIRONMENT") and not os.getenv("RENDER"):
                console.clear()
            print_banner(settings.DRY_RUN)
            console.print(f"[dim]Waktu Pemeriksaan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]\n")

            # 0. Ambil saldo akun dan tampilkan Portfolio Pool & Proteksi Earn
            bal = await bitget_client.fetch_balance()
            total_bal = float(bal.get("total", {}).get("USDT", 0.0) or bal.get("USDT", {}).get("total", 0.0) or (settings.TOTAL_MAX_CAPITAL_USDT or 0.0))
            spot_free = float(bal.get("spot_free", 0.0))
            swap_free = float(bal.get("swap_free", 0.0))
            otc_free = float(bal.get("otc_free", 0.0))
            display_compounding_pool(total_bal, spot_free, swap_free, otc_free)
            display_ai_brain_status()

            # Hitung modal trading dinamis yang dialokasikan (seluruh saldo cair atau manual override)
            current_compounded_cap = manual_capital if manual_capital else compounding_manager.get_position_capital(total_bal)

            # 1. Health & Risk Check pada posisi aktif
            log.info("Menjalankan pemeriksaan risiko (Margin Guard & Funding Guard)...")
            await margin_guard.check_positions_health()
            await funding_guard.check_positions_funding()

            # 2. Auto-Rebalancing Delta Drift
            await auto_rebalancer.rebalance_delta_drift()

            # Tampilkan posisi aktif
            display_active_positions()

            # 3. Pindai dan evaluasi seluruh pasar secara prediktif dengan modal ter-compound
            opportunities = await opportunity_finder.scan(target_nominal_usdt=current_compounded_cap)
            display_opportunities(opportunities, top_n=10)

            # 3.5. Evaluasi Real-Time Ground-Truth oleh Gemini AGI (Alokasi 50% Kuota = 720 calls/hari)
            if settings.ENABLE_AI_BRAIN and getattr(settings, "AI_REALTIME_EVALUATION", True):
                from core.gemini_brain import gemini_brain
                active_pos_list = position_manager.get_active_positions()
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
                capital_per_position=current_compounded_cap
            )

            # 5. Keputusan Eksekusi Pembukaan Posisi Baru
            eligible_opportunities = [o for o in opportunities if o.is_eligible]
            active_count = len(position_manager.get_active_positions())

            # Batas posisi aktif: default MAX_CONCURRENT_POSITIONS (1 posisi agar modal fokus 100%)
            max_allowed_positions = settings.MAX_CONCURRENT_POSITIONS

            if auto_trade and eligible_opportunities:
                if active_count < max_allowed_positions:
                    from core.gemini_brain import gemini_brain
                    best_opp = await gemini_brain.select_optimal_taker_agi(eligible_opportunities, current_compounded_cap) or eligible_opportunities[0]
                    if not position_manager.get_position_by_base(best_opp.base_asset):
                        console.print(
                            f"\n[bold green]🎯 Peluang Terbaik Terpilih AGI: {best_opp.base_asset} "
                            f"(Skor: {best_opp.composite_performance_score:.1f}, Prediksi Next: {best_opp.predicted_next_funding_rate*100:.4f}%, "
                            f"Net APY: {best_opp.net_apy_percent:.1f}%).\n"
                            f"Mengeksekusi posisi Delta-Neutral dengan Alokasi Modal Cair: ${current_compounded_cap:.2f} USDT "
                            f"(Max {settings.LEVERAGE}x Leverage)...[/bold green]"
                        )
                        await order_executor.open_delta_neutral_position(
                            opportunity=best_opp,
                            allocated_capital_usdt=current_compounded_cap
                        )
                else:
                    console.print(
                        f"\n[yellow]Kapasitas modal trading terpakai penuh ({active_count}/{max_allowed_positions} posisi aktif). "
                        f"Modal sedang bekerja memanen funding fee.[/yellow]"
                    )

            # Bersihkan memori RAM agar hemat biaya container di cloud (Railway)
            gc.collect()

            console.print(f"\n[dim]Menunggu {settings.SCAN_INTERVAL_SECONDS} detik untuk pemindaian pasar berikutnya (pemantauan posisi aktif tiap {settings.MONITOR_INTERVAL_SECONDS} detik)...[/dim]")
            sleep_remaining = settings.SCAN_INTERVAL_SECONDS
            monitor_interval = getattr(settings, "MONITOR_INTERVAL_SECONDS", 60)
            while sleep_remaining > 0:
                step = min(monitor_interval, sleep_remaining)
                await asyncio.sleep(step)
                sleep_remaining -= step
                if sleep_remaining > 0 and position_manager.get_active_positions():
                    await margin_guard.check_positions_health()
                    await funding_guard.check_positions_funding()

    except asyncio.CancelledError:
        log.info("Autonomous loop dihentikan.")
    finally:
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

    await run_autonomous_loop(auto_trade=args.auto_trade, manual_capital=args.capital)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold red]Bot dihentikan oleh pengguna.[/bold red]")
        sys.exit(0)
