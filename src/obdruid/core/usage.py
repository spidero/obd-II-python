"""Local usage counters: how many times the program was started and since when it is installed.

Kept in the data folder only (usage.json) – nothing is ever sent anywhere.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

FILE_NAME = "usage.json"


def _read(root: Path) -> dict:
    try:
        data = json.loads((Path(root) / FILE_NAME).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def summary(root: Path, today: Optional[date] = None) -> dict:
    """{"starts": n, "since": "YYYY-MM-DD", "days": n} – days counts the first day as day 1."""
    data = _read(root)
    today = today or date.today()
    try:
        since = date.fromisoformat(str(data.get("since")))
    except ValueError:
        since = today
    return {"starts": int(data.get("starts", 0) or 0), "since": since.isoformat(),
            "days": max(1, (today - since).days + 1)}


def record_start(root: Path, today: Optional[date] = None) -> dict:
    """Count one program start; never fails (a read-only data folder just is not counted)."""
    today = today or date.today()
    current = summary(root, today)
    data = {"starts": current["starts"] + 1, "since": current["since"]}
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        (Path(root) / FILE_NAME).write_text(json.dumps(data) + "\n", "utf-8")
    except OSError:
        pass
    return summary(root, today) if (Path(root) / FILE_NAME).exists() else {**current, "starts": data["starts"]}
