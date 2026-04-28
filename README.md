# CoCo AI Agent (V1 Prototype)

## What This Project Is

This project is a rule-driven AI prototype to support Comparable Company (CoCo) analysis for valuation work.

The goal is to classify each candidate company against a target company as:

- `Strong`
- `Median`
- `Weak` (exported as **Exclude** in the final Excel for audit clarity)

using a consistent scoring rubric and structured JSON output. The model also returns per-criterion reasons and a short **tier justification** for audit review.

This is an AI-assisted workflow, not full automation of analyst judgment.

## Current V1 Workflow

1. **CapIQ export (manual)**  
   Input file from CapIQ (default: `data/Raw/CIQ_CoCo.xls`).

2. **Excel → JSON preprocessing**  
   `excel_json.py` converts the sheet to JSON with **all columns** preserved for downstream merge.

3. **Prompt building (one company at a time)**  
   `run_build_prompts.py` uses `prompts/Core/compare_prompt.txt`, injects target text and one candidate. Candidate fields are minimal:

   - `Company Name`
   - `Exchange:Ticker`
   - `Industry Classifications`
   - `Business Description`

4. **Scoring API run (one-by-one)**  
   `run_score_batch.py` reads `coco_prompt_payloads.jsonl`, calls OpenAI Chat Completions per row, and writes JSONL with raw response, parsed JSON, and errors.

5. **Final Excel (analyst + audit)**  
   `build_final_excel.py` merges **all original CapIQ columns** with:

   - `CoCo Score Overall` — numeric `overall_score` from parsed JSON  
   - Row order — **sorted by descending** `CoCo Score Overall` so the workbook reads best-to-weakest comparable; ties keep the earlier CapIQ order; rows **without** a numeric overall score appear **after** scored rows (e.g. not yet scored).  
   - `CoCo Score` — criteria breakdown text, e.g. `Business Model & Activities: 30/40; Strategic & Sector Alignment: 18/25; Scale & Asset Intensity: 14/20; Geography Relevance: 10/15`  
   - `CoCo Rank` — `Strong` / `Median` / `Exclude` (from `Weak`)  
   - `CoCo Reason` — when both are present in the parsed JSON: **paragraph 1** is the tier label (e.g. `Exclude.`) plus per-criterion reasons joined with `|` (same rubric labels as `CoCo Score`); **paragraph 2** (blank line separator) is `tier_justification`. If only one side exists (e.g. older runs missing `tier_justification`), that single block is used.

## Project Structure (Current)

| Path | Role |
|------|------|
| `src/preprocessing/` | Excel → JSON, prompt build, templates |
| `src/preprocessing/excel_json.py` | CapIQ Excel → JSON (`records` + all columns) |
| `src/preprocessing/run_build_prompts.py` | Build `coco_prompt_payloads.jsonl` (+ optional `.txt` per company) |
| `src/scoring/run_score_batch.py` | OpenAI scoring batch |
| `src/postprocessing/build_final_excel.py` | Merge CapIQ sheet + scoring → finalized `.xlsx` |
| `prompts/Core/compare_prompt.txt` | Master prompt + JSON schema (incl. `tier_justification`) |
| `prompts/Target Company/target_company_greenko.txt` | Target company reference |
| `secrets/scoring_config.example.json` | Example API config (safe to commit) |
| `secrets/scoring_config.json` | **Your** API key + model (**gitignored** — create locally) |
| `data/cleaned/coco_candidates_all_columns.json` | Preprocessed candidates |
| `data/output/prompt_runs/coco_prompt_payloads.jsonl` | One prompt row per candidate |
| `data/output/prompt_runs/prompts_txt/` | Optional per-company prompt snapshots |
| `data/output/scoring_runs/coco_scored_raw.jsonl` | Default full scoring output |
| `data/output/final/coco_finalized.xlsx` | Default path for full finalized workbook |
| `streamlit_app/app.py` | Optional **Streamlit** UI: upload CapIQ → runs the same CLI steps in a per-session temp workspace |

## API key and model (recommended)

Create `secrets/scoring_config.json` (copy from `secrets/scoring_config.example.json`):

```json
{
  "api_key": "YOUR_OPENAI_API_KEY",
  "model": "gpt-4o-mini"
}
```

Precedence: **`--api-key` / `--model` CLI** → **config file** → **`OPENAI_API_KEY` env** → default model `gpt-4o-mini` if model omitted.

Do not commit `secrets/scoring_config.json`; it is listed in `.gitignore`.

## Commands to Run

From project root.

### 1) Build candidate JSON from CapIQ Excel

```bash
python src/preprocessing/excel_json.py
```

### 2) Build prompts for all candidates

```bash
python src/preprocessing/run_build_prompts.py --write-prompt-files
```

### 3) Run scoring (config file — no key on command line)

Full list:

```bash
python src/scoring/run_score_batch.py
```

Pilot first (e.g. 5 companies):

```bash
python src/scoring/run_score_batch.py --max-rows 5 --output-jsonl data/output/scoring_runs/coco_scored_smoke5.jsonl
```

Useful flags: `--config`, `--model`, `--sleep-seconds`, `--max-retries`, `--timeout-seconds`. On **401/403**, the batch **stops early** so a bad key does not burn through the whole list.

### 4) Build finalized Excel

```bash
python src/postprocessing/build_final_excel.py --scores-jsonl data/output/scoring_runs/coco_scored_raw.jsonl --output-excel data/output/final/coco_finalized.xlsx
```

After a smoke run, point `--scores-jsonl` at your smoke JSONL (e.g. `coco_scored_smoke5.jsonl`).

Merge is by **row order** (`candidate_index` 1…N matches the CapIQ sheet rows used when prompts were built).

### Streamlit UI (optional)

Install dependencies (including Streamlit):

```bash
pip install -r requirements.txt
```

From the project root:

```bash
streamlit run streamlit_app/app.py
```

The UI uploads your CapIQ file into a **temporary session folder**, runs steps 1–4 with the same scripts as the CLI (using `secrets/scoring_config.json` for scoring), then offers a **Download** of `coco_finalized.xlsx`. Use **Scoring — max rows** for smoke tests (e.g. `10`).

## Important Notes

- One prompt template for all candidates; only candidate content changes.
- Each scoring JSONL line includes: `prompt_text`, `model_output_text`, `parsed_output`, `raw_api_response`, `status`, `error`.
- For cost control, prefer **`gpt-4o-mini`** and **`--max-rows`** for pilots; long prompts (target + business description) dominate token usage.

## What Has Been Done (highlights)

- End-to-end path: **CapIQ Excel → JSON → prompts JSONL → OpenAI scoring JSONL → finalized Excel** with numeric score, score breakdown, rank, and audit reason columns.
- **Secrets config**: `secrets/scoring_config.json` for API key + model choice; example file for onboarding; gitignore on the real file.
- **Scoring runner** (`run_score_batch.py`): optional `--max-rows`, auth early-stop on 401/403, end-of-run summary (`ok` / `errors` / `parsed_json_ok`), config + env + CLI resolution.
- **Post-processing** (`build_final_excel.py`): all source columns + `CoCo Score Overall`, `CoCo Score` (criteria breakdown), `CoCo Rank`, `CoCo Reason` (per-criterion reasons plus `tier_justification`, two paragraphs when both exist); rows sorted descending by overall score.
- **Streamlit wrapper** (`streamlit_app/app.py`): optional GUI for non-technical users; same pipeline, session-scratch workspace.
- **Prompt**: JSON schema includes **`tier_justification`** for short audit narrative; per-criterion `reason` fields retained and merged into `CoCo Reason` as described above.
- **Rank mapping** in Excel: `Strong` / `Median` / `Weak` → display **Strong** / **Median** / **Exclude** (strict tier strings; no typo fallbacks).

## Suggested Next Steps

1. Run full scoring on all candidates, then review rows with `status != ok` or missing `parsed_output`.
2. Add optional **validation** post-step (e.g. `overall_score` vs sum of criteria, tier vs score bands) and surface flags in Excel.
3. Optional: trim target text or cap business-description length for lower API cost once rubric is stable.

## Resume Prompt (for next chat)

> Continue CoCo AI Agent V1. Prompts: `src/preprocessing/run_build_prompts.py` + `prompts/Core/compare_prompt.txt`. Scoring: `src/scoring/run_score_batch.py` with `secrets/scoring_config.json` for key/model. Final Excel: `src/postprocessing/build_final_excel.py` → all CapIQ columns + `CoCo Score Overall`, `CoCo Score`, `CoCo Rank`, `CoCo Reason` (per-criterion + optional `tier_justification`); rows sorted by descending overall score. Candidate fields stay minimal: Company Name, Exchange:Ticker, Industry Classifications, Business Description.
