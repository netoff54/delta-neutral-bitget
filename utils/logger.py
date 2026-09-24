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
console_width = int(os.getenv("CONSOLE_WIDTH", "160"))
console = Console(width=console_width, force_terminal=True, soft_wrap=True)

def setup_logger(name: str = "delta_neutral", log_file: str = "bot.log") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        # Rich Console Handler
        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True
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
