import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


def detect_header_row(df: pd.DataFrame, expected_header: str = "Company Name") -> int:
    """Return the row index where the expected header appears."""
    for idx, row in df.iterrows():
        values = [str(v).strip() for v in row.tolist() if pd.notna(v)]
        if expected_header in values:
            return idx
    raise ValueError(f"Could not find header row containing '{expected_header}'.")


def normalize_value(value: Any) -> Any:
    """Convert pandas/numpy values into JSON-serializable primitives."""
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
    """
    Read one sheet and convert each row to a JSON record.
    Keeps all columns exactly as they appear in the sheet.
    """
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


def export_excel_to_json(
    input_file: Path,
    output_file: Path,
    sheet_name: str = "Screening",
    header_row: int | None = None,
) -> None:
    records = read_sheet_as_records(
        excel_path=input_file,
        sheet_name=sheet_name,
        header_row=header_row,
    )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_file": str(input_file),
        "sheet_name": sheet_name,
        "record_count": len(records),
        "records": records,
    }

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Exported {len(records)} records to: {output_file}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert CapIQ Excel sheet into row-wise JSON with all columns preserved."
    )
    parser.add_argument(
        "--input",
        default="data/Raw/CIQ_CoCo.xls",
        help="Input Excel file path (.xls/.xlsx).",
    )
    parser.add_argument(
        "--output",
        default="data/cleaned/coco_candidates_all_columns.json",
        help="Output JSON file path.",
    )
    parser.add_argument(
        "--sheet",
        default="Screening",
        help="Sheet name to export.",
    )
    parser.add_argument(
        "--header-row",
        type=int,
        default=None,
        help="Optional 0-based header row. If omitted, it auto-detects row containing 'Company Name'.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    export_excel_to_json(
        input_file=Path(args.input),
        output_file=Path(args.output),
        sheet_name=args.sheet,
        header_row=args.header_row,
    )


if __name__ == "__main__":
    main()

