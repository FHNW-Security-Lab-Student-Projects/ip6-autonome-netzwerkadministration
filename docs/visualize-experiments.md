# Visualize Experiments

## Purpose

`visualize_experiments.py` reads the same `experiment_log.jsonl` as `analyze_experiments.py` and writes **vector PDF charts** intended for inclusion in a LaTeX document. It is a companion to the text/CSV report — same data, graphical view.

Three groups of charts are produced:

- **Model comparison** — mean per metric (cost, duration, tokens, tool calls) with ±1 std error bars.
- **Scenario performance** — same metrics grouped by scenario, with model as the hue.
- **Distributions** — boxplots (with individual points overlaid) showing run-to-run variability per model.

---

## Usage

```bash
# All charts to ./figures/ as PDF
uv run python visualize_experiments.py

# Filter to a single scenario
uv run python visualize_experiments.py --scenario bgp-01

# Write to a custom directory (e.g. inside a paper repo)
uv run python visualize_experiments.py --out paper/figures

# Use PNG instead of PDF (for slides, web, GitHub previews)
uv run python visualize_experiments.py --format png

# Different log file
uv run python visualize_experiments.py --file path/to/other.jsonl
```

### CLI options

| Flag                | Default                 | Description                                       |
|---------------------|-------------------------|---------------------------------------------------|
| `--file` / `-f`     | `experiment_log.jsonl`  | Path to the JSONL log file                        |
| `--scenario` / `-s` | *(all)*                 | Filter to a single scenario label                 |
| `--out` / `-o`      | `figures`               | Output directory (created if missing)             |
| `--format`          | `pdf`                   | `pdf` (recommended for LaTeX) or `png`            |

---

## Generated files

Running with defaults produces ten files in `figures/`:

| File                                       | Chart                                       |
|--------------------------------------------|---------------------------------------------|
| `model_comparison_cost.pdf`                | Mean cost per session, by model             |
| `model_comparison_duration.pdf`            | Mean duration per session, by model         |
| `model_comparison_tokens.pdf`              | Mean input/output tokens, by model          |
| `model_comparison_tool_calls.pdf`          | Mean tool calls per session, by model       |
| `scenario_performance_duration.pdf`        | Duration grouped by scenario × model        |
| `scenario_performance_cost.pdf`            | Cost grouped by scenario × model            |
| `scenario_performance_tool_calls.pdf`      | Tool calls grouped by scenario × model      |
| `distribution_duration.pdf`                | Duration boxplot per model                  |
| `distribution_tokens.pdf`                  | Input-tokens boxplot per model              |
| `distribution_cost.pdf`                    | Cost boxplot per model                      |

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
           Error bars show $\pm 1$ standard deviation across three runs.}
  \label{fig:model-cost}
\end{figure}
```

Reference it in text with `Figure~\ref{fig:model-cost}`.

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
           interquartile range; whiskers extend to the full data
           range; individual runs are overlaid as black dots.}
  \label{fig:dist-cost}
\end{figure}
```

### Suppressing the white background

Seaborn's `whitegrid` theme already has a white background, so `\includegraphics` blends into the page. If you switch the theme and end up with a grey figure background, regenerate the PDFs rather than editing the LaTeX.

---

## Regenerating after new experiments

Every time you append more sessions to `experiment_log.jsonl` (or to a per-scenario log file), re-run the script. `figures/` is overwritten in place — LaTeX picks up the new PDFs on the next build without any source changes:

```bash
uv run python experiment_runner.py -m <model> -s <scenario> -f scenarios/<file>.txt
uv run python visualize_experiments.py --scenario <scenario>
# rebuild the paper
latexmk -pdf main.tex
```

If you want a permanent snapshot of figures for a specific paper draft, point `--out` at a directory you commit to the paper repo:

```bash
uv run python visualize_experiments.py --out ../thesis/figures/experiments-2026-05
```

---

## Dependencies

Requires `matplotlib` and `seaborn`, both listed in `pyproject.toml`. After pulling a fresh clone or updating dependencies, run `uv sync` once. No system packages (no LaTeX needed at figure-generation time — only at paper-compilation time).
