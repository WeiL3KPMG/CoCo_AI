import json
from pathlib import Path
from typing import Any

TARGET_PLACEHOLDER = "{PASTE TARGET TXT HERE}"
CANDIDATE_PLACEHOLDER = "{PASTE CANDIDATE JSON HERE}"


def read_non_empty_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Text file is empty: {path}")
    return text


def load_prompt_template(path: Path) -> str:
    template = read_non_empty_text(path)
    missing = []
    if TARGET_PLACEHOLDER not in template:
        missing.append(TARGET_PLACEHOLDER)
    if CANDIDATE_PLACEHOLDER not in template:
        missing.append(CANDIDATE_PLACEHOLDER)
    if missing:
        raise ValueError(
            "Prompt template is missing required placeholders: "
            + ", ".join(missing)
        )
    return template


def render_prompt(
    template: str,
    target_text: str,
    candidate_record: dict[str, Any],
    candidate_indent: int = 2,
) -> str:
    candidate_json = json.dumps(
        candidate_record,
        ensure_ascii=False,
        indent=candidate_indent,
    )
    return (
        template.replace(TARGET_PLACEHOLDER, target_text)
        .replace(CANDIDATE_PLACEHOLDER, candidate_json)
        .strip()
    )

