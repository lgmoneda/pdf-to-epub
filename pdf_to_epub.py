import argparse
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes

from dotenv import load_dotenv

DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.DOTALL)
INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)|\\\((.+?)\\\)", re.DOTALL)
SPACED_LETTERS_RE = re.compile(r"\b(?:[A-Za-z]\s+){2,}[A-Za-z]\b")
DATE_TITLE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
IMAGE_MARKDOWN_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
FILENAME_ALT_RE = re.compile(r"^img[-_\s]?\d+(?:[-_]\d+)?(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _safe_filename(value: str, max_length: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = cleaned.strip("_")
    if not cleaned:
        return "untitled"
    return cleaned[:max_length]


def _default_case_id(input_source: str) -> str:
    if _is_url(input_source):
        digest = hashlib.sha1(input_source.encode("utf-8")).hexdigest()[:12]
        return f"url_{digest}"
    return _safe_filename(Path(input_source).stem)


def _get_mistral_client() -> Any:
    try:
        from mistralai import Mistral
    except ImportError:
        try:
            from mistralai.client import Mistral
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "Missing dependency 'mistralai'. Install with: pip install -r requirements.txt"
            ) from exc

    load_dotenv()
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY is required to run OCR.")
    return Mistral(api_key=api_key)


def _prepare_document_url(client: Any, input_source: str) -> str:
    if _is_url(input_source):
        return input_source

    source_path = Path(input_source)
    if not source_path.exists():
        raise FileNotFoundError(f"PDF file not found: {source_path}")

    with source_path.open("rb") as handle:
        uploaded_pdf = client.files.upload(
            file={
                "file_name": source_path.name,
                "content": handle,
            },
            purpose="ocr",
        )

    return client.files.get_signed_url(file_id=uploaded_pdf.id).url


def _ocr_response_to_dict(ocr_response: Any) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for index, page in enumerate(ocr_response.pages):
        images = []
        for image in page.images:
            images.append(
                {
                    "id": image.id,
                    "image_base64": image.image_base64,
                }
            )

        pages.append(
            {
                "index": index,
                "markdown": page.markdown,
                "images": images,
            }
        )

    return {
        "model": "mistral-ocr-latest",
        "pages": pages,
    }


def extract_title_from_ocr(ocr_data: dict[str, Any]) -> str:
    for page in ocr_data.get("pages", []):
        markdown = page.get("markdown", "")
        lines = markdown.splitlines()
        for line in lines:
            title_candidate = line.strip().replace("#", "").strip()
            if len(title_candidate) <= 5:
                continue
            if DATE_TITLE_RE.match(title_candidate):
                continue
            if not re.search(r"[A-Za-z]", title_candidate):
                continue
            return _safe_filename(title_candidate, max_length=75)
    return "untitled"


def run_ocr(
    input_source: str,
    cache_dir: str | Path | None = None,
    case_id: str | None = None,
    force_ocr: bool = False,
) -> tuple[dict[str, Any], bool, str]:
    resolved_case_id = case_id or _default_case_id(input_source)

    cache_path: Path | None = None
    if cache_dir:
        cache_path = Path(cache_dir) / f"{resolved_case_id}.json"
        if cache_path.exists() and not force_ocr:
            return json.loads(cache_path.read_text(encoding="utf-8")), True, resolved_case_id

    client = _get_mistral_client()
    document_url = _prepare_document_url(client, input_source)
    ocr_response = client.ocr.process(
        model="mistral-ocr-latest",
        document={"type": "document_url", "document_url": document_url},
        include_image_base64=True,
    )

    ocr_data = _ocr_response_to_dict(ocr_response)

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(ocr_data), encoding="utf-8")

    return ocr_data, False, resolved_case_id


def _decode_data_uri(payload: str) -> bytes:
    if "," not in payload:
        raise ValueError("Invalid image_base64 payload: missing comma separator")

    header, body = payload.split(",", 1)
    if ";base64" in header:
        return base64.b64decode(body)
    return unquote_to_bytes(body)


def _collapse_spaced_letters(value: str) -> str:
    def _join(match: re.Match[str]) -> str:
        return "".join(match.group(0).split())

    return SPACED_LETTERS_RE.sub(_join, value)


def _normalize_latex_expression(value: str) -> str:
    normalized = html.unescape(value)
    normalized = re.sub(r"\\tag\s*\{[^{}]*\}", "", normalized)
    normalized = re.sub(
        r"\\(?:bigg|Bigg|big|Big|bigl|bigr|Bigl|Bigr|biggl|biggr|Biggl|Biggr)\s*",
        "",
        normalized,
    )
    normalized = normalized.replace(r"\bm{", r"\mathbf{")

    def _operatorname_fix(match: re.Match[str]) -> str:
        inner = match.group(1)
        return r"\operatorname{" + _collapse_spaced_letters(inner) + "}"

    normalized = re.sub(r"\\operatorname\s*\{([^{}]*)\}", _operatorname_fix, normalized)
    normalized = _collapse_spaced_letters(normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _has_balanced_braces(value: str) -> bool:
    balance = 0
    escaped = False
    for char in value:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            balance += 1
        elif char == "}":
            balance -= 1
            if balance < 0:
                return False
    return balance == 0


def _latex_to_plain_text(value: str) -> str:
    plain = html.unescape(value)
    plain = plain.replace(r"\texttt{", "")
    plain = plain.replace(r"\mathrm{", "")
    plain = plain.replace(r"\mathbf{", "")
    plain = plain.replace(r"\operatorname{", "")
    plain = plain.replace("\\", "")
    plain = plain.replace("{", "")
    plain = plain.replace("}", "")
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain.replace("`", "'")


def _should_demote_from_math(value: str) -> bool:
    if not _has_balanced_braces(value):
        return True
    if "<" in value or ">" in value:
        return True
    if value.count(r"\texttt") >= 2:
        return True
    return False


def _normalize_markdown_math(markdown: str) -> str:
    normalized = html.unescape(markdown)
    normalized = re.sub(r"(?<=\d)\$\$(?=\d)", "×", normalized)

    def _replace_display(match: re.Match[str]) -> str:
        expression = match.group(1) if match.group(1) is not None else match.group(2)
        if expression is None:
            return match.group(0)
        cleaned = _normalize_latex_expression(expression)
        if _should_demote_from_math(cleaned):
            return f"`{_latex_to_plain_text(cleaned)}`"
        if match.group(1) is not None:
            return f"$${cleaned}$$"
        return f"\\[{cleaned}\\]"

    def _replace_inline(match: re.Match[str]) -> str:
        expression = match.group(1) if match.group(1) is not None else match.group(2)
        if expression is None:
            return match.group(0)
        cleaned = _normalize_latex_expression(expression)
        if _should_demote_from_math(cleaned):
            return f"`{_latex_to_plain_text(cleaned)}`"
        if match.group(1) is not None:
            return f"${cleaned}$"
        return f"\\({cleaned}\\)"

    normalized = DISPLAY_MATH_RE.sub(_replace_display, normalized)
    normalized = INLINE_MATH_RE.sub(_replace_inline, normalized)
    return normalized


def _normalize_markdown_images(markdown: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        alt_text = match.group(1).strip()
        image_target = match.group(2).strip()

        image_name = Path(image_target).name
        image_stem = Path(image_name).stem

        if alt_text in {image_name, image_stem} or FILENAME_ALT_RE.match(alt_text):
            return f"![]({image_target})"

        return match.group(0)

    return IMAGE_MARKDOWN_RE.sub(_replace, markdown)


def _save_image(image_payload: dict[str, Any], output_dir: Path, fallback_name: str) -> str:
    raw_name = image_payload.get("id") or fallback_name
    file_name = Path(raw_name).name if raw_name else fallback_name
    if not file_name:
        file_name = fallback_name

    target_path = output_dir / file_name
    if not target_path.exists():
        image_bytes = _decode_data_uri(image_payload["image_base64"])
        target_path.write_bytes(image_bytes)

    return target_path.name


def create_markdown_file(ocr_data: dict[str, Any], output_filename: str | Path) -> Path:
    output_path = Path(output_filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("wt", encoding="utf-8") as handle:
        for page_index, page in enumerate(ocr_data.get("pages", [])):
            markdown = page.get("markdown", "")

            for image_index, image in enumerate(page.get("images", [])):
                saved_name = _save_image(
                    image,
                    output_path.parent,
                    fallback_name=f"img-{page_index}-{image_index}.png",
                )
                original_id = image.get("id")
                if original_id and original_id != saved_name:
                    markdown = markdown.replace(original_id, saved_name)

            normalized_markdown = _normalize_markdown_images(markdown)
            normalized_markdown = _normalize_markdown_math(normalized_markdown)
            handle.write(normalized_markdown)
            handle.write("\n\n")

    return output_path


def markdown_to_epub(
    md_file: str | Path,
    epub_file: str | Path,
    epub_title: str,
    author: str,
) -> dict[str, Any]:
    md_path = Path(md_file)
    epub_path = Path(epub_file)

    if not md_path.exists():
        raise FileNotFoundError(f"Markdown file '{md_path}' not found.")

    epub_path.parent.mkdir(parents=True, exist_ok=True)

    process = subprocess.run(
        [
            "pandoc",
            str(md_path),
            "-o",
            str(epub_path),
            "--toc",
            "--standalone",
            "--from",
            "markdown+raw_tex+tex_math_dollars+tex_math_single_backslash",
            "--mathml",
            "--resource-path",
            str(md_path.parent),
            "--metadata",
            "title=" + epub_title.replace("_", " "),
            "--metadata",
            f"author={author}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            returncode=process.returncode,
            cmd=process.args,
            output=process.stdout,
            stderr=process.stderr,
        )

    stderr_output = process.stderr or ""
    math_warning_count = len(re.findall(r"Could not convert TeX math", stderr_output))

    return {
        "math_warning_count": math_warning_count,
        "stderr": stderr_output.strip(),
        "stdout": (process.stdout or "").strip(),
    }


def process_input(
    input_source: str,
    output_epub: str | Path | None = None,
    output_dir: str | Path = "output",
    cache_dir: str | Path | None = None,
    case_id: str | None = None,
    force_ocr: bool = False,
    author: str = "pdf-to-epub",
) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    ocr_data, cache_hit, resolved_case_id = run_ocr(
        input_source=input_source,
        cache_dir=cache_dir,
        case_id=case_id,
        force_ocr=force_ocr,
    )

    title = extract_title_from_ocr(ocr_data)
    markdown_path = output_path / f"{title}.md"
    create_markdown_file(ocr_data, markdown_path)

    if output_epub is None:
        epub_path = output_path / f"{title}.epub"
    else:
        candidate = Path(output_epub)
        epub_path = candidate if candidate.is_absolute() else output_path / candidate

    pandoc_result = markdown_to_epub(markdown_path, epub_path, title, author=author)

    return {
        "title": title,
        "markdown_file": str(markdown_path),
        "epub_file": str(epub_path),
        "cache_hit": cache_hit,
        "case_id": resolved_case_id,
        "pandoc": pandoc_result,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert PDF (local path or URL) to EPUB using Mistral OCR and Pandoc."
    )
    parser.add_argument("input_source", help="PDF file path or URL")
    parser.add_argument("output_epub", nargs="?", default=None, help="Output EPUB file name/path")
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory for generated markdown/images and relative EPUB output path",
    )
    parser.add_argument(
        "--cache-dir",
        default=".ocr-cache",
        help="Directory to store/reuse OCR JSON responses",
    )
    parser.add_argument("--case-id", default=None, help="Stable cache key for repeated conversions")
    parser.add_argument("--force-ocr", action="store_true", help="Ignore OCR cache and call API again")
    parser.add_argument("--author", default="pdf-to-epub", help="EPUB author metadata")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        result = process_input(
            input_source=args.input_source,
            output_epub=args.output_epub,
            output_dir=args.output_dir,
            cache_dir=args.cache_dir,
            case_id=args.case_id,
            force_ocr=args.force_ocr,
            author=args.author,
        )
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
