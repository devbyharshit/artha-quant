"""Centralized logging framework for Artha Quant using Loguru."""
import sys
from pathlib import Path
from loguru import logger
from configs.config import BASE_DIR

LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Remove default handlers
logger.remove()

# 1. Standard Output Sink (Formatted, colored)
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    colorize=True,
)

# 2. Daily Rotating Log File (All debug info, 30 days retention, compressed)
logger.add(
    str(LOGS_DIR / "artha_{time:YYYY-MM-DD}.log"),
    rotation="00:00",
    retention="30 days",
    compression="zip",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {process} | {name}:{function}:{line} - {message}",
    backtrace=True,
    diagnose=True,
)

__all__ = ["logger"]
