# Visualize Experiments

## Purpose

`visualize_experiments.py` reads the same `experiment_log.jsonl` as `analyze_experiments.py` and writes **vector PDF charts** intended for inclusion in a LaTeX document. It is a companion to the text/CSV report — same data, graphical view.

Nine plot functions produce up to 23 PDFs:

- **Model comparison** (6 files) — mean per metric by model with ±1 std error bars: cost, summed LLM inference time, wall-clock time, tool calls, an inference-vs-wall-clock side-by-side, and normalized tokens.
- **Scenario performance** (4 files) — inference time, wall-clock time, cost, and tool calls grouped by scenario, with model as the hue. Error bands span the observed **min–max** of the runs in each group (`errorbar=('pi', 100)`), not ±1 SD.
- **Invalid commands** (1 file) — total invalid SR Linux commands per scenario × model.
- **Correctness** (1 file) — `found_rate` per scenario × model, sourced from your manual verdicts in `evaluation_log.jsonl` (the `found_issue` field). This is the **source of truth for what was successful**, not the `success` field — `success` only means a run didn't crash (DNF timeouts are `success=false`). Only evaluated runs count; the chart is skipped if you haven't evaluated anything yet.
- **Cost vs. correctness** (1 file) — mean cost per run (log x) against found rate, one dot per model, with the Pareto frontier as a dashed step line. Also built on the `found_issue` verdicts.
- **Time to diagnosis** (1 file) — wall-clock time to a *correct* diagnosis per model (verdicts + wall clock; DNF runs excluded as right-censored).
- **Distributions (per model)** (4 files) — boxplots (with individual points overlaid) showing run-to-run variability per model.
- **Distributions (per scenario)** (4 files) — per-model panels of the raw runs per scenario (one dot = one run, tick at the median), showing how each metric spreads within a troubleshooting scenario.
- **Scenario time-to-diagnosis** (1 file) — the time-to-diagnosis view broken down per scenario, same per-model panel layout.

The wall-clock and time-to-diagnosis figures require `wall_clock_durations.jsonl` (see `--wall-clock-file` below) and are skipped without it.

---

## Usage

```bash
# All charts to ./figures/ as PDF
uv run python visualize_experiments.py

# Filter to a single scenario
uv run python visualize_experiments.py --scenario intf-down

# Write to a custom directory (e.g. inside a paper repo)
uv run python visualize_experiments.py --out paper/figures

# Use PNG instead of PDF (for slides, web, GitHub previews)
uv run python visualize_experiments.py --format png

# Different log file
uv run python visualize_experiments.py --file path/to/other.jsonl
```

### CLI options

| Flag                 | Default                 | Description                                                        |
|----------------------|-------------------------|--------------------------------------------------------------------|
| `--file` / `-f`      | `experiment_log.jsonl`  | Path to the JSONL log file                                         |
| `--scenario` / `-s`  | *(all)*                 | Filter to a single scenario label                                  |
| `--out` / `-o`       | `figures`               | Output directory (created if missing)                              |
| `--format`           | `pdf`                   | `pdf` (recommended for LaTeX) or `png`                             |
| `--eval-file` / `-e` | `evaluation_log.jsonl`  | Manual verdicts for the correctness / cost-vs-correctness / time-to-diagnosis charts; missing file = skipped |
| `--wall-clock-file` / `-w` | `wall_clock_durations.jsonl` | Recovered wall-clock durations (from `recover_wall_clock.py`); enables the wall-clock and time-to-diagnosis figures, missing file = those are skipped |

---

## Generated files

Running with defaults produces up to 23 files in `figures/` (charts marked *verdicts*
are omitted when there are no verdicts yet; charts marked *wall clock* are omitted
without `wall_clock_durations.jsonl`):

| File                                       | Chart                                       |
|--------------------------------------------|---------------------------------------------|
| `model_comparison_cost.pdf`                | Mean cost per session, by model             |
| `model_comparison_duration.pdf`            | Mean **summed LLM inference time** per session, by model (y-label "LLM inference time, summed (s)") — not elapsed time; see `model_comparison_time_metrics.pdf` for the contrast |
| `model_comparison_wall_clock.pdf`          | Mean wall-clock time per session, by model *(wall clock)* |
| `model_comparison_time_metrics.pdf`        | Summed inference time and wall-clock time side by side per model — the gap shows orchestration concurrency (summed inference can exceed wall clock) *(wall clock)* |
| `model_comparison_tokens.pdf`              | Mean **normalized** input/output tokens (Generation API), by model. Comparable across models that tokenize differently — but sessions where a generation was dropped have NaN normalized counts and are **silently omitted** from these bars |
| `model_comparison_tool_calls.pdf`          | Mean tool calls per session, by model       |
| `scenario_performance_duration.pdf`        | Summed inference time grouped by scenario × model |
| `scenario_performance_wall_clock.pdf`      | Wall-clock time grouped by scenario × model *(wall clock)* |
| `scenario_performance_cost.pdf`            | Cost grouped by scenario × model            |
| `scenario_performance_tool_calls.pdf`      | Tool calls grouped by scenario × model      |
| `invalid_commands.pdf`                     | Total invalid SR Linux commands per scenario × model |
| `correctness_found_rate.pdf`               | Found rate (manual verdicts) by scenario × model *(verdicts)* |
| `cost_vs_correctness.pdf`                  | Mean cost (log x) vs. found rate per model, with Pareto frontier *(verdicts)* |
| `time_to_diagnosis.pdf`                    | Wall-clock time to a correct diagnosis per model; DNF runs excluded *(verdicts, wall clock)* |
| `distribution_duration.pdf`                | Summed-inference-time boxplot per model     |
| `distribution_wall_clock.pdf`              | Wall-clock boxplot per model *(wall clock)* |
| `distribution_tokens.pdf`                  | Normalized-input-tokens boxplot per model   |
| `distribution_cost.pdf`                    | Cost boxplot per model                      |
| `scenario_distribution_cost.pdf`           | Cost per scenario, per-model panels         |
| `scenario_distribution_duration.pdf`       | Summed inference time per scenario, per-model panels |
| `scenario_distribution_wall_clock.pdf`     | Wall-clock time per scenario, per-model panels *(wall clock)* |
| `scenario_distribution_tokens.pdf`         | Normalized input tokens per scenario, per-model panels |
| `scenario_time_to_diagnosis.pdf`           | Time to correct diagnosis per scenario, per-model panels *(verdicts, wall clock)* |

All PDFs are **vector** — they scale to any column width without aliasing and stay sharp at any zoom level.

---

## Why PDF (not PNG) for LaTeX

`\includegraphics` accepts both formats, but PDF is the right default for a thesis/paper:

- **Vector geometry.** Axes, ticks, bars, and text are drawn as vector primitives. Resizing to `\linewidth`, `\columnwidth`, or `0.5\textwidth` never produces blurry edges.
- **Embedded TrueType fonts** (`pdf.fonttype: 42` in the script). Text in the figure stays selectable and copyable in the final paper PDF; reviewers can search inside figures.
- **Smaller files for plots** with few data points (the current charts are ~10 KB each).
- **Compatible with both** `pdflatex` and `lualatex`/`xelatex` out of the box. (`latex` → `dvipdf` workflows that require EPS are uncommon now; if you need EPS, add `--format` support or post-convert with `pdftops -eps`.)

Use `--format png` only when targeting a venue that explicitly requires it, or for non-LaTeX outputs (slides, web).

---

## Including in a LaTeX document

### Preamble

```latex
\usepackage{graphicx}
\graphicspath{{figures/}}    % so you can write just the filename
```

If your `figures/` directory lives elsewhere (e.g. one level up from `main.tex`), set the path accordingly: `\graphicspath{{../figures/}}`.

### Single figure (one column)

```latex
\begin{figure}[t]
  \centering
  \includegraphics[width=\linewidth]{model_comparison_cost.pdf}
  \caption{Mean cost per session by model on the
           client1\,$\leftrightarrow$\,client3 communication scenario.
           Error bars show $\pm 1$ standard deviation across five runs.}
  \label{fig:model-cost}
\end{figure}
```

Reference it in text with `Figure~\ref{fig:model-cost}`.

> **Caption accuracy:** the ±1 SD wording applies to the `model_comparison_*`
> charts only. The `scenario_performance_*` charts draw the observed
> **min–max range** of the runs in each group (`errorbar=('pi', 100)`), so
> caption those as "bands span the min–max of five runs", not as ±1 SD.

### Two figures side by side

For comparing two related metrics (e.g. cost and duration) on the same row:

```latex
\usepackage{subcaption}   % preamble

\begin{figure}[t]
  \centering
  \begin{subfigure}[t]{0.48\linewidth}
    \centering
    \includegraphics[width=\linewidth]{model_comparison_cost.pdf}
    \caption{Cost per session.}
    \label{fig:model-cost-sub}
  \end{subfigure}
  \hfill
  \begin{subfigure}[t]{0.48\linewidth}
    \centering
    \includegraphics[width=\linewidth]{model_comparison_duration.pdf}
    \caption{Duration per session.}
    \label{fig:model-duration-sub}
  \end{subfigure}
  \caption{Per-model performance averages, $\pm 1$~std.}
  \label{fig:model-performance}
\end{figure}
```

### Distribution figure with discussion

```latex
\begin{figure}[t]
  \centering
  \includegraphics[width=0.9\linewidth]{distribution_cost.pdf}
  \caption{Per-session cost distribution by model. Box shows the
           interquartile range; whiskers extend to 1.5\,IQR;
           individual runs are overlaid as black dots.}
  \label{fig:dist-cost}
\end{figure}
```

### Suppressing the white background

Seaborn's `whitegrid` theme already has a white background, so `\includegraphics` blends into the page. If you switch the theme and end up with a grey figure background, regenerate the PDFs rather than editing the LaTeX.

---

## Regenerating after new experiments

Every time you append more sessions to `experiment_log.jsonl` (or to a per-scenario log file), re-run the script. `figures/` is overwritten in place — LaTeX picks up the new PDFs on the next build without any source changes:

```bash
uv run python experiment_runner.py -m <model> -s <scenario>   # scenario folder provides queries.txt
uv run python visualize_experiments.py --scenario <scenario>
# rebuild the paper
latexmk -pdf main.tex
```

(`-f` is unnecessary here — and ignored — whenever `-s` names a real folder under
`scenarios/`; the runner loads the folder's own `queries.txt`.)

If you want a permanent snapshot of figures for a specific paper draft, point `--out` at a directory you commit to the paper repo:

```bash
uv run python visualize_experiments.py --out ../thesis/figures/experiments-2026-05
```

---

## Dependencies

Requires `matplotlib` and `seaborn`, both listed in `pyproject.toml`. After pulling a fresh clone or updating dependencies, run `uv sync` once. No system packages (no LaTeX needed at figure-generation time — only at paper-compilation time).
