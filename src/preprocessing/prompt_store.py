import hashlib
import json
import re
from pathlib import Path


def normalize_company_key(company_name: str, fallback_index: int) -> str:
    base = company_name.strip() if company_name else f"candidate_{fallback_index}"
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower()
    return safe or f"candidate_{fallback_index}"


def prompt_sha256(prompt_text: str) -> str:
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_prompt_snapshot(path: Path, prompt_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt_text, encoding="utf-8")

