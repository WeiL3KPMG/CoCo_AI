# Labeled CoCo Backtests

Use this folder to run repeatable backtests against labeled CoCo datasets without touching normal pipeline outputs.

## Folder Layout

- `backtests/datasets/` - place labeled Excel files here (`.xls` / `.xlsx`)
- `backtests/scripts/` - reusable backtest scripts
- `backtests/runs/` - auto-generated run outputs (timestamped)

## Expected Excel Format

Each sheet should include:

1. Target-company reference text in cell `B1` (row 1, column B).
2. A candidate section with header row:
   - column A = `CoCos` (or `CoCos:`)
   - column B = `Descriptions`
3. Candidate rows below header:
   - column A = ticker (`Exchange:Ticker`)
   - column B = business description

All listed candidate rows are treated as **ground-truth positive** comparables.

## One-Command Backtest

From repo root:

```bash
python backtests/scripts/run_labeled_backtest.py --datasets-dir backtests/datasets
```

For a single file:

```bash
python backtests/scripts/run_labeled_backtest.py --input-excel "path/to/file.xlsx"
```

## Outputs

Each run writes a folder under `backtests/runs/<timestamp>/`:

- `results.jsonl` - one row per candidate with model output and status
- `false_negatives.csv` - candidates predicted outside the positive tier gate
- `backtest_review.xlsx` - Excel review file with `CoCo Score`, `CoCo Evidence`, `CoCo Rank`, and `CoCo Reason`
- `summary.json` - run-level metrics and output paths

## Useful Options

- `--compare-prompt prompts/Core/compare_prompt.txt`
- `--config secrets/scoring_config.json`
- `--model gpt-4o-mini-2024-07-18`
- `--provider openai` (default) or `--provider deepseek`
- `--base-url https://api.deepseek.com/v1`
- `--api-key-env DEEPSEEK_API_KEY`
- `--max-candidates 20`
- `--positive-tiers Strong,Median`

## DeepSeek Pilot Run

Create a DeepSeek config from `secrets/scoring_config.deepseek.example.json`, then run:

```bash
python backtests/scripts/run_labeled_backtest.py --config secrets/scoring_config.deepseek.json --max-candidates 20 --no-cache
```

## Example

```bash
python backtests/scripts/run_labeled_backtest.py --input-excel "backtests/datasets/CoCo_examples.xlsx" --sleep-seconds 1.5 --max-retries 8 --no-cache
```
