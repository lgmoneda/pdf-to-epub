import argparse
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urljoin, urlparse

import requests
from dotenv import load_dotenv

DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.DOTALL)
INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)|\\\((.+?)\\\)", re.DOTALL)
SPACED_LETTERS_RE = re.compile(r"\b(?:[A-Za-z]\s+){2,}[A-Za-z]\b")
DATE_TITLE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
IMAGE_MARKDOWN_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
FILENAME_ALT_RE = re.compile(r"^img[-_\s]?\d+(?:[-_]\d+)?(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)
ARXIV_NAVIGATION_LINE_RE = re.compile(
    r"^\[(?:About arXiv|Contact|Donate|Help|Login|Subscribe|Skip to main content)\]\([^)]*arxiv\.org[^)]*\)$",
    re.IGNORECASE,
)
HTML_IMAGE_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
MARKDOWN_ATTRIBUTE_BLOCK_RE = re.compile(r"\{(?:#[^}]+)?(?:\s*\.[^}\s]+)+\}")
COLON_FENCE_LINE_RE = re.compile(r"^\s*:{3,}.*$")
COLON_FENCE_WITH_ID_RE = re.compile(r"^\s*:{3,}\s*\{#([^\s}]+)[^}]*\}\s*$")


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _safe_filename(value: str, max_length: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = cleaned.strip("_")
    if not cleaned:
        return "untitled"
    return cleaned[:max_length]


def _slugify(value: str, max_length: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").lower()
    if not slug:
        return "untitled"
    return slug[:max_length].strip("-") or "untitled"


def _clean_title(value: str, max_length: int = 140) -> str:
    cleaned = html.unescape(value)
    cleaned = cleaned.replace("_", " ")
    cleaned = cleaned.replace(":", " - ")
    cleaned = re.sub(r"[<>:\"/\\|?*]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    if not cleaned:
        return "Untitled"
    return cleaned[:max_length].rstrip()


def _artifact_dir_for_title(output_dir: Path, case_id: str, title: str) -> Path:
    case_segment = _safe_filename(case_id, max_length=40)
    title_segment = _slugify(title, max_length=60)
    return output_dir / "artifacts" / f"{case_segment}-{title_segment}"


def _default_case_id(input_source: str) -> str:
    if _is_url(input_source):
        digest = hashlib.sha1(input_source.encode("utf-8")).hexdigest()[:12]
        return f"url_{digest}"
    return _safe_filename(Path(input_source).stem)


def _is_arxiv_url(input_source: str) -> bool:
    if not _is_url(input_source):
        return False

    parsed = urlparse(input_source)
    host = parsed.netloc.lower()
    return host == "arxiv.org" or host.endswith(".arxiv.org")


def _extract_arxiv_id_from_url(input_source: str) -> str | None:
    if not _is_arxiv_url(input_source):
        return None

    path = urlparse(input_source).path.strip("/")
    if not path:
        return None

    for prefix in ("abs/", "pdf/", "html/"):
        if path.startswith(prefix):
            arxiv_id = path[len(prefix) :].strip("/")
            if prefix == "pdf/" and arxiv_id.endswith(".pdf"):
                arxiv_id = arxiv_id[:-4]
            if prefix == "html/" and arxiv_id.endswith(".html"):
                arxiv_id = arxiv_id[:-5]
            return arxiv_id or None

    return None


def _arxiv_pdf_url(input_source: str) -> str | None:
    arxiv_id = _extract_arxiv_id_from_url(input_source)
    if not arxiv_id:
        return None
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def _fetch_arxiv_html_document(input_source: str, timeout_seconds: int = 20) -> tuple[str | None, str | None]:
    arxiv_id = _extract_arxiv_id_from_url(input_source)
    if not arxiv_id:
        return None, None

    html_url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        response = requests.get(
            html_url,
            timeout=timeout_seconds,
            headers={"User-Agent": "pdf-to-epub/1.0"},
            allow_redirects=True,
        )
    except requests.RequestException:
        return None, None

    content_type = response.headers.get("content-type", "").lower()
    looks_like_html = "text/html" in content_type or "<html" in response.text[:500].lower()
    if response.status_code != 200 or not looks_like_html:
        return None, None

    return html_url, response.text


def _normalize_arxiv_html_document(html_document: str) -> str:
    normalized = re.sub(r"<base\b[^>]*>", "", html_document, flags=re.IGNORECASE)
    normalized = re.sub(r"<base\b[^>]*/>", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"alt=[\"']Refer to caption[\"']",
        "alt=\"\"",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized


def _download_html_image_assets(
    html_document: str,
    base_url: str,
    target_dir: Path,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    downloaded = 0
    skipped = 0
    failed = 0

    base_with_slash = base_url if base_url.endswith("/") else f"{base_url}/"
    image_sources = sorted(set(HTML_IMAGE_SRC_RE.findall(html_document)))

    for source in image_sources:
        if source.startswith("data:"):
            skipped += 1
            continue

        if source.startswith("http://") or source.startswith("https://"):
            asset_url = source
            local_rel = urlparse(source).path.lstrip("/")
        else:
            asset_url = urljoin(base_with_slash, source)
            local_rel = source.split("?", 1)[0].split("#", 1)[0].lstrip("/")

        if not local_rel:
            skipped += 1
            continue

        destination = target_dir / local_rel
        if destination.exists():
            skipped += 1
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            response = requests.get(
                asset_url,
                timeout=timeout_seconds,
                headers={"User-Agent": "pdf-to-epub/1.0"},
                allow_redirects=True,
            )
            response.raise_for_status()
            destination.write_bytes(response.content)
            downloaded += 1
        except (requests.RequestException, OSError):
            failed += 1

    return {
        "total_image_references": len(image_sources),
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
    }


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
            return _clean_title(title_candidate, max_length=120)
    return "Untitled"


def extract_title_from_html_document(html_document: str) -> str:
    citation_title_patterns = [
        r"<meta[^>]+name=[\"']citation_title[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+name=[\"']citation_title[\"']",
    ]
    for pattern in citation_title_patterns:
        match = re.search(pattern, html_document, flags=re.IGNORECASE)
        if match:
            title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
            if title and not DATE_TITLE_RE.match(title):
                return _clean_title(title, max_length=120)

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_document, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        title = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()
        title = re.sub(r"\s*\|\s*arXiv.*$", "", title, flags=re.IGNORECASE)
        if title and not DATE_TITLE_RE.match(title):
            return _clean_title(title, max_length=120)

    return "Untitled"


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
    normalized = re.sub(r"\$`([^`]+)`\$", r"$\1$", normalized)
    normalized = re.sub(r"\$\$`([^`]+)`\$\$", r"$$\1$$", normalized)
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

        if alt_text.lower() == "refer to caption":
            return f"![]({image_target})"

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


def _localize_arxiv_links(markdown: str, html_source_url: str | None) -> str:
    if not html_source_url:
        return markdown

    parsed = urlparse(html_source_url)
    base_path = parsed.path.rstrip("/")
    base_url = html_source_url.rstrip("/")

    candidates = [
        f"{base_url}/",
        base_url,
        f"https://arxiv.org{base_path}/",
        f"http://arxiv.org{base_path}/",
        f"https://arxiv.org{base_path}",
        f"http://arxiv.org{base_path}",
        f"{base_path}/",
        base_path,
        "https://arxiv.org",
        "http://arxiv.org",
    ]

    localized = markdown
    for candidate in candidates:
        localized = localized.replace(f"{candidate}#", "#")

    return localized


def _cleanup_arxiv_markdown(markdown: str, html_source_url: str | None = None) -> str:
    lines = markdown.splitlines()

    first_heading_index = next(
        (index for index, line in enumerate(lines) if line.startswith("# ")),
        0,
    )
    if first_heading_index > 0:
        lines = lines[first_heading_index:]

    cleaned_lines: list[str] = []
    in_references = False

    for line in lines:
        stripped = line.strip()
        if ARXIV_NAVIGATION_LINE_RE.match(stripped):
            continue

        fence_with_id_match = COLON_FENCE_WITH_ID_RE.match(stripped)
        if fence_with_id_match:
            cleaned_lines.append(f"<a id=\"{fence_with_id_match.group(1)}\"></a>")
            cleaned_lines.append("")
            continue

        if COLON_FENCE_LINE_RE.match(stripped):
            continue

        anchor_ids = re.findall(r"\{#([^\s}]+)[^}]*\}", line)
        normalized_line = re.sub(r"\{#([^\s}]+)[^}]*\}", "", line)
        normalized_line = MARKDOWN_ATTRIBUTE_BLOCK_RE.sub("", normalized_line)

        for anchor_id in anchor_ids:
            if not anchor_id.startswith("your-spending-needs-attention"):
                cleaned_lines.append(f"<a id=\"{anchor_id}\"></a>")
                cleaned_lines.append("")

        normalized_line = re.sub(r"\[\[([^\[\]]+)\]\]\(([^)]+)\)", r"[\1](\2)", normalized_line)
        normalized_line = re.sub(r"\]\((#[^)\s]+)\s+\"[^\"]*\"\)", r"](\1)", normalized_line)
        normalized_line = re.sub(r"\(#(S\d+)\.E\d+(?:\s+\"[^\"]*\")?\)", r"(#\1)", normalized_line)

        if "](" not in normalized_line:
            normalized_line = re.sub(r"\[\[([^\[\]]+)\]\]", r"\1", normalized_line)
            normalized_line = re.sub(r"\[(\s*)\[(\s*)", "[", normalized_line)
            normalized_line = re.sub(r"(\s*)\](\s*)\]", "]", normalized_line)
            normalized_line = re.sub(r"\]\s*\[", " ", normalized_line)

        normalized_line = re.sub(r"^\s*\[\(\d+\)\]\s*(\$\$.*\$\$)\s*$", r"\1", normalized_line)
        normalized_line = re.sub(r"(\$\$[^$]+)\.(\$\$)", r"\1\2", normalized_line)

        if re.fullmatch(r"[\s\|+\-]{5,}", normalized_line):
            continue

        if re.fullmatch(r"\s*-\s*[•\-–]+\s*", normalized_line):
            continue

        if re.fullmatch(r"\s*-\s*\(\d+\)\s*", normalized_line):
            continue

        if re.fullmatch(r"\s*-\s*\[\s*\]\s*", normalized_line):
            continue

        normalized_line = re.sub(r"\s{2,}", " ", normalized_line).rstrip()
        normalized_line = re.sub(
            r"^(#{1,6})\s*\[([0-9]+(?:\.[0-9]+)?\.?\s*)\](.*)$",
            r"\1 \2\3",
            normalized_line,
        )

        if normalized_line == "## References":
            in_references = True

        if in_references and normalized_line.startswith("-"):
            reference_line = normalized_line[1:].strip()
            reference_segments = [segment.strip() for segment in re.findall(r"\[([^\[\]]+)\]", reference_line)]
            if reference_segments:
                compact_segments = [segment for segment in reference_segments if segment]
                if compact_segments and compact_segments[0].startswith("(") and len(compact_segments) == 1:
                    continue
                if compact_segments:
                    normalized_line = "- " + " ".join(compact_segments)

        cleaned_lines.append(normalized_line)

    cleaned_markdown = "\n".join(cleaned_lines)
    cleaned_markdown = re.sub(r"\n{3,}", "\n\n", cleaned_markdown).strip()
    cleaned_markdown = _localize_arxiv_links(cleaned_markdown, html_source_url)
    return cleaned_markdown + "\n"


def create_markdown_from_html(
    html_file: str | Path,
    output_filename: str | Path,
    html_source_url: str | None = None,
) -> Path:
    html_path = Path(html_file)
    output_path = Path(output_filename)

    if not html_path.exists():
        raise FileNotFoundError(f"HTML file '{html_path}' not found.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    media_dir = output_path.parent / f"{output_path.stem}_media"

    process = subprocess.run(
        [
            "pandoc",
            str(html_path),
            "-o",
            str(output_path),
            "--from",
            "html",
            "--to",
            "markdown+tex_math_dollars+fenced_divs+bracketed_spans+header_attributes+link_attributes",
            "--wrap=none",
            "--resource-path",
            str(html_path.parent),
            "--extract-media",
            str(media_dir),
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

    markdown_text = output_path.read_text(encoding="utf-8")
    markdown_text = _cleanup_arxiv_markdown(markdown_text, html_source_url=html_source_url)
    markdown_text = _normalize_markdown_images(markdown_text)
    output_path.write_text(markdown_text, encoding="utf-8")

    return output_path


def _read_epub_metadata_title(epub_file: str | Path) -> str | None:
    epub_path = Path(epub_file)
    if not epub_path.exists():
        return None

    try:
        with zipfile.ZipFile(epub_path, "r") as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                return None
            opf_path = rootfile.attrib.get("full-path")
            if not opf_path:
                return None

            opf = ET.fromstring(archive.read(opf_path))
            title_element = opf.find(".//{*}metadata/{*}title")
            if title_element is None or not title_element.text:
                return None
            return title_element.text.strip()
    except Exception:
        return None


def markdown_to_epub(
    md_file: str | Path,
    epub_file: str | Path,
    epub_title: str,
    author: str | None = None,
    include_title_page: bool = False,
) -> dict[str, Any]:
    md_path = Path(md_file)
    epub_path = Path(epub_file)

    if not md_path.exists():
        raise FileNotFoundError(f"Markdown file '{md_path}' not found.")

    epub_path.parent.mkdir(parents=True, exist_ok=True)

    pandoc_command = [
        "pandoc",
        str(md_path),
        "-o",
        str(epub_path),
        "--toc",
        "--standalone",
        "--from",
        "markdown+raw_tex+raw_html+fenced_divs+bracketed_spans+header_attributes+link_attributes+tex_math_dollars+tex_math_single_backslash",
        "--mathml",
        "--resource-path",
        str(md_path.parent),
        "--metadata",
        "title=" + epub_title,
    ]

    if author and author.strip():
        pandoc_command.extend(["--metadata", f"author={author.strip()}"])

    pandoc_command.append(f"--epub-title-page={'true' if include_title_page else 'false'}")

    process = subprocess.run(
        pandoc_command,
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
    author: str | None = None,
    include_title_page: bool = False,
    force_pdf: bool = False,
) -> dict[str, Any]:
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    resolved_case_id = case_id or _default_case_id(input_source)

    html_source_url: str | None = None
    html_fallback_error: str | None = None

    if not force_pdf:
        html_source_url, html_document = _fetch_arxiv_html_document(input_source)
        if html_source_url and html_document:
            try:
                normalized_html_document = _normalize_arxiv_html_document(html_document)
                title = extract_title_from_html_document(normalized_html_document)
                artifact_dir = _artifact_dir_for_title(output_path, resolved_case_id, title)
                artifact_dir.mkdir(parents=True, exist_ok=True)

                html_path = artifact_dir / f"{title}.html"
                html_path.write_text(normalized_html_document, encoding="utf-8")

                html_asset_report = _download_html_image_assets(
                    html_document=normalized_html_document,
                    base_url=html_source_url,
                    target_dir=artifact_dir,
                )

                markdown_path = artifact_dir / f"{title}.md"
                create_markdown_from_html(
                    html_path,
                    markdown_path,
                    html_source_url=html_source_url,
                )

                if output_epub is None:
                    epub_path = output_path / f"{title}.epub"
                else:
                    candidate = Path(output_epub)
                    epub_path = candidate if candidate.is_absolute() else output_path / candidate

                pandoc_result = markdown_to_epub(
                    markdown_path,
                    epub_path,
                    title,
                    author=author,
                    include_title_page=include_title_page,
                )

                return {
                    "title": title,
                    "markdown_file": str(markdown_path.resolve()),
                    "epub_file": str(epub_path.resolve()),
                    "epub_metadata_title": _read_epub_metadata_title(epub_path),
                    "cache_hit": False,
                    "case_id": resolved_case_id,
                    "pipeline": "arxiv_html",
                    "html_source_url": html_source_url,
                    "html_file": str(html_path.resolve()),
                    "html_assets": html_asset_report,
                    "artifacts_dir": str(artifact_dir.resolve()),
                    "pandoc": pandoc_result,
                }
            except Exception as exc:
                html_fallback_error = str(exc)

    pdf_input_source = _arxiv_pdf_url(input_source) or input_source

    ocr_data, cache_hit, resolved_case_id = run_ocr(
        input_source=pdf_input_source,
        cache_dir=cache_dir,
        case_id=resolved_case_id,
        force_ocr=force_ocr,
    )

    title = extract_title_from_ocr(ocr_data)
    artifact_dir = _artifact_dir_for_title(output_path, resolved_case_id, title)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = artifact_dir / f"{title}.md"
    create_markdown_file(ocr_data, markdown_path)

    if output_epub is None:
        epub_path = output_path / f"{title}.epub"
    else:
        candidate = Path(output_epub)
        epub_path = candidate if candidate.is_absolute() else output_path / candidate

    pandoc_result = markdown_to_epub(
        markdown_path,
        epub_path,
        title,
        author=author,
        include_title_page=include_title_page,
    )

    return {
        "title": title,
        "markdown_file": str(markdown_path.resolve()),
        "epub_file": str(epub_path.resolve()),
        "epub_metadata_title": _read_epub_metadata_title(epub_path),
        "cache_hit": cache_hit,
        "case_id": resolved_case_id,
        "pipeline": "pdf_ocr",
        "html_source_url": html_source_url,
        "html_fallback_error": html_fallback_error,
        "artifacts_dir": str(artifact_dir.resolve()),
        "pandoc": pandoc_result,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert papers to EPUB, auto-using arXiv HTML when available and "
            "falling back to PDF OCR."
        )
    )
    parser.add_argument(
        "input_source",
        help="PDF file path or URL (arXiv URLs auto-use HTML when available)",
    )
    parser.add_argument("output_epub", nargs="?", default=None, help="Output EPUB file name/path")
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory for EPUB output and intermediate conversion artifacts",
    )
    parser.add_argument(
        "--cache-dir",
        default=".ocr-cache",
        help="Directory to store/reuse OCR JSON responses",
    )
    parser.add_argument("--case-id", default=None, help="Stable cache key for repeated conversions")
    parser.add_argument("--force-ocr", action="store_true", help="Ignore OCR cache and call API again")
    parser.add_argument(
        "--author",
        default=None,
        help="Optional EPUB author metadata (adds a subtitle on some readers)",
    )
    parser.add_argument(
        "--title-page",
        action="store_true",
        help="Include generated EPUB title page (disabled by default)",
    )
    parser.add_argument(
        "--force-pdf",
        action="store_true",
        help="Force OCR pipeline from PDF even when an arXiv HTML version is available",
    )
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
            include_title_page=args.title_page,
            force_pdf=args.force_pdf,
        )
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
