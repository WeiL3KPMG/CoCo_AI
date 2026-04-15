import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


TARGET_PLACEHOLDER = "{PASTE TARGET TXT HERE}"
CANDIDATE_PLACEHOLDER = "{PASTE CANDIDATE JSON HERE}"


def read_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(
            f"Input text file is empty: {path}. Save content before running this script."
        )
    return text


def read_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    if not isinstance(records, list):
        raise ValueError("Invalid candidate JSON: 'records' must be a list.")
    return records


def normalize_company_key(company_name: str, fallback_index: int) -> str:
    base = company_name.strip() if company_name else f"candidate_{fallback_index}"
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower()
    return safe or f"candidate_{fallback_index}"


def build_prompt(
    prompt_template: str,
    target_text: str,
    candidate_record: dict[str, Any],
    candidate_indent: int = 2,
) -> str:
    candidate_json = json.dumps(
        candidate_record,
        ensure_ascii=False,
        indent=candidate_indent,
    )
    if TARGET_PLACEHOLDER in prompt_template and CANDIDATE_PLACEHOLDER in prompt_template:
        return (
            prompt_template.replace(TARGET_PLACEHOLDER, target_text)
            .replace(CANDIDATE_PLACEHOLDER, candidate_json)
            .strip()
        )

    # Fallback: if placeholders are not present, append sections in a stable format.
    return (
        f"{prompt_template.strip()}\n\n"
        f"Target Company Reference:\n{target_text}\n\n"
        f"Candidate Company:\n{candidate_json}"
    ).strip()


def export_prompt_payloads(
    compare_prompt_path: Path,
    target_company_path: Path,
    candidates_path: Path,
    output_jsonl_path: Path,
) -> None:
    prompt_template = read_text(compare_prompt_path)
    target_text = read_text(target_company_path)
    candidates = read_candidates(candidates_path)

    output_jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    with output_jsonl_path.open("w", encoding="utf-8") as f:
        for idx, candidate in enumerate(candidates, start=1):
            company_name = str(candidate.get("Company Name", "")).strip()
            ticker = str(candidate.get("Exchange:Ticker", "")).strip()

            filled_prompt = build_prompt(
                prompt_template=prompt_template,
                target_text=target_text,
                candidate_record=candidate,
            )

            row = {
                "candidate_index": idx,
                "candidate_key": normalize_company_key(company_name, idx),
                "company_name": company_name,
                "ticker": ticker,
                "prompt_text": filled_prompt,
                "candidate_record": candidate,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Built {len(candidates)} prompt payloads: {output_jsonl_path}")


def extract_json_object(text: str) -> dict[str, Any] | None:
    """
    Parse the model output as JSON. If extra text exists, try to recover
    the first top-level JSON object.
    """
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


def call_chatgpt(
    api_key: str,
    model: str,
    prompt_text: str,
    timeout_seconds: int = 120,
) -> tuple[str, dict[str, Any]]:
    url = "https://api.openai.com/v1/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": prompt_text}],
    }
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
    message_content = (
        response_json.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    return message_content, response_json


def run_batch_comparison(
    compare_prompt_path: Path,
    target_company_path: Path,
    candidates_path: Path,
    output_jsonl_path: Path,
    api_key: str,
    model: str,
    sleep_seconds: float = 0.5,
    max_retries: int = 3,
) -> None:
    prompt_template = read_text(compare_prompt_path)
    target_text = read_text(target_company_path)
    candidates = read_candidates(candidates_path)

    output_jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    with output_jsonl_path.open("w", encoding="utf-8") as f:
        for idx, candidate in enumerate(candidates, start=1):
            company_name = str(candidate.get("Company Name", "")).strip()
            ticker = str(candidate.get("Exchange:Ticker", "")).strip()
            candidate_key = normalize_company_key(company_name, idx)

            prompt_text = build_prompt(
                prompt_template=prompt_template,
                target_text=target_text,
                candidate_record=candidate,
            )

            error_message: str | None = None
            model_output_text = ""
            raw_api_response: dict[str, Any] = {}
            parsed_output: dict[str, Any] | None = None

            for attempt in range(1, max_retries + 1):
                try:
                    model_output_text, raw_api_response = call_chatgpt(
                        api_key=api_key,
                        model=model,
                        prompt_text=prompt_text,
                    )
                    parsed_output = extract_json_object(model_output_text)
                    error_message = None
                    break
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8", errors="replace")
                    error_message = f"HTTPError {e.code}: {body}"
                except Exception as e:
                    error_message = f"{type(e).__name__}: {e}"

                if attempt < max_retries:
                    time.sleep(min(2 * attempt, 10))

            row = {
                "candidate_index": idx,
                "candidate_key": candidate_key,
                "company_name": company_name,
                "ticker": ticker,
                "model": model,
                "prompt_text": prompt_text,
                "status": "ok" if error_message is None else "error",
                "error": error_message,
                "model_output_text": model_output_text,
                "parsed_output": parsed_output,
                "raw_api_response": raw_api_response,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()

            print(
                f"[{idx}/{len(candidates)}] {company_name or candidate_key}: "
                f"{'ok' if error_message is None else 'error'}"
            )
            time.sleep(max(0.0, sleep_seconds))

    print(f"Completed batch run. Results saved to: {output_jsonl_path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run CoCo comparisons one-by-one via ChatGPT API and log outputs."
    )
    parser.add_argument(
        "--compare-prompt",
        default="prompts/Core/compare_prompt.txt",
        help="Path to prompt template with placeholders.",
    )
    parser.add_argument(
        "--target-company",
        default="prompts/Traget Company/target_company_greenko.txt",
        help="Path to target company reference text file.",
    )
    parser.add_argument(
        "--candidates-json",
        default="data/cleaned/coco_candidates_all_columns.json",
        help="Path to candidate JSON containing 'records'.",
    )
    parser.add_argument(
        "--output-jsonl",
        default="data/output/coco_scored_raw.jsonl",
        help="Where to write one model output record per candidate.",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="OpenAI API key. If empty, set OPENAI_API_KEY env var.",
    )
    parser.add_argument(
        "--model",
        default="gpt-3.5-turbo",
        help="Chat model name (default: gpt-3.5-turbo).",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.5,
        help="Delay between candidate requests.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retries per candidate on API failure.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    api_key = args.api_key.strip()
    if not api_key:
        import os

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "Missing API key. Use --api-key or set OPENAI_API_KEY environment variable."
        )

    run_batch_comparison(
        compare_prompt_path=Path(args.compare_prompt),
        target_company_path=Path(args.target_company),
        candidates_path=Path(args.candidates_json),
        output_jsonl_path=Path(args.output_jsonl),
        api_key=api_key,
        model=args.model,
        sleep_seconds=args.sleep_seconds,
        max_retries=args.max_retries,
    )


if __name__ == "__main__":
    main()
