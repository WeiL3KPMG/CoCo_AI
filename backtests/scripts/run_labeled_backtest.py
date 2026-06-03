import argparse
import csv
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


POSITIVE_TIERS_DEFAULT = {"strong", "median"}
CRITERIA_LABEL_ALIASES: dict[str, str] = {
    "Energy Transition Alignment": "Strategic & Sector Alignment",
    "Scale & Asset Intensity": "Scale & Infrastructure Intensity",
}
CRITERIA_ORDER: list[str] = [
    "Core Business & Product Overlap",
    "Customer, End-Market & Use-Case Alignment",
    "Value Chain, Revenue Model & Go-to-Market Similarity",
    "Operating Scale, Capability & Maturity",
    "Geographic & Regulatory Market Relevance",
    "Business Model & Activities",
    "Strategic & Sector Alignment",
    "Scale & Infrastructure Intensity",
    "Geography Relevance",
]
ALLOWED_SCORE_BANDS: dict[str, list[int]] = {
    "Core Business & Product Overlap": [0, 8, 15, 23, 30],
    "Customer, End-Market & Use-Case Alignment": [0, 5, 10, 15, 20],
    "Value Chain, Revenue Model & Go-to-Market Similarity": [0, 5, 10, 15, 20],
    "Operating Scale, Capability & Maturity": [0, 4, 8, 12, 15],
    "Geographic & Regulatory Market Relevance": [0, 4, 8, 12, 15],
    "Business Model & Activities": [0, 10, 20, 30, 40],
    "Strategic & Sector Alignment": [0, 6, 12, 18, 25],
    "Scale & Infrastructure Intensity": [0, 5, 10, 15, 20],
    "Geography Relevance": [0, 5, 10, 15],
}
TIER_CUTOFFS = (30, 60)
TIER_CUTOFF_MARGIN = 5
BOUNDARY_EXTRA_CALLS = 4
DEFAULT_PROVIDER = "openai"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass
class CandidateRow:
    dataset_file: str
    sheet: str
    row_index: int
    target_text: str
    ticker: str
    description: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run labeled CoCo backtests from Excel files where each listed candidate "
            "is treated as a ground-truth positive comparable."
        )
    )
    parser.add_argument(
        "--input-excel",
        default="",
        help=(
            "Optional single Excel file to backtest. If omitted, scans --datasets-dir "
            "for .xls/.xlsx files."
        ),
    )
    parser.add_argument(
        "--datasets-dir",
        default="backtests/datasets",
        help="Directory containing labeled backtest Excel files.",
    )
    parser.add_argument(
        "--compare-prompt",
        default="prompts/Core/compare_prompt.txt",
        help="Prompt template path.",
    )
    parser.add_argument(
        "--config",
        default="secrets/scoring_config.json",
        help="JSON config path with model/api settings.",
    )
    parser.add_argument(
        "--provider",
        default="",
        help="API provider (default: openai).",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="API base URL (e.g. https://api.openai.com/v1 or https://api.deepseek.com/v1).",
    )
    parser.add_argument(
        "--api-key-env",
        default="",
        help="Environment variable name for API key fallback (e.g. OPENAI_API_KEY, DEEPSEEK_API_KEY).",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API key override. If empty, falls back to config and env var from api_key_env.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Model override. If empty, falls back to config and gpt-4o-mini.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature (default from config, then 0.0).",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=None,
        help="Top-p sampling (default from config, then 1.0).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional request seed for reproducibility (default from config; unset means no seed).",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=None,
        help="Optional cap on number of candidates processed.",
    )
    parser.add_argument(
        "--runs-dir",
        default="backtests/runs",
        help="Directory where each run output folder is created.",
    )
    parser.add_argument(
        "--positive-tiers",
        default="Strong,Median",
        help="Comma-separated tiers treated as positive prediction (default: Strong,Median).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="Retry attempts per candidate for transient/API errors (default: 4).",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.5,
        help="Base delay between successful requests (default: 0.5).",
    )
    parser.add_argument(
        "--json-mode",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use JSON response_format mode for API calls (default: true).",
    )
    parser.add_argument(
        "--cache-path",
        default="backtests/cache/request_cache.jsonl",
        help="Persistent cache file for successful responses keyed by input hash.",
    )
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable persistent cache reads/writes (default: true). Use --no-cache for fresh API calls.",
    )
    return parser.parse_args()


def load_json_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file must be a JSON object: {path}")
    return data


def discover_excel_paths(input_excel: str, datasets_dir: Path) -> list[Path]:
    if input_excel.strip():
        path = Path(input_excel)
        if not path.exists():
            raise FileNotFoundError(f"Input Excel not found: {path}")
        return [path]

    if not datasets_dir.exists():
        raise FileNotFoundError(
            f"Datasets directory not found: {datasets_dir}. "
            "Create it and add labeled Excel files."
        )

    excel_files = sorted(list(datasets_dir.glob("*.xlsx")) + list(datasets_dir.glob("*.xls")))
    if not excel_files:
        raise FileNotFoundError(
            f"No .xls/.xlsx files found in datasets dir: {datasets_dir}"
        )
    return excel_files


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _coerce_config_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _normalize_provider(value: Any) -> str:
    provider = str(value or DEFAULT_PROVIDER).strip().lower()
    return provider or DEFAULT_PROVIDER


def _normalize_base_url(value: Any, provider: str) -> str:
    base_url = str(value or "").strip()
    if base_url:
        return base_url.rstrip("/")
    if provider == "openai":
        return DEFAULT_OPENAI_BASE_URL
    raise ValueError(
        "Missing base URL for non-OpenAI provider. "
        "Set 'base_url' in config or pass --base-url."
    )


def _is_snapshot_model_name(model: str) -> bool:
    # Snapshot names usually end with a date suffix, e.g. gpt-4o-2024-08-06.
    return bool(re.search(r"\d{4}-\d{2}-\d{2}$", model.strip()))


def _response_model_matches_requested(requested: str, actual: str) -> bool:
    requested_clean = requested.strip()
    actual_clean = actual.strip()
    if not requested_clean or not actual_clean:
        return False
    if _is_snapshot_model_name(requested_clean):
        return actual_clean == requested_clean
    return actual_clean == requested_clean or actual_clean.startswith(f"{requested_clean}-")


def extract_candidates_from_workbook(path: Path) -> list[CandidateRow]:
    output: list[CandidateRow] = []
    xls = pd.ExcelFile(path)
    for sheet_name in xls.sheet_names:
        df = pd.read_excel(path, sheet_name=sheet_name, header=None)
        if df.empty:
            continue

        target_text = _coerce_text(df.iloc[0, 1]) if df.shape[1] > 1 else ""
        if not target_text:
            continue

        header_row = None
        for idx, row in df.iterrows():
            col_a = _coerce_text(row.iloc[0]) if len(row) > 0 else ""
            col_b = _coerce_text(row.iloc[1]) if len(row) > 1 else ""
            if col_a.lower() in {"cocos", "cocos:"} and "description" in col_b.lower():
                header_row = idx
                break

        if header_row is None:
            continue

        for ridx in range(header_row + 1, len(df)):
            ticker = _coerce_text(df.iloc[ridx, 0]) if df.shape[1] > 0 else ""
            description = _coerce_text(df.iloc[ridx, 1]) if df.shape[1] > 1 else ""
            if not ticker and not description:
                continue
            output.append(
                CandidateRow(
                    dataset_file=path.name,
                    sheet=sheet_name,
                    row_index=ridx,
                    target_text=target_text,
                    ticker=ticker,
                    description=description,
                )
            )
    return output


def render_prompt(prompt_template: str, target_text: str, ticker: str, description: str) -> str:
    candidate = {
        "Company Name": "",
        "Exchange:Ticker": ticker,
        "Industry Classifications": "",
        "Business Description": description,
    }
    return (
        prompt_template.replace("{PASTE TARGET TXT HERE}", target_text)
        .replace("{PASTE CANDIDATE JSON HERE}", json.dumps(candidate, ensure_ascii=False, indent=2))
    )


def call_chat_completion(
    *,
    api_key: str,
    api_base_url: str,
    model: str,
    prompt_text: str,
    temperature: float,
    top_p: float,
    json_mode: bool,
    seed: int | None,
    timeout_seconds: int = 180,
) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": model,
        "temperature": temperature,
        "top_p": top_p,
        "messages": [{"role": "user", "content": prompt_text}],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if seed is not None:
        payload["seed"] = seed
    request = urllib.request.Request(
        url=f"{api_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8")

    response_json = json.loads(raw)
    content = (
        response_json.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    return content, response_json


def _extract_retry_after_seconds(error_text: str) -> float | None:
    # OpenAI can return either "try again in 8.34s" or "try again in 184ms".
    sec_match = re.search(
        r"try again in\s+([0-9]+(?:\.[0-9]+)?)s",
        error_text,
        re.IGNORECASE,
    )
    if sec_match:
        try:
            return float(sec_match.group(1))
        except ValueError:
            return None

    ms_match = re.search(
        r"try again in\s+([0-9]+(?:\.[0-9]+)?)ms",
        error_text,
        re.IGNORECASE,
    )
    if ms_match:
        try:
            return float(ms_match.group(1)) / 1000.0
        except ValueError:
            return None

    return None


def compute_request_hash(
    *,
    model: str,
    prompt_text: str,
    temperature: float,
    top_p: float,
    json_mode: bool,
    seed: int | None,
) -> str:
    payload = {
        "model": model,
        "prompt_text": prompt_text,
        "temperature": temperature,
        "top_p": top_p,
        "json_mode": json_mode,
        "seed": seed,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_cache_index(cache_path: Path) -> dict[str, dict[str, Any]]:
    if not cache_path.exists():
        return {}
    index: dict[str, dict[str, Any]] = {}
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        key = str(row.get("request_hash", "")).strip()
        if not key:
            continue
        index[key] = row
    return index


def append_cache_row(cache_path: Path, cache_row: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(cache_row, ensure_ascii=False) + "\n")


def parse_model_json(text: str) -> dict[str, Any] | None:
    clean = text.strip()
    if not clean:
        return None

    try:
        parsed = json.loads(clean)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(clean[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _coerce_int_like(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _normalize_evidence_quotes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    quotes: list[str] = []
    for item in value:
        text = str(item).strip() if isinstance(item, str) else ""
        if text:
            quotes.append(text)
    return quotes


def _extract_usage_tokens(raw_api_response: dict[str, Any]) -> tuple[int, int, int]:
    usage = raw_api_response.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt_tokens = _coerce_int_like(usage.get("prompt_tokens")) or 0
    completion_tokens = _coerce_int_like(usage.get("completion_tokens")) or 0
    total_tokens = _coerce_int_like(usage.get("total_tokens"))
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


def _tier_from_score(score: int) -> str:
    if score >= 60:
        return "Strong"
    if score >= 30:
        return "Median"
    return "Weak"


def _is_near_tier_cutoff(score: int, margin: int = TIER_CUTOFF_MARGIN) -> bool:
    return any(abs(score - cutoff) <= margin for cutoff in TIER_CUTOFFS)


def _median_int(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _majority_tier_from_scores(scores: list[int]) -> tuple[str, int, dict[str, int]]:
    tier_scores: dict[str, list[int]] = {"Weak": [], "Median": [], "Strong": []}
    for score in scores:
        tier_scores[_tier_from_score(score)].append(score)

    tier_votes = {tier: len(vals) for tier, vals in tier_scores.items() if vals}
    max_votes = max(tier_votes.values())
    leaders = [tier for tier, count in tier_votes.items() if count == max_votes]

    if len(leaders) == 1:
        selected_tier = leaders[0]
    else:
        median_tier = _tier_from_score(_median_int(scores))
        if median_tier in leaders:
            selected_tier = median_tier
        else:
            # Conservative tie-breaker: favor lower tier.
            tier_rank = {"Weak": 0, "Median": 1, "Strong": 2}
            selected_tier = min(leaders, key=lambda t: tier_rank[t])

    representative_score = _median_int(tier_scores[selected_tier])
    return selected_tier, representative_score, tier_votes


def _clamp_to_allowed_band(score: int, allowed: list[int]) -> int:
    return min(
        allowed,
        key=lambda v: (abs(v - score), 0 if v <= score else 1, v),
    )


def normalize_parsed_output(parsed_output: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(parsed_output, dict):
        return parsed_output, []

    notes: list[str] = []
    criteria_scores = parsed_output.get("criteria_scores")
    if not isinstance(criteria_scores, list):
        return parsed_output, notes

    total = 0
    saw_any_numeric = False
    for idx, item in enumerate(criteria_scores):
        if not isinstance(item, dict):
            continue
        criteria_name = str(item.get("criteria", "")).strip()
        allowed = ALLOWED_SCORE_BANDS.get(criteria_name)
        score_raw = _coerce_int_like(item.get("score"))
        if allowed:
            item["max_score"] = max(allowed)
            min_allowed = min(allowed)
            quotes = _normalize_evidence_quotes(item.get("evidence_quotes"))
            item["evidence_quotes"] = quotes
            if score_raw is None:
                score = min_allowed
                item["score"] = score
                notes.append(
                    f"criteria_scores[{idx}] score missing/non-integer; defaulted to {score}."
                )
            elif score_raw not in allowed:
                clamped = _clamp_to_allowed_band(score_raw, allowed)
                item["score"] = clamped
                notes.append(
                    f"criteria_scores[{idx}] score {score_raw} clamped to {clamped}."
                )
            else:
                item["score"] = score_raw

            # Enforce evidence grounding: non-minimum score requires at least one quote.
            if int(item["score"]) > min_allowed and not quotes:
                item["score"] = min_allowed
                item["evidence_strength"] = "low"
                notes.append(
                    f"criteria_scores[{idx}] missing evidence_quotes; "
                    f"score clamped to {min_allowed} and evidence_strength set to low."
                )
            total += int(item["score"])
            saw_any_numeric = True
            continue

        if score_raw is not None:
            total += score_raw
            saw_any_numeric = True

    if saw_any_numeric:
        parsed_output["overall_score"] = total
        parsed_output["tier"] = _tier_from_score(total)
        notes.append(
            f"overall_score recomputed to {total}; tier recomputed to {parsed_output['tier']}."
        )

    return parsed_output, notes


def _call_and_normalize_once(
    *,
    api_key: str,
    api_base_url: str,
    model: str,
    prompt_text: str,
    temperature: float,
    top_p: float,
    json_mode: bool,
    seed: int | None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any], list[str], str | None]:
    model_output_text, raw_api_response = call_chat_completion(
        api_key=api_key,
        api_base_url=api_base_url,
        model=model,
        prompt_text=prompt_text,
        temperature=temperature,
        top_p=top_p,
        json_mode=json_mode,
        seed=seed,
    )
    parsed_output = parse_model_json(model_output_text)
    if parsed_output is None:
        return (
            model_output_text,
            None,
            raw_api_response,
            [],
            "Failed to parse model output as JSON object.",
        )
    parsed_output, normalization_notes = normalize_parsed_output(parsed_output)
    return model_output_text, parsed_output, raw_api_response, normalization_notes, None


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def canonical_criteria_label(name: str) -> str:
    return CRITERIA_LABEL_ALIASES.get(name.strip(), name.strip())


def _criteria_sort_key(name: str) -> tuple[int, str]:
    canon = canonical_criteria_label(name)
    try:
        return (CRITERIA_ORDER.index(canon), name)
    except ValueError:
        return (len(CRITERIA_ORDER), name)


def build_score_breakdown(parsed: dict[str, Any]) -> str:
    criteria_scores = parsed.get("criteria_scores")
    if not isinstance(criteria_scores, list):
        return ""

    parts: list[tuple[str, Any, Any]] = []
    for item in criteria_scores:
        if not isinstance(item, dict):
            continue
        name = str(item.get("criteria", "")).strip()
        score = item.get("score")
        max_score = item.get("max_score")
        if not name:
            continue
        parts.append((name, score, max_score))

    if not parts:
        return ""

    parts.sort(key=lambda t: _criteria_sort_key(t[0]))
    return "; ".join(
        f"{canonical_criteria_label(name)}: {score}/{max_score}"
        for name, score, max_score in parts
    )


def build_evidence_summary(parsed: dict[str, Any]) -> str:
    parts: list[str] = []
    confidence = str(parsed.get("confidence", "")).strip()
    if confidence:
        parts.append(f"Confidence: {confidence}")

    criteria_scores = parsed.get("criteria_scores")
    strength_parts: list[str] = []
    if isinstance(criteria_scores, list):
        for item in criteria_scores:
            if not isinstance(item, dict):
                continue
            criteria = str(item.get("criteria", "")).strip()
            strength = str(item.get("evidence_strength", "")).strip()
            if criteria and strength:
                strength_parts.append(f"{canonical_criteria_label(criteria)}: {strength}")
    if strength_parts:
        parts.append("Evidence Strength: " + "; ".join(strength_parts))
    return " | ".join(parts)


def map_tier_to_rank(tier: str) -> str:
    normalized = tier.strip().lower()
    if normalized == "strong":
        return "Strong"
    if normalized == "median":
        return "Median"
    if normalized == "weak":
        return "Exclude"
    return ""


def build_coco_reason(parsed: dict[str, Any], rank: str) -> str:
    criteria_scores = parsed.get("criteria_scores")
    reason_parts: list[str] = []
    if isinstance(criteria_scores, list):
        for item in criteria_scores:
            if not isinstance(item, dict):
                continue
            criteria = str(item.get("criteria", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if criteria and reason:
                reason_parts.append(f"{criteria}: {reason}")

    criterion_block = ""
    if reason_parts:
        prefix = f"{rank}. " if rank else ""
        criterion_block = prefix + " | ".join(reason_parts)

    tier_justification = str(parsed.get("tier_justification", "")).strip()
    if criterion_block and tier_justification:
        return f"{criterion_block}\n\n{tier_justification}"
    if criterion_block:
        return criterion_block
    return tier_justification


def main() -> None:
    args = parse_args()
    config = load_json_config(Path(args.config))

    provider = _normalize_provider(args.provider or config.get("provider"))
    api_base_url = _normalize_base_url(args.base_url or config.get("base_url"), provider)
    api_key_env = (
        args.api_key_env.strip()
        or str(config.get("api_key_env", "")).strip()
        or ("OPENAI_API_KEY" if provider == "openai" else "")
    )
    api_key = args.api_key.strip() or str(config.get("api_key", "")).strip()
    if not api_key and api_key_env:
        api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        raise ValueError(
            "Missing API key. Use --api-key, config api_key, or set env var "
            f"{api_key_env!r}."
        )

    model = args.model.strip() or str(config.get("model", "")).strip() or "gpt-4o-mini"
    temperature = (
        args.temperature
        if args.temperature is not None
        else float(config.get("temperature", 0.0))
    )
    top_p = args.top_p if args.top_p is not None else float(config.get("top_p", 1.0))
    seed = args.seed if args.seed is not None else _coerce_optional_int(config.get("seed"))
    require_snapshot_model = _coerce_config_bool(config.get("require_snapshot_model"), False)
    enforce_response_model_match = _coerce_config_bool(
        config.get("enforce_response_model_match"), False
    )

    if provider == "openai" and require_snapshot_model and not _is_snapshot_model_name(model):
        raise ValueError(
            "Config requires a snapshot model, but requested model is not pinned: "
            f"'{model}'. Use a snapshot name like 'gpt-4o-YYYY-MM-DD'."
        )

    positive_tiers = {
        part.strip().lower()
        for part in args.positive_tiers.split(",")
        if part.strip()
    } or POSITIVE_TIERS_DEFAULT

    prompt_template = Path(args.compare_prompt).read_text(encoding="utf-8")
    excel_paths = discover_excel_paths(args.input_excel, Path(args.datasets_dir))

    candidates: list[CandidateRow] = []
    for excel_path in excel_paths:
        candidates.extend(extract_candidates_from_workbook(excel_path))

    if not candidates:
        raise ValueError(
            "No candidate rows found. Ensure each sheet has target text in B1 and a "
            "'CoCos/Descriptions' section."
        )

    if args.max_candidates is not None:
        candidates = candidates[: max(0, args.max_candidates)]

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cache_enabled = bool(args.cache)
    cache_path = Path(args.cache_path)
    cache_index = load_cache_index(cache_path) if cache_enabled else {}
    cache_status = "ENABLED" if cache_enabled else "DISABLED"
    print(f"Cache: {cache_status} ({cache_path.as_posix()})")
    print(f"Request seed: {seed if seed is not None else 'UNSET'}")

    result_rows: list[dict[str, Any]] = []
    false_negatives: list[dict[str, Any]] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0

    total = len(candidates)
    for idx, row in enumerate(candidates, start=1):
        prompt_text = render_prompt(
            prompt_template=prompt_template,
            target_text=row.target_text,
            ticker=row.ticker,
            description=row.description,
        )

        error: str | None = None
        model_output_text = ""
        raw_api_response: dict[str, Any] = {}
        parsed_output: dict[str, Any] | None = None
        normalization_notes: list[str] = []
        boundary_stabilization_applied = False
        boundary_score_samples: list[int] = []
        boundary_tier_votes: dict[str, int] = {}
        request_hash = compute_request_hash(
            model=f"{provider}:{model}",
            prompt_text=prompt_text,
            temperature=temperature,
            top_p=top_p,
            json_mode=bool(args.json_mode),
            seed=seed,
        )

        cached = cache_index.get(request_hash) if cache_enabled else None
        from_cache = cached is not None
        if cached:
            model_output_text = str(cached.get("model_output_text", ""))
            raw_api_response = (
                cached.get("raw_api_response")
                if isinstance(cached.get("raw_api_response"), dict)
                else {}
            )
            parsed_output = (
                cached.get("parsed_output")
                if isinstance(cached.get("parsed_output"), dict)
                else None
            )
            normalization_notes = (
                cached.get("normalization_notes")
                if isinstance(cached.get("normalization_notes"), list)
                else []
            )
            boundary_stabilization_applied = bool(
                cached.get("boundary_stabilization_applied", False)
            )
            boundary_score_samples = (
                cached.get("boundary_score_samples")
                if isinstance(cached.get("boundary_score_samples"), list)
                else []
            )
            boundary_tier_votes = (
                cached.get("boundary_tier_votes")
                if isinstance(cached.get("boundary_tier_votes"), dict)
                else {}
            )
            error = None
        else:
            for attempt in range(1, max(1, args.max_retries) + 1):
                try:
                    (
                        model_output_text,
                        parsed_output,
                        raw_api_response,
                        normalization_notes,
                        error,
                    ) = _call_and_normalize_once(
                        api_key=api_key,
                        api_base_url=api_base_url,
                        model=model,
                        prompt_text=prompt_text,
                        temperature=temperature,
                        top_p=top_p,
                        json_mode=bool(args.json_mode),
                        seed=seed,
                    )
                    if parsed_output is not None:
                        score_raw = _coerce_int_like(parsed_output.get("overall_score"))
                        if score_raw is not None and _is_near_tier_cutoff(score_raw):
                            boundary_stabilization_applied = True
                            boundary_score_samples = [score_raw]
                            for _ in range(BOUNDARY_EXTRA_CALLS):
                                try:
                                    _, extra_parsed, _, extra_notes, extra_error = _call_and_normalize_once(
                                        api_key=api_key,
                                        api_base_url=api_base_url,
                                        model=model,
                                        prompt_text=prompt_text,
                                        temperature=temperature,
                                        top_p=top_p,
                                        json_mode=bool(args.json_mode),
                                        seed=seed,
                                    )
                                    if extra_error is None and extra_parsed is not None:
                                        extra_score = _coerce_int_like(extra_parsed.get("overall_score"))
                                        if extra_score is not None:
                                            boundary_score_samples.append(extra_score)
                                        normalization_notes.extend(extra_notes)
                                except Exception:
                                    # Keep primary result if extra boundary samples fail.
                                    continue

                            if len(boundary_score_samples) >= 2:
                                (
                                    voted_tier,
                                    representative_score,
                                    boundary_tier_votes,
                                ) = _majority_tier_from_scores(boundary_score_samples)
                                parsed_output["overall_score"] = representative_score
                                parsed_output["tier"] = voted_tier
                                normalization_notes.append(
                                    "Boundary stabilization applied: "
                                    f"scores={boundary_score_samples}, votes={boundary_tier_votes}, "
                                    f"selected_tier={voted_tier}, representative_score={representative_score}."
                                )

                        response_model = str(raw_api_response.get("model", "")).strip()
                        if enforce_response_model_match and not _response_model_matches_requested(
                            model, response_model
                        ):
                            error = (
                                "Response model mismatch: "
                                f"requested='{model}', actual='{response_model or 'UNKNOWN'}'. "
                                "Use a pinned snapshot model to avoid run-to-run drift."
                            )
                        else:
                            error = None
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    error = f"HTTPError {exc.code}: {detail}"
                    if attempt >= args.max_retries:
                        break
                    # Handle rate-limit gracefully using server hint when present.
                    if exc.code == 429:
                        suggested = _extract_retry_after_seconds(detail) or 2.0
                        sleep_for = max(1.0, suggested + 0.5)
                    else:
                        sleep_for = min(2.0 * attempt, 10.0)
                    time.sleep(sleep_for)
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                    if attempt >= args.max_retries:
                        break
                    time.sleep(min(2.0 * attempt, 10.0))

            if error is None and cache_enabled:
                cache_row = {
                    "request_hash": request_hash,
                    "model_output_text": model_output_text,
                    "raw_api_response": raw_api_response,
                    "parsed_output": parsed_output,
                    "normalization_notes": normalization_notes,
                    "boundary_stabilization_applied": boundary_stabilization_applied,
                    "boundary_score_samples": boundary_score_samples,
                    "boundary_tier_votes": boundary_tier_votes,
                }
                append_cache_row(cache_path, cache_row)
                cache_index[request_hash] = cache_row

        if not from_cache:
            (
                prompt_tokens,
                completion_tokens,
                used_total_tokens,
            ) = _extract_usage_tokens(raw_api_response)
            total_prompt_tokens += prompt_tokens
            total_completion_tokens += completion_tokens
            total_tokens += used_total_tokens

        status = "ok" if error is None else "error"
        predicted_tier = str((parsed_output or {}).get("tier", "")).strip()
        predicted_positive = status == "ok" and predicted_tier.lower() in positive_tiers
        expected_positive = True
        is_false_negative = status == "ok" and expected_positive and not predicted_positive

        out_row = {
            "dataset_file": row.dataset_file,
            "sheet": row.sheet,
            "row_index": row.row_index,
            "ticker": row.ticker,
            "expected_positive": expected_positive,
            "predicted_positive": predicted_positive,
            "is_false_negative": is_false_negative,
            "predicted_tier": predicted_tier,
            "overall_score": (parsed_output or {}).get("overall_score"),
            "status": status,
            "error": error,
            "normalization_notes": normalization_notes,
            "provider": provider,
            "response_model": str(raw_api_response.get("model", "")).strip(),
            "from_cache": from_cache,
            "boundary_stabilization_applied": boundary_stabilization_applied,
            "boundary_score_samples": boundary_score_samples,
            "boundary_tier_votes": boundary_tier_votes,
            "parsed_output": parsed_output,
        }
        result_rows.append(out_row)

        if is_false_negative:
            false_negatives.append(
                {
                    "dataset_file": row.dataset_file,
                    "sheet": row.sheet,
                    "ticker": row.ticker,
                    "predicted_tier": predicted_tier,
                    "overall_score": (parsed_output or {}).get("overall_score"),
                    "status": out_row["status"],
                    "error": error or "",
                }
            )

        status_label = out_row["status"]
        print(f"[{idx}/{total}] {row.sheet} {row.ticker} -> {predicted_tier or 'N/A'} ({status_label})")
        if status == "ok":
            time.sleep(max(0.0, args.sleep_seconds))

    errors_count = sum(1 for r in result_rows if r["status"] != "ok")
    predicted_positive_count = sum(1 for r in result_rows if r["predicted_positive"])
    false_negative_count = len(false_negatives)
    recall_at_tier_gate = (
        (predicted_positive_count / total) if total > 0 else 0.0
    )

    summary = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_excel": args.input_excel,
        "datasets_dir": args.datasets_dir,
        "dataset_files": [p.name for p in excel_paths],
        "compare_prompt": args.compare_prompt,
        "provider": provider,
        "model_requested": model,
        "request_seed": seed,
        "require_snapshot_model": require_snapshot_model,
        "enforce_response_model_match": enforce_response_model_match,
        "cache_enabled": cache_enabled,
        "cache_path": str(cache_path.as_posix()),
        "positive_tiers": sorted(list(positive_tiers)),
        "total_candidates": total,
        "predicted_positive_count": predicted_positive_count,
        "false_negative_count": false_negative_count,
        "error_count": errors_count,
        "recall_at_tier_gate": round(recall_at_tier_gate, 4),
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "outputs": {
            "results_jsonl": str((run_dir / "results.jsonl").as_posix()),
            "summary_json": str((run_dir / "summary.json").as_posix()),
            "false_negatives_csv": str((run_dir / "false_negatives.csv").as_posix()),
        },
    }

    write_jsonl(run_dir / "results.jsonl", result_rows)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with (run_dir / "false_negatives.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "dataset_file",
                "sheet",
                "ticker",
                "predicted_tier",
                "overall_score",
                "status",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(false_negatives)

    excel_rows: list[dict[str, Any]] = []
    for row in result_rows:
        parsed = row.get("parsed_output")
        parsed_dict = parsed if isinstance(parsed, dict) else {}
        predicted_tier = str(parsed_dict.get("tier", "")).strip()
        rank = map_tier_to_rank(predicted_tier)
        excel_rows.append(
            {
                "Dataset File": row["dataset_file"],
                "Sheet": row["sheet"],
                "Ticker": row["ticker"],
                "Business Description": next(
                    (
                        c.description
                        for c in candidates
                        if c.dataset_file == row["dataset_file"]
                        and c.sheet == row["sheet"]
                        and c.ticker == row["ticker"]
                    ),
                    "",
                ),
                "Backtest Status": row["status"],
                "Backtest Error": row["error"] or "",
                "Expected Positive": row["expected_positive"],
                "Predicted Positive": row["predicted_positive"],
                "False Negative": row["is_false_negative"],
                "CoCo Score Overall": parsed_dict.get("overall_score"),
                "CoCo Score": build_score_breakdown(parsed_dict) if parsed_dict else "",
                "CoCo Evidence": build_evidence_summary(parsed_dict) if parsed_dict else "",
                "CoCo Rank": rank,
                "Predicted Tier": predicted_tier,
                "CoCo Reason": build_coco_reason(parsed_dict, rank) if parsed_dict else "",
            }
        )
    pd.DataFrame(excel_rows).to_excel(run_dir / "backtest_review.xlsx", index=False)

    summary["outputs"]["backtest_review_excel"] = str(
        (run_dir / "backtest_review.xlsx").as_posix()
    )
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("")
    print("Backtest completed.")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
