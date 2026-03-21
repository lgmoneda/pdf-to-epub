import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlsplit
import xml.etree.ElementTree as ET

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
IMAGE_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^\)]+\)")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
MATHML_TAG_RE = re.compile(r"<math\b", re.IGNORECASE)
MATHML_TEX_ANNOTATION_RE = re.compile(
    r"<annotation[^>]*encoding=\"application/x-tex\"[^>]*>.*?</annotation>",
    re.IGNORECASE | re.DOTALL,
)
UNRENDERED_MATH_MARKER_RE = re.compile(r"(\$\$|\\\\\(|\\\\\[|\\\\begin\{|\\\\tag\{)")


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}


def normalize_text(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip()


def strip_markdown(value: str) -> str:
    value = MARKDOWN_IMAGE_RE.sub(" ", value)
    value = MARKDOWN_LINK_RE.sub(r"\1", value)
    value = value.replace("#", " ")
    value = value.replace("`", " ")
    value = value.replace("*", " ")
    value = value.replace(">", " ")
    value = value.replace("-", " ")
    return normalize_text(value)


def strip_html(value: str) -> str:
    without_tags = HTML_TAG_RE.sub(" ", value)
    return normalize_text(without_tags)


def read_opf_path(extracted_root: Path) -> Path | None:
    container_path = extracted_root / "META-INF" / "container.xml"
    if not container_path.exists():
        return None

    tree = ET.parse(container_path)
    root = tree.getroot()

    rootfile = root.find(".//{*}rootfile")
    if rootfile is None:
        return None

    full_path = rootfile.attrib.get("full-path")
    if not full_path:
        return None

    return extracted_root / full_path


def parse_opf(opf_path: Path) -> dict[str, Any]:
    tree = ET.parse(opf_path)
    root = tree.getroot()

    title_element = root.find(".//{*}metadata/{*}title")
    title = title_element.text.strip() if title_element is not None and title_element.text else ""

    manifest_items = []
    for item in root.findall(".//{*}manifest/{*}item"):
        manifest_items.append(
            {
                "id": item.attrib.get("id", ""),
                "href": item.attrib.get("href", ""),
                "media_type": item.attrib.get("media-type", ""),
                "properties": item.attrib.get("properties", ""),
            }
        )

    return {
        "title": title,
        "manifest_items": manifest_items,
    }


def extract_epub_text_and_assets(epub_path: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="epub_extract_") as temp_dir:
        extraction_root = Path(temp_dir)

        with zipfile.ZipFile(epub_path, "r") as archive:
            archive.extractall(extraction_root)

        opf_path = read_opf_path(extraction_root)
        if opf_path is None or not opf_path.exists():
            return {
                "metadata_title": "",
                "text": "",
                "html_file_count": 0,
                "image_file_count": 0,
                "broken_image_references": [],
                "toc_present": False,
            }

        opf_data = parse_opf(opf_path)
        opf_base = opf_path.parent

        html_files: list[Path] = []
        image_files: list[Path] = []
        toc_present = False

        for item in opf_data["manifest_items"]:
            href = item.get("href", "")
            media_type = item.get("media_type", "")
            properties = item.get("properties", "")
            resolved = (opf_base / href).resolve()

            if media_type in {"application/xhtml+xml", "text/html"} and resolved.exists():
                html_files.append(resolved)
            if Path(href).suffix.lower() in IMAGE_SUFFIXES and resolved.exists():
                image_files.append(resolved)
            if "nav" in properties or href.endswith("toc.ncx"):
                toc_present = True

        broken_image_refs: list[str] = []
        text_parts: list[str] = []
        mathml_tag_count = 0
        unrendered_math_marker_count = 0

        for html_file in html_files:
            content = html_file.read_text(encoding="utf-8", errors="ignore")
            text_parts.append(strip_html(content))

            mathml_tag_count += len(MATHML_TAG_RE.findall(content))
            without_tex_annotations = MATHML_TEX_ANNOTATION_RE.sub("", content)
            unrendered_math_marker_count += len(
                UNRENDERED_MATH_MARKER_RE.findall(without_tex_annotations)
            )

            for reference in IMAGE_SRC_RE.findall(content):
                if reference.startswith("http") or reference.startswith("data:") or reference.startswith("#"):
                    continue

                reference_without_fragment, _ = urldefrag(reference)
                if not reference_without_fragment:
                    continue

                parsed = urlsplit(reference_without_fragment)
                path_reference = parsed.path
                if not path_reference:
                    continue

                suffix = Path(path_reference).suffix.lower()
                if suffix and suffix not in IMAGE_SUFFIXES:
                    continue

                target = (html_file.parent / path_reference).resolve()
                if not target.exists():
                    broken_image_refs.append(f"{html_file.name}:{reference}")

        return {
            "metadata_title": opf_data.get("title", ""),
            "text": normalize_text(" ".join(text_parts)),
            "html_file_count": len(html_files),
            "image_file_count": len(image_files),
            "mathml_tag_count": mathml_tag_count,
            "unrendered_math_marker_count": unrendered_math_marker_count,
            "broken_image_references": sorted(set(broken_image_refs)),
            "toc_present": toc_present,
        }


def run_epubcheck(epub_path: Path) -> dict[str, Any]:
    executable = shutil.which("epubcheck")
    if not executable:
        return {
            "available": False,
            "success": None,
            "errors": 0,
            "warnings": 0,
            "output": "epubcheck not found on PATH",
        }

    process = subprocess.run(
        [executable, str(epub_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    combined_output = (process.stdout or "") + "\n" + (process.stderr or "")
    errors = len(re.findall(r"\bERROR\b|\bFATAL\b", combined_output, flags=re.IGNORECASE))
    warnings = len(re.findall(r"\bWARNING\b", combined_output, flags=re.IGNORECASE))

    return {
        "available": True,
        "success": process.returncode == 0,
        "errors": errors,
        "warnings": warnings,
        "output": combined_output.strip(),
    }


def run_kindle_convert(epub_path: Path, output_dir: Path) -> dict[str, Any]:
    executable = shutil.which("ebook-convert")
    if not executable:
        return {
            "available": False,
            "success": None,
            "warnings": 0,
            "output": "ebook-convert not found on PATH",
            "azw3_path": None,
        }

    azw3_path = output_dir / "kindle-preview.azw3"
    process = subprocess.run(
        [executable, str(epub_path), str(azw3_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    combined_output = (process.stdout or "") + "\n" + (process.stderr or "")
    warnings = len(re.findall(r"\bWARNING\b", combined_output, flags=re.IGNORECASE))

    return {
        "available": True,
        "success": process.returncode == 0,
        "warnings": warnings,
        "output": combined_output.strip(),
        "azw3_path": str(azw3_path) if azw3_path.exists() else None,
    }


def validate_case(
    epub_path: Path,
    markdown_path: Path,
    expectations: dict[str, Any],
    output_dir: Path,
    pandoc_math_warning_count: int = 0,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "epub_exists": epub_path.exists(),
        "zip_valid": False,
        "metadata_title": "",
        "toc_present": False,
        "html_file_count": 0,
        "image_file_count": 0,
        "mathml_tag_count": 0,
        "unrendered_math_marker_count": 0,
        "broken_image_references": [],
        "required_phrases_missing": [],
        "ordered_phrases_pass": True,
        "text_coverage_ratio": 0.0,
        "math_symbols_present": None,
        "replacement_characters": 0,
        "pandoc_math_warning_count": pandoc_math_warning_count,
        "epubcheck": {},
        "kindle_conversion": {},
        "critical_failures": [],
        "score": 0,
        "pass": False,
        "epub_text_sample": "",
    }

    if not epub_path.exists():
        result["critical_failures"].append("epub_not_created")
        return result

    try:
        with zipfile.ZipFile(epub_path, "r"):
            result["zip_valid"] = True
    except zipfile.BadZipFile:
        result["critical_failures"].append("invalid_epub_zip")
        return result

    extracted = extract_epub_text_and_assets(epub_path)
    result["metadata_title"] = extracted["metadata_title"]
    result["toc_present"] = extracted["toc_present"]
    result["html_file_count"] = extracted["html_file_count"]
    result["image_file_count"] = extracted["image_file_count"]
    result["mathml_tag_count"] = extracted.get("mathml_tag_count", 0)
    result["unrendered_math_marker_count"] = extracted.get("unrendered_math_marker_count", 0)
    result["broken_image_references"] = extracted["broken_image_references"]

    epub_text = extracted["text"]
    markdown_text = ""
    if markdown_path.exists():
        markdown_text = strip_markdown(markdown_path.read_text(encoding="utf-8", errors="ignore"))

    if markdown_text:
        result["text_coverage_ratio"] = round(len(epub_text) / max(1, len(markdown_text)), 4)

    required_phrases = expectations.get("required_phrases", [])
    lower_text = epub_text.lower()
    result["required_phrases_missing"] = [
        phrase for phrase in required_phrases if phrase.lower() not in lower_text
    ]

    ordered_phrases = expectations.get("ordered_phrases", [])
    current_position = -1
    for phrase in ordered_phrases:
        next_position = lower_text.find(phrase.lower(), current_position + 1)
        if next_position == -1:
            result["ordered_phrases_pass"] = False
            break
        current_position = next_position

    result["replacement_characters"] = sum(
        epub_text.count(symbol) for symbol in ["�", "□", "◻", "¤"]
    )

    if expectations.get("has_math"):
        math_markers = ["=", "\\", "∑", "∞", "≤", "≥", "alpha", "beta", "theta", "lambda"]
        result["math_symbols_present"] = any(marker in lower_text for marker in math_markers)

    result["epubcheck"] = run_epubcheck(epub_path)
    result["kindle_conversion"] = run_kindle_convert(epub_path, output_dir)

    min_images = expectations.get("min_image_files", 0)

    if not result["metadata_title"]:
        result["critical_failures"].append("missing_epub_title")
    if not result["toc_present"]:
        result["critical_failures"].append("missing_toc")
    if result["broken_image_references"]:
        result["critical_failures"].append("broken_image_references")
    if result["image_file_count"] < min_images:
        result["critical_failures"].append("too_few_images")
    if result["required_phrases_missing"]:
        result["critical_failures"].append("missing_required_phrases")
    if not result["ordered_phrases_pass"]:
        result["critical_failures"].append("reading_order_signal_failed")
    if result["text_coverage_ratio"] < 0.6:
        result["critical_failures"].append("low_text_coverage")
    if expectations.get("has_math") and result["math_symbols_present"] is False:
        result["critical_failures"].append("math_symbols_missing")
    if expectations.get("has_math") and result["mathml_tag_count"] == 0:
        result["critical_failures"].append("mathml_missing")
    if expectations.get("has_math") and result["unrendered_math_marker_count"] > 2:
        result["critical_failures"].append("unrendered_latex_markers")
    if result["replacement_characters"] > 25:
        result["critical_failures"].append("too_many_replacement_characters")
    if expectations.get("has_math") and pandoc_math_warning_count > 0:
        result["critical_failures"].append("pandoc_math_warnings")

    epubcheck = result["epubcheck"]
    if epubcheck.get("available") and not epubcheck.get("success"):
        result["critical_failures"].append("epubcheck_failed")

    kindle = result["kindle_conversion"]
    if kindle.get("available") and not kindle.get("success"):
        result["critical_failures"].append("kindle_conversion_failed")

    score = 100
    score -= len(result["critical_failures"]) * 12
    score -= min(25, len(result["required_phrases_missing"]) * 5)
    score -= min(20, len(result["broken_image_references"]) * 3)
    score -= min(20, max(0, min_images - result["image_file_count"]) * 4)
    score -= min(20, pandoc_math_warning_count * 2)
    score -= min(20, result["unrendered_math_marker_count"])

    if result["text_coverage_ratio"] < 0.8:
        score -= int((0.8 - result["text_coverage_ratio"]) * 50)

    score = max(0, score)
    result["score"] = score
    result["pass"] = len(result["critical_failures"]) == 0
    result["epub_text_sample"] = epub_text[:4000]
    return result
