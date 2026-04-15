import json
from pathlib import Path
from typing import Any


def load_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Candidate JSON must contain a top-level 'records' list.")
    return records

