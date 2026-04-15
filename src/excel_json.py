from preprocessing.excel_json import (
    build_arg_parser,
    detect_header_row,
    export_excel_to_json,
    main,
    normalize_value,
    read_sheet_as_records,
)

__all__ = [
    "detect_header_row",
    "normalize_value",
    "read_sheet_as_records",
    "export_excel_to_json",
    "build_arg_parser",
    "main",
]

if __name__ == "__main__":
    main()
