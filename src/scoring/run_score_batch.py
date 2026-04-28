import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


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


def call_openai_chat(
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
    model: str,
    sleep_seconds: float,
    max_retries: int,
    timeout_seconds: int,
    max_rows: int | None,
) -> None:
    rows = load_prompt_rows(prompts_jsonl)
    if max_rows is not None:
        rows = rows[: max(0, max_rows)]
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl.write_text("", encoding="utf-8")

    total = len(rows)
    success_count = 0
    error_count = 0
    parsed_json_count = 0
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

        for attempt in range(1, max_retries + 1):
            try:
                model_output_text, raw_api_response = call_openai_chat(
                    api_key=api_key,
                    model=model,
                    prompt_text=prompt_text,
                    timeout_seconds=timeout_seconds,
                )
                parsed_output = extract_json_object(model_output_text)
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

        out_row = {
            "candidate_index": row.get("candidate_index"),
            "candidate_key": row.get("candidate_key"),
            "company_name": row.get("company_name"),
            "ticker": row.get("ticker"),
            "model": model,
            "status": "ok" if error_message is None else "error",
            "error": error_message,
            "prompt_text": prompt_text,
            "model_output_text": model_output_text,
            "parsed_output": parsed_output,
            "raw_api_response": raw_api_response,
        }

        with output_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(out_row, ensure_ascii=False) + "\n")

        status = "ok" if error_message is None else "error"
        if status == "ok":
            success_count += 1
            if parsed_output is not None:
                parsed_json_count += 1
        else:
            error_count += 1
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
        f"parsed_json_ok={parsed_json_count}"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call OpenAI API one-by-one for each stored CoCo prompt."
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
        "--api-key",
        default="",
        help="OpenAI API key. If empty, reads config file then OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="OpenAI chat model.",
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
        "--max-rows",
        type=int,
        default=None,
        help="Optional cap on number of prompt rows to process (for pilot runs).",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    config = load_runtime_config(Path(args.config))

    api_key = (
        args.api_key.strip()
        or str(config.get("api_key", "")).strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        raise ValueError(
            "Missing API key. Use --api-key, set secrets/scoring_config.json api_key, "
            "or set OPENAI_API_KEY."
        )

    model = args.model.strip() or str(config.get("model", "")).strip() or "gpt-4o-mini"

    run_batch(
        prompts_jsonl=Path(args.prompts_jsonl),
        output_jsonl=Path(args.output_jsonl),
        api_key=api_key,
        model=model,
        sleep_seconds=args.sleep_seconds,
        max_retries=args.max_retries,
        timeout_seconds=args.timeout_seconds,
        max_rows=args.max_rows,
    )


if __name__ == "__main__":
    main()

