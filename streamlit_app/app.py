"""Streamlit wrapper for the CoCo pipeline. Run from repository root:

    streamlit run streamlit_app/app.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]

EXAMPLE_TARGET_PATH = REPO_ROOT / "prompts" / "Target Company" / "target_company_marketnode.txt"


@st.cache_data(show_spinner=False)
def load_example_target_text() -> str:
    """Bundled example profile — same folder as CLI defaults; shown for copy / load-in-editor."""
    return EXAMPLE_TARGET_PATH.read_text(encoding="utf-8")


WORKSPACE_FILENAMES = {
    "candidates_json": "coco_candidates_all_columns.json",
    "prompts_jsonl": "coco_prompt_payloads.jsonl",
    "scores_jsonl": "coco_scored_raw.jsonl",
    "final_xlsx": "coco_finalized.xlsx",
}


def workspace_dir() -> Path:
    if "workspace_dir" not in st.session_state:
        st.session_state.workspace_dir = Path(
            tempfile.mkdtemp(prefix="coco_ui_"),
        )
    return st.session_state.workspace_dir


def resolve_saved_input(ws: Path) -> Path | None:
    """Most recent coco_input.xls / coco_input.xlsx in the session workspace."""
    hits = sorted(ws.glob("coco_input.*"))
    return hits[-1] if hits else None


def _format_cmd_line(cmd: list[str]) -> str:
    if sys.platform == "win32":
        from subprocess import list2cmdline

        return list2cmdline(cmd)
    import shlex

    return shlex.join(cmd)


def _stream_subprocess_to_placeholder(
    cmd: list[str],
    log_placeholder: Any,
    *,
    prepend: str = "",
    max_visible_chars: int = 200_000,
) -> tuple[int, str]:
    """Run command, merge stderr into stdout, stream lines into log_placeholder. Returns (exit_code, full_output)."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    chunks: list[str] = [prepend]
    assert proc.stdout is not None
    try:
        for line in iter(proc.stdout.readline, ""):
            chunks.append(line)
            text = "".join(chunks)
            if len(text) > max_visible_chars:
                text = text[-max_visible_chars:]
            try:
                log_placeholder.code(text, language="text", height=400)
            except TypeError:
                log_placeholder.code(text, language="text")
    finally:
        proc.stdout.close()

    exit_code = proc.wait()
    if exit_code is None:
        exit_code = -1
    full = "".join(chunks)
    return exit_code, full


def main() -> None:
    st.set_page_config(page_title="CoCo AI Pipeline", layout="wide")
    st.title("CoCo AI — Pipeline")
    st.caption(
        "Run the valuation workflow without the terminal: upload a CapIQ export, "
        "then step through preprocessing → prompts → scoring → final Excel."
    )

    ws = workspace_dir()
    with st.sidebar:
        st.subheader("Session workspace")
        st.code(str(ws), language="text")
        if st.button("Reset workspace"):
            shutil.rmtree(ws, ignore_errors=True)
            del st.session_state["workspace_dir"]
            st.rerun()

    uploaded = st.file_uploader(
        "CapIQ Excel (.xls / .xlsx)",
        type=["xls", "xlsx"],
    )
    paths = {k: ws / name for k, name in WORKSPACE_FILENAMES.items()}
    inp = resolve_saved_input(ws)

    up_col, go_col = st.columns([3, 1])
    with up_col:
        load_btn = st.button("Save upload to workspace", type="secondary")
    with go_col:
        run_all_btn = st.button("Run full pipeline", type="primary")

    if uploaded is not None and load_btn:
        suffix = (Path(uploaded.name).suffix or ".xls").lower()
        dest = ws / f"coco_input{suffix}"
        dest.write_bytes(uploaded.getvalue())
        st.success(f"Saved to `{dest.name}` in the workspace.")

    inp = resolve_saved_input(ws)

    sheet_help = (
        "This is the **tab name inside Excel** (bottom of the window), "
        "**not** the workbook file name."
    )
    if inp is not None and inp.exists():
        try:
            tab_names = pd.ExcelFile(inp).sheet_names
            default_idx = tab_names.index("Screening") if "Screening" in tab_names else 0
            sheet = st.selectbox(
                "Worksheet tab to read",
                options=tab_names,
                index=default_idx,
                help=sheet_help,
            )
        except Exception as exc:
            st.warning(f"Could not list worksheet tabs ({exc}); enter the tab name manually.")
            sheet = st.text_input(
                "Worksheet tab to read",
                value="Screening",
                help=sheet_help,
            )
    else:
        sheet = st.text_input(
            "Worksheet tab to read",
            value="Screening",
            help=sheet_help + " Upload and save first to pick from the list.",
        )

    sheet = str(sheet).strip() if sheet else ""

    st.divider()
    st.subheader("Target company profile")
    st.markdown(
        "The model compares each CapIQ candidate to **this text** (replacing `{PASTE TARGET TXT HERE}` in "
        "`prompts/Core/compare_prompt.txt`). Write it yourself or generate it elsewhere, then paste below."
    )

    how = st.expander("How to create a profile (recommended workflow)", expanded=False)
    with how:
        st.markdown(
            """
1. Collect source material — company website, annual report, Investor Relations **About** pages.
2. In **Microsoft Copilot** (or your preferred assistant), paste excerpts or summaries of that material.
3. Ask the assistant to draft a **structured target-company profile** suitable for **comparable-company scoring**.
4. Request that it mirror the **section layout and depth** shown in **Example profile** below
   *(company name, geography, sector, core business description, segments, business model,
   scale indicators, strategic positioning, keywords for AI comparison)*.
5. Edit for accuracy, then paste the final text into the box under **Your target profile**.
            """.strip()
        )

    ex = st.expander("Example profile — copy as a template", expanded=False)
    with ex:
        st.caption(f"Bundled file: `{EXAMPLE_TARGET_PATH.relative_to(REPO_ROOT)}`")
        try:
            st.code(load_example_target_text(), language="markdown")
        except OSError:
            st.warning(f"Could not read example file: {EXAMPLE_TARGET_PATH}")

    if "target_profile_text" not in st.session_state:
        st.session_state.target_profile_text = ""

    bc1, bc2 = st.columns([1, 1])
    with bc1:
        if st.button("Load example into editor", key="load_target_example"):
            try:
                st.session_state.target_profile_text = load_example_target_text()
            except OSError as exc:
                st.error(f"Could not load example: {exc}")
            else:
                st.rerun()
    with bc2:
        if st.button("Clear editor", key="clear_target_profile"):
            st.session_state.target_profile_text = ""
            st.rerun()

    st.text_area(
        "Your target profile (required for step 2 + full run)",
        height=320,
        key="target_profile_text",
        placeholder="Paste your target company profile here…",
        help="Non-empty text is written to the session workspace and passed to run_build_prompts.py via --target-company.",
    )

    max_rows_val = st.number_input(
        "Scoring — max rows (0 = score all)",
        min_value=0,
        max_value=10_000,
        value=0,
        step=1,
        help="Use a small number for pilots; full run uses every prompt row.",
    )
    max_rows_arg = (
        []
        if int(max_rows_val) <= 0
        else ["--max-rows", str(int(max_rows_val))]
    )

    if run_all_btn and (inp is None or not inp.exists()):
        st.warning("Upload a file and click **Save upload to workspace** first.")
        st.stop()

    if run_all_btn and not str(st.session_state.get("target_profile_text", "")).strip():
        st.warning(
            "Paste a **target company profile** above (or click **Load example into editor**) "
            "before **Run full pipeline**."
        )
        st.stop()

    if inp is not None and inp.exists():
        target_company_path = ws / "target_company_user.txt"

        def write_target_company_file() -> None:
            txt = str(st.session_state.get("target_profile_text", "")).strip()
            if not txt:
                st.error("Target profile is empty. Paste text or load the example.")
                st.stop()
            target_company_path.write_text(txt + "\n", encoding="utf-8")

        steps = (
            (
                "1) Excel → JSON",
                Path("src/preprocessing/excel_json.py"),
                [
                    "--input",
                    str(inp),
                    "--output",
                    str(paths["candidates_json"]),
                    "--sheet",
                    sheet.strip() or "Screening",
                ],
            ),
            (
                "2) Build prompts",
                Path("src/preprocessing/run_build_prompts.py"),
                [
                    "--candidates-json",
                    str(paths["candidates_json"]),
                    "--output-jsonl",
                    str(paths["prompts_jsonl"]),
                    "--target-company",
                    str(target_company_path),
                ],
            ),
            (
                "3) Scoring (OpenAI)",
                Path("src/scoring/run_score_batch.py"),
                [
                    "--prompts-jsonl",
                    str(paths["prompts_jsonl"]),
                    "--output-jsonl",
                    str(paths["scores_jsonl"]),
                    "--config",
                    "secrets/scoring_config.json",
                    *max_rows_arg,
                ],
            ),
            (
                "4) Final Excel",
                Path("src/postprocessing/build_final_excel.py"),
                [
                    "--input-excel",
                    str(inp),
                    "--scores-jsonl",
                    str(paths["scores_jsonl"]),
                    "--output-excel",
                    str(paths["final_xlsx"]),
                    "--sheet",
                    sheet.strip() or "Screening",
                ],
            ),
        )

        num_steps = len(steps)

        st.divider()
        st.markdown("##### Live pipeline output")
        st.caption(
            " Streams stdout/stderr (like cmd). Progress lines during scoring show as they arrive "
            "(`python -u` + unbuffered env)."
        )
        status_placeholder = st.empty()
        cmd_placeholder = st.empty()
        terminal_log = st.empty()

        def run_step(
            step_label: str,
            step_index: int,
            script: Path,
            argv: list[str],
            *,
            log_carry: str = "",
        ) -> str:
            header = (
                f"\n{'─'*60}\n"
                f"Step {step_index}/{num_steps}: {step_label}\n"
                f"{'─'*60}\n"
            )
            prepend = log_carry + header

            cmd_run = [
                sys.executable,
                "-u",
                str(REPO_ROOT / script),
                *argv,
            ]
            status_placeholder.markdown(
                f"**Running** step **{step_index}/{num_steps}** — _{step_label}_"
            )
            cmd_placeholder.markdown("**Command:**")
            cmd_placeholder.code(_format_cmd_line(cmd_run), language="text")

            exit_code, full_text = _stream_subprocess_to_placeholder(
                cmd_run,
                terminal_log,
                prepend=prepend,
            )

            status_placeholder.markdown(
                f"**Finished** step **{step_index}/{num_steps}** — {step_label} — exit `{exit_code}`"
            )
            if exit_code != 0:
                st.error(f"Step failed: {step_label}")
                st.stop()
            return full_text

        manual_exp = st.expander("Run steps individually", expanded=False)
        with manual_exp:
            if st.button("Run step 1 only", key="step1"):
                label_a, script, argv = steps[0]
                run_step(label_a, 1, script, argv, log_carry="")
            if st.button("Run step 2 only", key="step2"):
                write_target_company_file()
                label_b, script, argv = steps[1]
                run_step(label_b, 2, script, argv, log_carry="")
            if st.button("Run step 3 only", key="step3"):
                label_c, script, argv = steps[2]
                run_step(label_c, 3, script, argv, log_carry="")
            if st.button("Run step 4 only", key="step4"):
                label_d, script, argv = steps[3]
                run_step(label_d, 4, script, argv, log_carry="")

        if run_all_btn:
            write_target_company_file()
            cumulative_log = ""
            for step_index, (step_label, script, argv) in enumerate(steps, start=1):
                cumulative_log = run_step(
                    step_label,
                    step_index,
                    script,
                    argv,
                    log_carry=cumulative_log,
                )
            st.success("Pipeline finished.")

        if paths["final_xlsx"].exists():
            st.download_button(
                label="Download finalized Excel",
                data=paths["final_xlsx"].read_bytes(),
                file_name="coco_finalized.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="download_final",
            )
    elif not load_btn:
        st.info(
            "Upload a CapIQ file, click **Save upload to workspace**, then "
            "**Run full pipeline** (or expand **Run steps individually**)."
        )


if __name__ == "__main__":
    main()
