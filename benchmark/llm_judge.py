import json
import os
from pathlib import Path
from typing import Any

import requests

from validators import extract_epub_text_and_assets, strip_markdown


def _build_prompt(
    case_id: str,
    expectations: dict[str, Any],
    markdown_sample: str,
    epub_sample: str,
) -> str:
    return f"""
You are evaluating EPUB conversion quality for Kindle reading.

Case ID: {case_id}
Expectations: {json.dumps(expectations)}

Evaluate the EPUB sample against the Markdown OCR sample.
Focus on:
1. Reading order quality (especially two-column collapse to one coherent flow)
2. Math legibility and symbol preservation
3. Figure/caption continuity and reference integrity
4. Overall Kindle reading quality

Return ONLY valid JSON with this exact structure:
{{
  "reading_order_score": <integer 1-5>,
  "math_legibility_score": <integer 1-5>,
  "figure_integrity_score": <integer 1-5>,
  "overall_score": <integer 1-5>,
  "critical_issues": ["..."],
  "notes": ["..."]
}}

Markdown sample:
---
{markdown_sample}
---

EPUB sample:
---
{epub_sample}
---
""".strip()


def judge_case_with_openai(
    case_id: str,
    markdown_path: Path,
    epub_path: Path,
    expectations: dict[str, Any],
    model: str = "gpt-4.1-mini",
    timeout: int = 60,
) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "enabled": False,
            "skipped": True,
            "reason": "OPENAI_API_KEY not set",
        }

    markdown_text = ""
    if markdown_path.exists():
        markdown_text = strip_markdown(markdown_path.read_text(encoding="utf-8", errors="ignore"))

    epub_data = extract_epub_text_and_assets(epub_path)
    epub_text = epub_data.get("text", "")

    markdown_sample = markdown_text[:6000]
    epub_sample = epub_text[:6000]

    prompt = _build_prompt(
        case_id=case_id,
        expectations=expectations,
        markdown_sample=markdown_sample,
        epub_sample=epub_sample,
    )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a strict EPUB QA evaluator. Return JSON only.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0,
    }

    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )

    if response.status_code >= 300:
        return {
            "enabled": True,
            "skipped": True,
            "reason": f"OpenAI API request failed ({response.status_code})",
            "raw_response": response.text,
        }

    data = response.json()
    content = data["choices"][0]["message"]["content"]

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {
            "enabled": True,
            "skipped": True,
            "reason": "Model response was not valid JSON",
            "raw_response": content,
        }

    reading_order_score = int(parsed.get("reading_order_score", 1))
    math_legibility_score = int(parsed.get("math_legibility_score", 1))
    figure_integrity_score = int(parsed.get("figure_integrity_score", 1))
    overall_score = int(parsed.get("overall_score", 1))

    llm_pass = (
        reading_order_score >= 4
        and math_legibility_score >= 4
        and figure_integrity_score >= 4
        and overall_score >= 4
    )

    return {
        "enabled": True,
        "skipped": False,
        "model": model,
        "pass": llm_pass,
        "reading_order_score": reading_order_score,
        "math_legibility_score": math_legibility_score,
        "figure_integrity_score": figure_integrity_score,
        "overall_score": overall_score,
        "critical_issues": parsed.get("critical_issues", []),
        "notes": parsed.get("notes", []),
    }
