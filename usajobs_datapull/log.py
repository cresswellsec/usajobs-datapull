"""Logging: INFO to the console, DEBUG to data/logs/<stage>-<timestamp>.log."""
from __future__ import annotations

import logging
import sys
from datetime import datetime

from . import config

LOGGER = "usajobs_datapull"


def setup(stage: str) -> logging.Logger:
    config.LOGS.mkdir(parents=True, exist_ok=True)
    path = config.LOGS / f"{stage}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    logger = logging.getLogger(LOGGER)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    file = logging.FileHandler(path, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(fmt)
    logger.addHandler(console)
    logger.addHandler(file)
    logger.debug("log file: %s", path)
    return logger
