#!/usr/bin/env python3
"""
logger.py — tiny rotating log so we can see what happened (the app runs
windowless, so there's no console). Writes pc_caster.log next to the code,
capped at ~256 KB with 2 rollovers, and records uncaught exceptions.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pc_caster.log")
_LOGGER_NAME = "pccaster"


def setup_logging():
    log = logging.getLogger(_LOGGER_NAME)
    if log.handlers:           # already configured
        return log
    log.setLevel(logging.INFO)
    try:
        h = RotatingFileHandler(LOG_FILE, maxBytes=256 * 1024,
                                backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
    except Exception:
        pass

    # Log anything that crashes the app (would otherwise be invisible).
    def _excepthook(exc_type, exc, tb):
        log.error("UNCAUGHT EXCEPTION", exc_info=(exc_type, exc, tb))
    sys.excepthook = _excepthook

    log.info("================ PC Caster started ================")
    return log


def get_logger():
    return logging.getLogger(_LOGGER_NAME)
