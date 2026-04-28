import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

def detect_header_row(df: pd.DataFrame, expected_header: str = "Company Name") -> int:
    for idx, row in df.iterrows():
        values = [str(v).strip() for v in row.tolist() if pd.notna(v)]
        if expected_header in values:
            return idx
    raise ValueError(f"Could not find header row containing '{expected_header}'.")


def normalize_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def read_sheet_as_records(
    excel_path: Path, sheet_name: str, header_row: int | None = None
) -> list[dict[str, Any]]:
    raw_df = pd.read_excel(excel_path, sheet_name=sheet_name, header=None)
    effective_header = (
        detect_header_row(raw_df) if header_row is None else int(header_row)
    )

    headers = [str(h).strip() for h in raw_df.iloc[effective_header].tolist()]
    data_df = raw_df.iloc[effective_header + 1 :].copy()
    data_df.columns = headers
    data_df = data_df.dropna(how="all")

    records: list[dict[str, Any]] = []
    for _, row in data_df.iterrows():
        record = {col: normalize_value(row[col]) for col in data_df.columns}
        records.append(record)
    return records


def load_scoring_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Scoring JSONL not found: {path}")

    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_no}: {exc}") from exc
        if not isinstance(payload, dict):
            continue
        rows.append(payload)
    return rows


CRITERIA_ORDER: list[str] = [
    "Business Model & Activities",
    "Strategic & Sector Alignment",
    "Scale & Asset Intensity",
    "Geography Relevance",
]

# Older scoring JSON may still say "Energy Transition Alignment".
CRITERIA_LABEL_ALIASES: dict[str, str] = {
    "Energy Transition Alignment": "Strategic & Sector Alignment",
}


def canonical_criteria_label(name: str) -> str:
    stripped = name.strip()
    return CRITERIA_LABEL_ALIASES.get(stripped, stripped)


def _criteria_sort_key(name: str) -> tuple[int, str]:
    canon = canonical_criteria_label(name)
    try:
        return (CRITERIA_ORDER.index(canon), name)
    except ValueError:
        return (len(CRITERIA_ORDER), name)


def build_score_breakdown(parsed: dict[str, Any]) -> str | None:
    """Format criteria_scores into 'Name: XX/YY; ...' for Excel."""
    criteria_scores = parsed.get("criteria_scores")
    if not isinstance(criteria_scores, list) or not criteria_scores:
        return None

    parts: list[tuple[str, Any, Any]] = []
    for item in criteria_scores:
        if not isinstance(item, dict):
            continue
        name = str(item.get("criteria", "")).strip()
        score = item.get("score")
        max_score = item.get("max_score")
        if not name:
            continue
        try:
            s = float(score) if score is not None else None
            m = float(max_score) if max_score is not None else None
        except (TypeError, ValueError):
            continue
        if s is None or m is None:
            continue
        # Prefer integers when whole numbers for cleaner display
        s_disp = int(s) if s == int(s) else s
        m_disp = int(m) if m == int(m) else m
        parts.append((name, s_disp, m_disp))

    if not parts:
        return None

    parts.sort(key=lambda t: _criteria_sort_key(t[0]))
    return "; ".join(
        f"{canonical_criteria_label(name)}: {s_disp}/{m_disp}"
        for name, s_disp, m_disp in parts
    )


def map_tier_to_rank(tier: Any, median_label: str) -> str | None:
    if not isinstance(tier, str):
        return None

    normalized = tier.strip().lower()
    if normalized == "strong":
        return "Strong"
    if normalized == "median":
        return median_label
    if normalized == "weak":
        return "Exclude"
    return None


def build_coco_reason(parsed: dict[str, Any], rank: str | None) -> str | None:
    tier_justification = parsed.get("tier_justification")
    if isinstance(tier_justification, str) and tier_justification.strip():
        return tier_justification.strip()

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

    if reason_parts:
        prefix = f"{rank}. " if rank else ""
        return prefix + " | ".join(reason_parts)

    return None


def build_finalized_records(
    excel_records: list[dict[str, Any]],
    scoring_rows: list[dict[str, Any]],
    median_label: str,
) -> list[dict[str, Any]]:
    by_index: dict[int, dict[str, Any]] = {}
    for row in scoring_rows:
        idx = row.get("candidate_index")
        if isinstance(idx, int):
            by_index[idx] = row

    finalized: list[dict[str, Any]] = []
    for idx, record in enumerate(excel_records, start=1):
        out = dict(record)
        scoring_row = by_index.get(idx, {})
        parsed = scoring_row.get("parsed_output")

        overall_numeric: Any = None
        score_breakdown: str | None = None
        rank: str | None = None
        reason: str | None = None
        if isinstance(parsed, dict):
            overall_numeric = parsed.get("overall_score")
            score_breakdown = build_score_breakdown(parsed)
            rank = map_tier_to_rank(parsed.get("tier"), median_label=median_label)
            reason = build_coco_reason(parsed=parsed, rank=rank)

        # Numeric total for sorting/filtering; breakdown is human-readable rubric lines.
        out["CoCo Score Overall"] = overall_numeric
        out["CoCo Score"] = score_breakdown
        out["CoCo Rank"] = rank
        out["CoCo Reason"] = reason
        finalized.append(out)

    return finalized


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build finalized CoCo Excel with all source columns plus "
            "'CoCo Score Overall', rubric breakdown in 'CoCo Score', "
            "'CoCo Rank', and 'CoCo Reason'."
        )
    )
    parser.add_argument(
        "--input-excel",
        default="data/Raw/CIQ_CoCo.xls",
        help="Input source Excel path from CapIQ.",
    )
    parser.add_argument(
        "--sheet",
        default="Screening",
        help="Sheet name in input Excel.",
    )
    parser.add_argument(
        "--header-row",
        type=int,
        default=None,
        help="Optional 0-based header row. Auto-detects if omitted.",
    )
    parser.add_argument(
        "--scores-jsonl",
        default="data/output/scoring_runs/coco_scored_raw.jsonl",
        help="Scoring output JSONL from run_score_batch.py.",
    )
    parser.add_argument(
        "--output-excel",
        default="data/output/final/coco_finalized.xlsx",
        help="Output finalized Excel path.",
    )
    parser.add_argument(
        "--median-label",
        default="Median",
        help="Label for median tier in final output.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    excel_records = read_sheet_as_records(
        excel_path=Path(args.input_excel),
        sheet_name=args.sheet,
        header_row=args.header_row,
    )
    scoring_rows = load_scoring_rows(Path(args.scores_jsonl))

    finalized_records = build_finalized_records(
        excel_records=excel_records,
        scoring_rows=scoring_rows,
        median_label=args.median_label,
    )

    output_path = Path(args.output_excel)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(finalized_records).to_excel(output_path, index=False)

    print(f"Source rows: {len(excel_records)}")
    print(f"Scoring rows: {len(scoring_rows)}")
    print(f"Finalized Excel written to: {output_path}")


if __name__ == "__main__":
    main()
