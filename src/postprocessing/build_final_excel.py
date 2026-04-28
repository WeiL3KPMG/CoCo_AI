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

        score: Any = None
        rank: str | None = None
        reason: str | None = None
        if isinstance(parsed, dict):
            score = parsed.get("overall_score")
            rank = map_tier_to_rank(parsed.get("tier"), median_label=median_label)
            reason = build_coco_reason(parsed=parsed, rank=rank)

        out["CoCo Score"] = score
        out["CoCo Rank"] = rank
        out["CoCo Reason"] = reason
        finalized.append(out)

    return finalized


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build finalized CoCo Excel with all source columns plus "
            "'CoCo Score' and 'CoCo Rank'."
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
