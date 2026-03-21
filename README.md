# pdf-to-epub

Convert academic PDFs (local or URL) into Kindle-friendly EPUBs using OCR + Pandoc, with a benchmark feedback loop to continuously improve quality.

## Features

- Converts a PDF URL or local PDF file to EPUB
- Caches OCR responses for fast iterations
- Preserves figures and improves math rendering quality
- Avoids noisy filename captions under images
- Includes a benchmark suite (10 ArXiv papers) for regression testing
- Optional LLM quality judge for visual/reading-order scoring

## Requirements

- Python 3.10+
- `pandoc` in PATH

## Python environment setup (recommended)

If you are new to Python, set up an isolated environment first.

### Option A: `venv` (built into Python)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

When you are done:

```bash
deactivate
```

### Option B: `conda`

```bash
conda create -n pdf-to-epub python=3.11 -y
conda activate pdf-to-epub
pip install -r requirements.txt
```

Optional but recommended:

- `ebook-convert` (Calibre) for Kindle compatibility checks in benchmark
- `epubcheck` for EPUB conformance checks in benchmark

## Configure Mistral API key

The converter uses Mistral OCR for document extraction.

You can get started on Mistral's free tier.

1. Create an account (or sign in) at: https://console.mistral.ai/home
2. Create an API key in the console.
3. Export it in your shell:

```bash
export MISTRAL_API_KEY="your_key_here"
```

4. (Optional) persist it in your shell profile:

```bash
echo 'export MISTRAL_API_KEY="your_key_here"' >> ~/.zshrc
source ~/.zshrc
```

You can also use a `.env` file in this repo root:

```bash
echo 'MISTRAL_API_KEY=your_key_here' > .env
```

## Usage

Convert from URL:

```bash
python pdf_to_epub.py https://arxiv.org/pdf/2511.10395
```

Convert from local file:

```bash
python pdf_to_epub.py ~/Downloads/paper.pdf
```

Output files are saved to `output/` by default.

Useful options:

```bash
python pdf_to_epub.py <url_or_pdf> \
  --output-dir output \
  --cache-dir .ocr-cache \
  --case-id custom_case_id \
  --force-ocr
```

## Benchmark feedback loop

### Download benchmark PDFs

```bash
python benchmark/download_testset.py
```

### Run full benchmark

```bash
python benchmark/run_benchmark.py
```

### Run selected cases

```bash
python benchmark/run_benchmark.py --case attention_is_all_you_need --case bert_pretraining
```

### Optional LLM judge (OpenAI)

```bash
export OPENAI_API_KEY="your_key_here"
python benchmark/run_benchmark.py --llm-judge
```

See benchmark details in `benchmark/README.md`.

## Add a new benchmark case

1. Edit `testset/manifest.json`.
2. Add a new object under `cases` with:
   - `id` (unique slug)
   - `title`
   - `pdf_file` (for example `pdfs/my_case.pdf`)
   - `source_url` (download URL)
   - `expectations` (math/images/phrases/order)
3. Download it:

```bash
python benchmark/download_testset.py
```

4. Run only the new case:

```bash
python benchmark/run_benchmark.py --case my_case_id
```

## Ask Codex / Claude Code to iterate automatically

Use this prompt template to run a strict feedback loop for your use case:

```text
I want you to optimize pdf_to_epub.py for this benchmark case: <CASE_ID>.

Workflow:
1) Run benchmark only for <CASE_ID>.
2) Inspect failures (especially math, reading order, image integrity, missing symbols).
3) Make one focused code change.
4) Re-run the same benchmark.
5) Repeat until score is 100 and no critical failures.
6) Stop after 10 iterations if unresolved and summarize blockers.

Constraints:
- Do not relax benchmark thresholds to "pass".
- Fix root causes, not superficial patches.
- Keep changes minimal and explain each iteration briefly.
```

You can also ask for multi-case optimization:

```text
Optimize for cases: attention_is_all_you_need, diffusion_models, bert_pretraining.
Run iterative loop with regression protection:
- no existing passing case may regress.
- stop at 10 iterations and report remaining gaps.
```

## Repository layout

```text
.
├── pdf_to_epub.py
├── requirements.txt
├── benchmark/
│   ├── run_benchmark.py
│   ├── validators.py
│   ├── report.py
│   ├── llm_judge.py
│   └── download_testset.py
└── testset/
    └── manifest.json
```
