from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .paths import logs_dir


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "transfer"


def new_log_file(prefix: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return logs_dir() / f"{timestamp}_{safe_name(prefix)}.log"

