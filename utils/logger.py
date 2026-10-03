import logging
import sys
from rich.logging import RichHandler
from rich.console import Console
from pathlib import Path

import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
import os
console_width = min(92, int(os.getenv("CONSOLE_WIDTH", "92")))
is_cloud = bool(os.getenv("RENDER") or os.getenv("RAILWAY_ENVIRONMENT") or not sys.stdout.isatty())

# Aktifkan warna standar ANSI dan batas lebar 92 kolom agar tabel tidak wrap dan berpenampilan penuh warna
console = Console(width=console_width, force_terminal=True, color_system="standard", soft_wrap=True)

def print_clean_table(lines: list):
    """Mencetak baris tabel presisi dengan Rich console agar warna (Green, Yellow, Cyan, Red, Magenta) muncul di Render logs."""
    for line in lines:
        console.print(line)

def log_separator(label: str = ""):
    """Mencetak garis pemisah siklus scan agar log Render mudah dibaca dan tidak bercampur antar iterasi."""
    sep = "=" * 70
    if label:
        console.print(f"\n[dim]{sep}[/dim]")
        console.print(f"[bold cyan]  {label}[/bold cyan]")
        console.print(f"[dim]{sep}[/dim]\n")
    else:
        console.print(f"\n[dim]{sep}[/dim]\n")

def setup_logger(name: str = "delta_neutral", log_file: str = "bot.log") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        # Rich Console Handler — format ringkas untuk Render (timestamp sudah ada dari Render platform)
        rich_handler = RichHandler(
            console=console,
            show_time=not is_cloud,   # Di cloud Render/Railway timestamp sudah ditambahkan otomatis
            show_path=False,
            rich_tracebacks=True,
            markup=False,             # Jangan interpret [...] sebagai markup agar teks tidak hilang di log cloud
            omit_repeated_times=False,
            keywords=[]
        )
        rich_handler.setLevel(logging.INFO)
        
        # File Handler — simpan semua log lengkap ke bot.log dengan format timestamp penuh
        log_path = Path(log_file)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(file_formatter)
        
        logger.addHandler(rich_handler)
        logger.addHandler(file_handler)
        
    return logger

log = setup_logger()
