# Benchmark feedback loop

This benchmark creates a repeatable feedback loop for improving `pdf_to_epub.py`.

## What it checks

- EPUB structural sanity (zip, metadata title, TOC, broken image references)
- Text fidelity proxies (required phrases, phrase order, text coverage)
- Math quality proxies (MathML presence, raw LaTeX leaks, replacement characters)
- Kindle compatibility (`ebook-convert` EPUB -> AZW3 if available)
- Optional LLM qualitative judge (reading order, math legibility, figure integrity)

## Setup

```bash
pip install -r requirements.txt
```

Recommended tools in PATH:

- `pandoc`
- `ebook-convert` (Calibre) for Kindle conversion checks
- `epubcheck` for EPUB conformance checks

## Download benchmark PDFs

```bash
python benchmark/download_testset.py
```

Manifest location: `testset/manifest.json`.

## Run benchmark

```bash
python benchmark/run_benchmark.py
```

With optional OpenAI qualitative judge:

```bash
OPENAI_API_KEY=... python benchmark/run_benchmark.py --llm-judge
```

Run only one case:

```bash
python benchmark/run_benchmark.py --case attention_is_all_you_need
```

Force fresh OCR:

```bash
python benchmark/run_benchmark.py --force-ocr
```

## Output structure

- OCR cache: `testset/cache/`
- Run artifacts: `testset/runs/<timestamp>/`
- Run summary JSON: `summary.json`
- Human report: `report.md`

## Iteration workflow

1. Run benchmark and inspect `report.md`
2. Patch `pdf_to_epub.py`
3. Run benchmark again
4. Compare regressions/improvements in the next run report
