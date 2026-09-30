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
console_width = min(76, int(os.getenv("CONSOLE_WIDTH", "76")))
is_cloud = bool(os.getenv("RENDER") or os.getenv("RAILWAY_ENVIRONMENT") or not sys.stdout.isatty())

# Aktifkan warna standar ANSI dan batas lebar ketat 76 kolom agar log di Render tidak wrap
console = Console(width=console_width, force_terminal=True, color_system="standard", soft_wrap=True)

def print_clean_table(lines: list):
    """Mencetak baris tabel presisi langsung ke stdout agar perataan kolom 100% sempurna tanpa indentasi atau wrapping."""
    for line in lines:
        print(line, flush=True)

def setup_logger(name: str = "delta_neutral", log_file: str = "bot.log") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        # Rich Console Handler - Bersih tanpa prefix ganda di cloud logs
        rich_handler = RichHandler(
            console=console,
            show_time=not is_cloud,  # Cloud Render/Railway sudah memiliki timestamp otomatis
            show_path=False,
            rich_tracebacks=True,
            markup=False,  # Jangan interpretasikan tanda kurung siku [...] sebagai markup agar teks tidak hilang
            omit_repeated_times=False,
            keywords=[]
        )
        rich_handler.setLevel(logging.INFO)
        
        # File Handler
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
