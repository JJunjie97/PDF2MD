# CLI reference

Run the wrapper from the skill directory or by absolute path:

```powershell
scripts\mineru-pdf.cmd inspect "D:\docs\paper.pdf"
scripts\mineru-pdf.cmd search "D:\docs\paper.pdf" --query "power supply current" --top-k 8
scripts\mineru-pdf.cmd prepare "D:\docs\paper.pdf" --query "What is the maximum input voltage?"
scripts\mineru-pdf.cmd convert "D:\docs\paper.pdf" --pages "3-8" --profile balanced
scripts\mineru-pdf.cmd status "D:\docs\paper.pdf"
```

All commands emit one UTF-8 JSON object on stdout. `prepare` and `convert` return one `markdown` path and one `images_dir`. Read those only. Internal diagnostic output is stored under `<pdf-stem>.mineru/raw` and may be written to stderr.

The public output is intentionally small:

```text
<pdf-stem>.mineru/
├── <pdf-stem>.md
├── images/
└── raw/
```

`raw/` contains indexes, logs, manifests, and cached Markdown/image selections needed for fast reuse. Upstream JSON, model output, content lists, and original PDF copies are not requested. Do not inspect `raw/` during normal reading.

## Commands

- `inspect`: Return page count, PDF kind, outline entries, contents-page candidates, sampled character counts, and estimated tokens without loading MinerU.
- `search`: Extract or reuse native page text and return ranked page snippets. It is unsuitable for image-only pages.
- `prepare`: Inspect and search, choose a small page set using the token/page budget, then convert it.
- `convert`: Convert all pages or an explicit 1-based page expression such as `3`, `3-8`, or `1-3,8,12-15`.
- `status`: Report the public Markdown/image paths and compact cache state without exposing internal selection details.

## Important options

- `--profile fast|balanced|accurate`: `fast` uses Pipeline, `balanced` uses Hybrid medium without image/chart analysis, and `accurate` uses Hybrid high with image/chart analysis.
- `--token-budget N`: Default `12000`; guides `prepare` page selection.
- `--context-pages N`: Default `1`.
- `--max-pages N`: Default `12` for automatic preparation.
- `--top-k N`: Default `8` for search.
- `--force`: Ignore an existing matching selection and convert again.
- `--timeout N`: MinerU timeout in seconds; default `1800`.

Exit codes: `0` success, `2` invalid arguments, `3` invalid input, `4` unreadable/encrypted PDF, `5` missing runtime, `6` conversion failure, `7` timeout, `8` output/cache error.

When search returns no hits for a scanned PDF, use `convert` on likely front matter or a user-specified page range. Do not repeat an identical failed command.
