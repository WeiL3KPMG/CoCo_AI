import argparse
from pathlib import Path

try:
    from .candidate_loader import load_candidates
    from .prompt_store import (
        append_jsonl,
        normalize_company_key,
        prompt_sha256,
        write_prompt_snapshot,
    )
    from .prompt_template import load_prompt_template, read_non_empty_text, render_prompt
except ImportError:
    from candidate_loader import load_candidates
    from prompt_store import (
        append_jsonl,
        normalize_company_key,
        prompt_sha256,
        write_prompt_snapshot,
    )
    from prompt_template import load_prompt_template, read_non_empty_text, render_prompt

MINIMAL_CANDIDATE_FIELDS = [
    "Company Name",
    "Exchange:Ticker",
    "Industry Classifications",
    "Business Description",
]


def build_minimal_candidate(candidate: dict) -> dict:
    return {field: candidate.get(field) for field in MINIMAL_CANDIDATE_FIELDS}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build one prompt per candidate using compare_prompt.txt."
    )
    parser.add_argument(
        "--compare-prompt",
        default="prompts/Core/compare_prompt.txt",
        help="Prompt template path (must include both placeholders).",
    )
    parser.add_argument(
        "--target-company",
        default="prompts/Target Company/target_company_greenko.txt",
        help="Target company reference text file path.",
    )
    parser.add_argument(
        "--candidates-json",
        default="data/cleaned/coco_candidates_all_columns.json",
        help="Candidates JSON file path with top-level 'records'.",
    )
    parser.add_argument(
        "--output-jsonl",
        default="data/output/prompt_runs/coco_prompt_payloads.jsonl",
        help="Output JSONL path for all prompt payloads.",
    )
    parser.add_argument(
        "--output-prompts-dir",
        default="data/output/prompt_runs/prompts_txt",
        help="Directory for per-company prompt text snapshots.",
    )
    parser.add_argument(
        "--write-prompt-files",
        action="store_true",
        help="If set, write one .txt prompt file per candidate.",
    )
    parser.add_argument(
        "--template-version",
        default="v1",
        help="Version label stored with each prompt record.",
    )
    return parser


def run(
    compare_prompt_path: Path,
    target_company_path: Path,
    candidates_path: Path,
    output_jsonl_path: Path,
    output_prompts_dir: Path,
    write_prompt_files: bool,
    template_version: str,
) -> None:
    prompt_template = load_prompt_template(compare_prompt_path)
    target_text = read_non_empty_text(target_company_path)
    candidates = load_candidates(candidates_path)

    output_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl_path.write_text("", encoding="utf-8")

    for idx, candidate in enumerate(candidates, start=1):
        minimal_candidate = build_minimal_candidate(candidate)
        company_name = str(minimal_candidate.get("Company Name", "")).strip()
        ticker = str(minimal_candidate.get("Exchange:Ticker", "")).strip()
        candidate_key = normalize_company_key(company_name, idx)

        prompt_text = render_prompt(
            template=prompt_template,
            target_text=target_text,
            candidate_record=minimal_candidate,
        )
        prompt_hash = prompt_sha256(prompt_text)

        row = {
            "candidate_index": idx,
            "candidate_key": candidate_key,
            "company_name": company_name,
            "ticker": ticker,
            "template_path": str(compare_prompt_path),
            "template_version": template_version,
            "prompt_sha256": prompt_hash,
            "prompt_text": prompt_text,
            "candidate_record": minimal_candidate,
        }
        append_jsonl(output_jsonl_path, row)

        if write_prompt_files:
            prompt_file = output_prompts_dir / f"{idx:03d}_{candidate_key}.txt"
            write_prompt_snapshot(prompt_file, prompt_text)

    print(f"Built {len(candidates)} prompt payloads.")
    print(f"JSONL: {output_jsonl_path}")
    if write_prompt_files:
        print(f"Prompt files: {output_prompts_dir}")


def main() -> None:
    args = build_arg_parser().parse_args()
    run(
        compare_prompt_path=Path(args.compare_prompt),
        target_company_path=Path(args.target_company),
        candidates_path=Path(args.candidates_json),
        output_jsonl_path=Path(args.output_jsonl),
        output_prompts_dir=Path(args.output_prompts_dir),
        write_prompt_files=bool(args.write_prompt_files),
        template_version=args.template_version,
    )


if __name__ == "__main__":
    main()

