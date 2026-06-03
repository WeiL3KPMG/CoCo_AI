import argparse
from collections import Counter
import copy
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MODEL_SNAPSHOT_PATTERN = re.compile(r".*-\d{4}-\d{2}-\d{2}$")
DEFAULT_PROVIDER = "openai"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
EXPECTED_CRITERIA = [
    ("Business Model & Activities", 40, {0, 10, 20, 30, 40}),
    ("Strategic & Sector Alignment", 25, {0, 6, 12, 18, 25}),
    ("Scale & Infrastructure Intensity", 20, {0, 5, 10, 15, 20}),
    ("Geography Relevance", 15, {0, 5, 10, 15}),
]
ALLOWED_TIERS = {"Strong", "Median", "Weak"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_EVIDENCE_STRENGTH = {"high", "medium", "low"}
REQUIRED_TOP_LEVEL_KEYS = (
    "company_name",
    "ticker",
    "overall_score",
    "tier",
    "tier_justification",
    "criteria_scores",
    "key_differences_vs_target",
    "confidence",
)
REQUIRED_CRITERIA_KEYS = (
    "criteria",
    "max_score",
    "score",
    "reason",
    "evidence_strength",
)
ALLOWED_SCORE_BANDS: dict[str, list[int]] = {
    # v3 5-factor rubric
    "Core Business & Product Overlap": [0, 8, 15, 23, 30],
    "Customer, End-Market & Use-Case Alignment": [0, 5, 10, 15, 20],
    "Value Chain, Revenue Model & Go-to-Market Similarity": [0, 5, 10, 15, 20],
    "Operating Scale, Capability & Maturity": [0, 4, 8, 12, 15],
    "Geographic & Regulatory Market Relevance": [0, 4, 8, 12, 15],
    # legacy 4-factor rubric
    "Business Model & Activities": [0, 10, 20, 30, 40],
    "Strategic & Sector Alignment": [0, 6, 12, 18, 25],
    "Scale & Infrastructure Intensity": [0, 5, 10, 15, 20],
    "Geography Relevance": [0, 5, 10, 15],
}
TIER_CUTOFFS = (30, 60)
TIER_CUTOFF_MARGIN = 5
BOUNDARY_EXTRA_CALLS = 4


def is_snapshot_model_name(model: str) -> bool:
    """Return True when model name appears date-pinned (e.g. gpt-4o-mini-2024-07-18)."""
    return bool(MODEL_SNAPSHOT_PATTERN.fullmatch(model.strip()))


def normalize_provider(value: Any) -> str:
    provider = str(value or DEFAULT_PROVIDER).strip().lower()
    return provider or DEFAULT_PROVIDER


def normalize_base_url(value: Any, provider: str) -> str:
    base_url = str(value or "").strip()
    if base_url:
        return base_url.rstrip("/")
    if provider == "openai":
        return DEFAULT_OPENAI_BASE_URL
    raise ValueError(
        "Missing base URL for non-OpenAI provider. "
        "Set 'base_url' in config or pass --base-url."
    )


def coerce_config_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
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
    raise ValueError(f"Invalid boolean config value: {value!r}")


def coerce_config_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid float config value: {value!r}") from exc


def coerce_config_int(value: Any, default: int | None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid int config value: {value!r}") from exc


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
    # Tie-breaker favors lower band when equidistant.
    return min(
        allowed,
        key=lambda v: (abs(v - score), 0 if v <= score else 1, v),
    )


def normalize_parsed_output(
    parsed_output: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(parsed_output, dict):
        return parsed_output, []

    normalized = copy.deepcopy(parsed_output)
    notes: list[str] = []
    criteria_scores = normalized.get("criteria_scores")
    if not isinstance(criteria_scores, list):
        return normalized, notes

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

        # Unknown criterion: keep current score if numeric for stable total recompute.
        if score_raw is not None:
            total += score_raw
            saw_any_numeric = True

    if saw_any_numeric:
        normalized["overall_score"] = total
        normalized["tier"] = _tier_from_score(total)
        notes.append(
            f"overall_score recomputed to {total}; tier recomputed to {normalized['tier']}."
        )

    return normalized, notes


def _extract_borderline_count(parsed_output: dict[str, Any]) -> int:
    top_level = parsed_output.get("borderline_decisions_count")
    top_level_int = _coerce_int_like(top_level)
    if top_level_int is not None and top_level_int >= 0:
        return top_level_int

    criteria_scores = parsed_output.get("criteria_scores")
    if not isinstance(criteria_scores, list):
        return 0

    count = 0
    for item in criteria_scores:
        if isinstance(item, dict) and item.get("borderline_decision") is True:
            count += 1
    return count


def _mentions_limited_evidence(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    patterns = (
        "limited evidence",
        "lack of evidence",
        "insufficient evidence",
        "sparse evidence",
        "evidence is limited",
        "evidence was limited",
        "evidence is sparse",
        "absence of relevant evidence",
        "missing evidence",
    )
    return any(p in normalized for p in patterns)


def classify_error_reason(error_message: str | None) -> str:
    if not error_message:
        return "unknown"
    msg = error_message.strip()
    if msg.startswith("HTTPError "):
        code = msg.split(":", 1)[0].replace("HTTPError ", "").strip()
        return f"http_{code}"
    if msg.startswith("TimeoutError"):
        return "timeout"
    if "JSON integrity failed" in msg:
        return "json_integrity_failed"
    if "Rubric validation failed" in msg:
        return "rubric_validation_failed"
    if msg.startswith("ValueError"):
        return "value_error"
    if msg.startswith("URLError"):
        return "url_error"
    if ":" in msg:
        return msg.split(":", 1)[0].strip().lower()
    return "other_error"


def check_json_structural_integrity(
    parsed_output: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(parsed_output, dict):
        return False, ["Model output is not a valid JSON object."]

    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in parsed_output:
            errors.append(f"Missing top-level key: {key}")

    criteria_scores = parsed_output.get("criteria_scores")
    if not isinstance(criteria_scores, list):
        errors.append("criteria_scores must be a list.")
    else:
        if not criteria_scores:
            errors.append("criteria_scores cannot be empty.")
        for idx, item in enumerate(criteria_scores):
            if not isinstance(item, dict):
                errors.append(f"criteria_scores[{idx}] must be an object.")
                continue
            for key in REQUIRED_CRITERIA_KEYS:
                if key not in item:
                    errors.append(f"criteria_scores[{idx}] missing key: {key}")

    return len(errors) == 0, errors


def validate_parsed_output(parsed_output: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    criteria_scores = parsed_output.get("criteria_scores")
    if not isinstance(criteria_scores, list):
        return ["criteria_scores must be a list."], warnings
    if len(criteria_scores) != len(EXPECTED_CRITERIA):
        errors.append(
            f"criteria_scores must contain exactly {len(EXPECTED_CRITERIA)} items."
        )
        return errors, warnings

    total_score = 0
    evidence_levels: list[str] = []
    for idx, (expected_name, expected_max, allowed_scores) in enumerate(
        EXPECTED_CRITERIA
    ):
        item = criteria_scores[idx]
        if not isinstance(item, dict):
            errors.append(f"criteria_scores[{idx}] must be an object.")
            continue

        actual_name = str(item.get("criteria", "")).strip()
        if actual_name != expected_name:
            errors.append(
                f"criteria_scores[{idx}].criteria must be '{expected_name}' "
                f"(found '{actual_name}')."
            )

        max_score = _coerce_int_like(item.get("max_score"))
        if max_score != expected_max:
            errors.append(
                f"criteria_scores[{idx}].max_score must be {expected_max}."
            )

        score = _coerce_int_like(item.get("score"))
        if score is None:
            errors.append(f"criteria_scores[{idx}].score must be an integer.")
        else:
            if score not in allowed_scores:
                allowed = ", ".join(str(v) for v in sorted(allowed_scores))
                errors.append(
                    f"criteria_scores[{idx}].score={score} is invalid; allowed values: {allowed}."
                )
            total_score += score

        reason = str(item.get("reason", "")).strip()
        if not reason:
            errors.append(f"criteria_scores[{idx}].reason must be non-empty.")

        evidence = str(item.get("evidence_strength", "")).strip().lower()
        if evidence not in ALLOWED_EVIDENCE_STRENGTH:
            errors.append(
                f"criteria_scores[{idx}].evidence_strength must be one of "
                f"{sorted(ALLOWED_EVIDENCE_STRENGTH)}."
            )
        else:
            evidence_levels.append(evidence)

        quotes = item.get("evidence_quotes")
        if not isinstance(quotes, list):
            errors.append(f"criteria_scores[{idx}].evidence_quotes must be a list.")
            quote_count = 0
        else:
            quote_count = sum(1 for q in quotes if isinstance(q, str) and q.strip())
            if quote_count == 0:
                warnings.append(
                    f"criteria_scores[{idx}].evidence_quotes is empty; "
                    "criterion should be treated as low-evidence."
                )

        min_allowed = min(allowed_scores)
        if score is not None and score > min_allowed and quote_count == 0:
            errors.append(
                f"criteria_scores[{idx}] score={score} requires at least one evidence quote."
            )

    overall_score = _coerce_int_like(parsed_output.get("overall_score"))
    if overall_score is None:
        errors.append("overall_score must be an integer.")
    elif overall_score != total_score:
        errors.append(
            f"overall_score ({overall_score}) must equal sum(criteria_scores) ({total_score})."
        )

    tier = str(parsed_output.get("tier", "")).strip()
    if tier not in ALLOWED_TIERS:
        errors.append(f"tier must be one of {sorted(ALLOWED_TIERS)}.")
    elif overall_score is not None:
        expected_tier = (
            "Strong" if overall_score >= 60 else "Median" if overall_score >= 30 else "Weak"
        )
        if tier != expected_tier:
            errors.append(
                f"tier '{tier}' does not match overall_score {overall_score} "
                f"(expected '{expected_tier}')."
            )

    key_differences = parsed_output.get("key_differences_vs_target")
    if not isinstance(key_differences, list):
        errors.append("key_differences_vs_target must be a list.")
    else:
        if not (1 <= len(key_differences) <= 3):
            errors.append("key_differences_vs_target must contain 1-3 items.")
        for idx, item in enumerate(key_differences):
            if not isinstance(item, str) or not item.strip():
                errors.append(
                    f"key_differences_vs_target[{idx}] must be a non-empty string."
                )

    confidence = str(parsed_output.get("confidence", "")).strip().lower()
    if confidence not in ALLOWED_CONFIDENCE:
        errors.append(
            f"confidence must be one of {sorted(ALLOWED_CONFIDENCE)}."
        )

    if evidence_levels:
        low_count = sum(1 for e in evidence_levels if e == "low")
        medium_count = sum(1 for e in evidence_levels if e == "medium")
        high_count = sum(1 for e in evidence_levels if e == "high")
        borderline_count = _extract_borderline_count(parsed_output)

        expected_confidence = "medium"
        if (
            low_count >= 2
            or borderline_count >= 2
            or (low_count + medium_count) >= 3
        ):
            expected_confidence = "low"
        elif high_count >= 3 and borderline_count == 0 and medium_count <= 1:
            expected_confidence = "high"

        if confidence in ALLOWED_CONFIDENCE and confidence != expected_confidence:
            warnings.append(
                f"confidence '{confidence}' violates deterministic mapping "
                f"(expected '{expected_confidence}')."
            )

        if low_count >= 2:
            if confidence == "high":
                errors.append(
                    "confidence cannot be 'high' when 2+ criteria have evidence_strength='low'."
                )
            tier_justification = str(parsed_output.get("tier_justification", ""))
            if not _mentions_limited_evidence(tier_justification):
                warnings.append(
                    "tier_justification must mention limited/insufficient evidence when 2+ criteria are low evidence."
                )

    return errors, warnings


def load_runtime_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return payload


def load_prompt_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Prompt JSONL not found: {path}")

    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL line {line_no} in {path}: {exc}") from exc
    return rows


def compute_request_hash(
    *,
    model: str,
    prompt_text: str,
    temperature: float,
    top_p: float,
    frequency_penalty: float,
    presence_penalty: float,
    seed: int | None,
    json_mode: bool,
) -> str:
    payload = {
        "model": model,
        "prompt_text": prompt_text,
        "temperature": temperature,
        "top_p": top_p,
        "frequency_penalty": frequency_penalty,
        "presence_penalty": presence_penalty,
        "seed": seed,
        "json_mode": json_mode,
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


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def call_chat_completion(
    api_key: str,
    api_base_url: str,
    model: str,
    prompt_text: str,
    temperature: float,
    top_p: float,
    frequency_penalty: float,
    presence_penalty: float,
    seed: int | None,
    json_mode: bool,
    timeout_seconds: int = 120,
) -> tuple[str, dict[str, Any]]:
    url = f"{api_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "temperature": temperature,
        "top_p": top_p,
        "frequency_penalty": frequency_penalty,
        "presence_penalty": presence_penalty,
        "messages": [{"role": "user", "content": prompt_text}],
    }
    if seed is not None:
        payload["seed"] = seed
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
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


def run_batch(
    prompts_jsonl: Path,
    output_jsonl: Path,
    api_key: str,
    api_base_url: str,
    provider: str,
    model: str,
    sleep_seconds: float,
    max_retries: int,
    timeout_seconds: int,
    max_rows: int | None,
    temperature: float,
    top_p: float,
    frequency_penalty: float,
    presence_penalty: float,
    seed: int | None,
    require_snapshot_model: bool,
    enforce_response_model_match: bool,
    json_mode: bool,
    cache_path: Path,
) -> None:
    if provider == "openai" and require_snapshot_model and not is_snapshot_model_name(model):
        raise ValueError(
            "Model must be snapshot/date-pinned when --require-snapshot-model is set. "
            f"Received: {model!r}. Example: gpt-4o-mini-2024-07-18"
        )

    rows = load_prompt_rows(prompts_jsonl)
    if max_rows is not None:
        rows = rows[: max(0, max_rows)]
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl.write_text("", encoding="utf-8")
    cache_index = load_cache_index(cache_path)

    total = len(rows)
    success_count = 0
    error_count = 0
    parsed_json_count = 0
    error_reason_counts: Counter[str] = Counter()
    validation_warning_count = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    for idx, row in enumerate(rows, start=1):
        prompt_text = str(row.get("prompt_text", "")).strip()
        if not prompt_text:
            raise ValueError(
                f"Missing prompt_text in row {idx} of {prompts_jsonl}. "
                "Build prompts first using preprocessing/run_build_prompts.py."
            )

        error_message: str | None = None
        model_output_text = ""
        raw_api_response: dict[str, Any] = {}
        parsed_output: dict[str, Any] | None = None
        json_integrity_ok = False
        json_integrity_errors: list[str] = []
        validation_warnings: list[str] = []
        normalization_notes: list[str] = []
        boundary_stabilization_applied = False
        boundary_score_samples: list[int] = []
        boundary_tier_votes: dict[str, int] = {}
        boundary_extra_prompt_tokens = 0
        boundary_extra_completion_tokens = 0
        boundary_extra_total_tokens = 0
        request_hash = compute_request_hash(
            model=f"{provider}:{model}",
            prompt_text=prompt_text,
            temperature=temperature,
            top_p=top_p,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            seed=seed,
            json_mode=json_mode,
        )

        cached = cache_index.get(request_hash)
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
            json_integrity_ok = bool(cached.get("json_integrity_ok", False))
            json_integrity_errors = (
                cached.get("json_integrity_errors")
                if isinstance(cached.get("json_integrity_errors"), list)
                else []
            )
            validation_warnings = (
                cached.get("validation_warnings")
                if isinstance(cached.get("validation_warnings"), list)
                else []
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
            error_message = None
        else:
            for attempt in range(1, max_retries + 1):
                try:
                    model_output_text, raw_api_response = call_chat_completion(
                        api_key=api_key,
                        api_base_url=api_base_url,
                        model=model,
                        prompt_text=prompt_text,
                        temperature=temperature,
                        top_p=top_p,
                        frequency_penalty=frequency_penalty,
                        presence_penalty=presence_penalty,
                        seed=seed,
                        json_mode=json_mode,
                        timeout_seconds=timeout_seconds,
                    )
                    response_model = str(raw_api_response.get("model", "")).strip()
                    if (
                        enforce_response_model_match
                        and response_model
                        and response_model != model
                    ):
                        raise ValueError(
                            "Response model mismatch: "
                            f"requested={model!r}, response={response_model!r}"
                        )
                    parsed_candidate = extract_json_object(model_output_text)
                    json_integrity_ok, json_integrity_errors = check_json_structural_integrity(
                        parsed_candidate
                    )
                    if not json_integrity_ok:
                        raise ValueError(
                            "JSON integrity failed: " + " | ".join(json_integrity_errors)
                        )

                    parsed_output, normalization_notes = normalize_parsed_output(
                        parsed_candidate
                    )
                    if parsed_output is not None:
                        validation_errors, validation_warnings = validate_parsed_output(
                            parsed_output
                        )
                        if validation_errors:
                            validation_warnings = [
                                f"Validation Error: {msg}" for msg in validation_errors
                            ] + validation_warnings
                        else:
                            score_raw = _coerce_int_like(parsed_output.get("overall_score"))
                            if score_raw is not None and _is_near_tier_cutoff(score_raw):
                                boundary_stabilization_applied = True
                                boundary_score_samples = [score_raw]
                                for _ in range(BOUNDARY_EXTRA_CALLS):
                                    try:
                                        extra_output_text, extra_raw = call_chat_completion(
                                            api_key=api_key,
                                            api_base_url=api_base_url,
                                            model=model,
                                            prompt_text=prompt_text,
                                            temperature=temperature,
                                            top_p=top_p,
                                            frequency_penalty=frequency_penalty,
                                            presence_penalty=presence_penalty,
                                            seed=seed,
                                            json_mode=json_mode,
                                            timeout_seconds=timeout_seconds,
                                        )
                                        (
                                            extra_prompt_tokens,
                                            extra_completion_tokens,
                                            extra_total_tokens,
                                        ) = _extract_usage_tokens(extra_raw)
                                        boundary_extra_prompt_tokens += extra_prompt_tokens
                                        boundary_extra_completion_tokens += extra_completion_tokens
                                        boundary_extra_total_tokens += extra_total_tokens

                                        extra_candidate = extract_json_object(extra_output_text)
                                        extra_ok, _ = check_json_structural_integrity(extra_candidate)
                                        if not extra_ok:
                                            continue
                                        extra_parsed, extra_notes = normalize_parsed_output(
                                            extra_candidate
                                        )
                                        if extra_parsed is None:
                                            continue
                                        extra_validation_errors, _ = validate_parsed_output(
                                            extra_parsed
                                        )
                                        if extra_validation_errors:
                                            continue
                                        extra_score = _coerce_int_like(
                                            extra_parsed.get("overall_score")
                                        )
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
                    error_message = None
                    break
                except urllib.error.HTTPError as exc:
                    response_text = exc.read().decode("utf-8", errors="replace")
                    error_message = f"HTTPError {exc.code}: {response_text}"
                    if exc.code in (401, 403):
                        # Stop the entire run for invalid/unauthorized key errors.
                        break
                except Exception as exc:  # noqa: BLE001
                    error_message = f"{type(exc).__name__}: {exc}"

                if attempt < max_retries:
                    time.sleep(min(2 * attempt, 10))

            if error_message is None:
                cache_row = {
                    "request_hash": request_hash,
                    "model_output_text": model_output_text,
                    "raw_api_response": raw_api_response,
                    "parsed_output": parsed_output,
                    "json_integrity_ok": json_integrity_ok,
                    "json_integrity_errors": json_integrity_errors,
                    "validation_warnings": validation_warnings,
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
            total_prompt_tokens += boundary_extra_prompt_tokens
            total_completion_tokens += boundary_extra_completion_tokens
            total_tokens += boundary_extra_total_tokens

        out_row = {
            "candidate_index": row.get("candidate_index"),
            "candidate_key": row.get("candidate_key"),
            "company_name": row.get("company_name"),
            "ticker": row.get("ticker"),
            "provider": provider,
            "model": model,
            "response_model": str(raw_api_response.get("model", "")).strip(),
            "status": "ok" if error_message is None else "error",
            "from_cache": from_cache,
            "error": error_message,
            "json_integrity_ok": json_integrity_ok,
            "json_integrity_errors": json_integrity_errors,
            "prompt_text": prompt_text,
            "model_output_text": model_output_text,
            "parsed_output": parsed_output,
            "validation_warnings": validation_warnings,
            "normalization_notes": normalization_notes,
            "boundary_stabilization_applied": boundary_stabilization_applied,
            "boundary_score_samples": boundary_score_samples,
            "boundary_tier_votes": boundary_tier_votes,
            "raw_api_response": raw_api_response,
        }

        with output_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(out_row, ensure_ascii=False) + "\n")

        status = "ok" if error_message is None else "error"
        if status == "ok":
            success_count += 1
            if parsed_output is not None:
                parsed_json_count += 1
            if validation_warnings:
                validation_warning_count += 1
        else:
            error_count += 1
            error_reason_counts[classify_error_reason(error_message)] += 1
        print(f"[{idx}/{total}] {row.get('company_name', row.get('candidate_key'))}: {status}")

        if error_message is not None and (
            error_message.startswith("HTTPError 401:")
            or error_message.startswith("HTTPError 403:")
        ):
            print(
                "Stopping run early due to authentication/authorization error. "
                "Check API key and model access."
            )
            break

        time.sleep(max(0.0, sleep_seconds))

    print(f"Completed scoring run. Results saved to: {output_jsonl}")
    print(
        "Summary: "
        f"ok={success_count}, "
        f"errors={error_count}, "
        f"parsed_json_ok={parsed_json_count}, "
        f"ok_with_validation_warnings={validation_warning_count}"
    )
    print(
        "Token usage (non-cached calls): "
        f"total_prompt_tokens={total_prompt_tokens}, "
        f"total_completion_tokens={total_completion_tokens}, "
        f"total_tokens={total_tokens}"
    )
    summary = {
        "provider": provider,
        "model": model,
        "total_rows": total,
        "ok_count": success_count,
        "error_count": error_count,
        "parsed_json_ok_count": parsed_json_count,
        "ok_with_validation_warnings": validation_warning_count,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
    }
    summary_path = output_jsonl.with_name(f"{output_jsonl.stem}_summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Run summary saved to: {summary_path}")
    if error_reason_counts:
        print("Error breakdown by type:")
        for reason, count in error_reason_counts.most_common():
            print(f"  - {reason}: {count}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call chat completion API one-by-one for each stored CoCo prompt."
    )
    parser.add_argument(
        "--prompts-jsonl",
        default="data/output/prompt_runs/coco_prompt_payloads.jsonl",
        help="Input prompt payload JSONL produced by run_build_prompts.py.",
    )
    parser.add_argument(
        "--output-jsonl",
        default="data/output/scoring_runs/coco_scored_raw.jsonl",
        help="Output JSONL path for model results.",
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
        help="API key. If empty, reads config file then env var configured by --api-key-env/api_key_env.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Chat model name.",
    )
    parser.add_argument(
        "--require-snapshot-model",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Require a date-pinned model name (e.g. gpt-4o-mini-2024-07-18) "
            "to reduce model drift across runs."
        ),
    )
    parser.add_argument(
        "--enforce-response-model-match",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Fail a row if API response model differs from requested model.",
    )
    parser.add_argument(
        "--config",
        default="secrets/scoring_config.json",
        help=(
            "Optional JSON config file (e.g. api_key, model). "
            "CLI args override config."
        ),
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.5,
        help="Delay between requests.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retry count per prompt.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="HTTP request timeout seconds.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature (use 0.0 for determinism).",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=None,
        help="Nucleus sampling parameter (keep 1.0 for determinism baseline).",
    )
    parser.add_argument(
        "--frequency-penalty",
        type=float,
        default=None,
        help="Frequency penalty (keep 0.0 for determinism baseline).",
    )
    parser.add_argument(
        "--presence-penalty",
        type=float,
        default=None,
        help="Presence penalty (keep 0.0 for determinism baseline).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional deterministic seed (if supported by the model/API). Use --seed -1 to disable config seed.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional cap on number of prompt rows to process (for pilot runs).",
    )
    parser.add_argument(
        "--json-mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use JSON response_format mode for API calls.",
    )
    parser.add_argument(
        "--cache-path",
        default="data/output/scoring_runs/request_cache.jsonl",
        help="Persistent cache file for successful responses keyed by input hash.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    config = load_runtime_config(Path(args.config))

    provider = normalize_provider(args.provider or config.get("provider"))
    api_base_url = normalize_base_url(args.base_url or config.get("base_url"), provider)
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
            "Missing API key. Use --api-key, set config api_key, or set environment variable "
            f"{api_key_env!r}."
        )

    model = args.model.strip() or str(config.get("model", "")).strip() or "gpt-4o-mini"
    temperature = (
        args.temperature
        if args.temperature is not None
        else coerce_config_float(config.get("temperature"), 0.0)
    )
    top_p = args.top_p if args.top_p is not None else coerce_config_float(config.get("top_p"), 1.0)
    frequency_penalty = (
        args.frequency_penalty
        if args.frequency_penalty is not None
        else coerce_config_float(config.get("frequency_penalty"), 0.0)
    )
    presence_penalty = (
        args.presence_penalty
        if args.presence_penalty is not None
        else coerce_config_float(config.get("presence_penalty"), 0.0)
    )
    require_snapshot_model = (
        args.require_snapshot_model
        if args.require_snapshot_model is not None
        else coerce_config_bool(config.get("require_snapshot_model"), False)
    )
    enforce_response_model_match = (
        args.enforce_response_model_match
        if args.enforce_response_model_match is not None
        else coerce_config_bool(config.get("enforce_response_model_match"), False)
    )
    json_mode = (
        args.json_mode
        if args.json_mode is not None
        else coerce_config_bool(config.get("json_mode"), True)
    )
    seed = args.seed
    if seed is None:
        seed = coerce_config_int(config.get("seed"), None)
    if seed == -1:
        seed = None

    if provider == "openai" and not is_snapshot_model_name(model):
        print(
            "Warning: model name is not date-pinned. "
            "For stronger reproducibility use a snapshot model name "
            "(e.g. gpt-4o-mini-2024-07-18)."
        )

    run_batch(
        prompts_jsonl=Path(args.prompts_jsonl),
        output_jsonl=Path(args.output_jsonl),
        api_key=api_key,
        api_base_url=api_base_url,
        provider=provider,
        model=model,
        sleep_seconds=args.sleep_seconds,
        max_retries=args.max_retries,
        timeout_seconds=args.timeout_seconds,
        max_rows=args.max_rows,
        temperature=temperature,
        top_p=top_p,
        frequency_penalty=frequency_penalty,
        presence_penalty=presence_penalty,
        seed=seed,
        require_snapshot_model=require_snapshot_model,
        enforce_response_model_match=enforce_response_model_match,
        json_mode=json_mode,
        cache_path=Path(args.cache_path),
    )


if __name__ == "__main__":
    main()

