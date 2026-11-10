"""Test logger initialization and file rotation."""
from pathlib import Path
from configs.logger import logger, LOGS_DIR


def test_logger_writes_to_file():
    """Verify logger creates logs and records messages."""
    test_msg = "Artha Quant Logger initialized successfully."
    logger.info(test_msg)

    # Check that logs directory exists
    assert LOGS_DIR.exists()
    assert LOGS_DIR.is_dir()

    # Find the log file created today
    log_files = list(LOGS_DIR.glob("artha_*.log"))
    assert len(log_files) > 0

    # Verify content in the active log file
    with open(log_files[0], "r", encoding="utf-8") as f:
        content = f.read()
        assert test_msg in content
