import argparse
import json
import traceback
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = CURRENT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from pdf_to_epub import process_input  # noqa: E402

from download_testset import download_file  # noqa: E402
from llm_judge import judge_case_with_openai  # noqa: E402
from report import write_reports  # noqa: E402
from validators import validate_case  # noqa: E402


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def select_cases(cases: list[dict[str, Any]], selected_ids: set[str] | None) -> list[dict[str, Any]]:
    if not selected_ids:
        return cases
    return [case for case in cases if case.get("id") in selected_ids]


def resolve_pdf_path(manifest_path: Path, case: dict[str, Any]) -> Path:
    pdf_file = case.get("pdf_file")
    if not pdf_file:
        raise ValueError(f"Case {case.get('id', 'unknown')} has no pdf_file")
    return (manifest_path.parent / pdf_file).resolve()


def maybe_download_pdf(manifest_path: Path, case: dict[str, Any], force_download: bool = False) -> Path:
    pdf_path = resolve_pdf_path(manifest_path, case)
    if pdf_path.exists() and not force_download:
        return pdf_path

    source_url = case.get("source_url")
    if not source_url:
        raise ValueError(f"Case {case.get('id', 'unknown')} missing source_url and pdf file not found")

    download_file(source_url, pdf_path, force=force_download)
    return pdf_path


def run_case(
    case: dict[str, Any],
    manifest_path: Path,
    run_dir: Path,
    cache_dir: Path,
    force_ocr: bool,
    force_download: bool,
    force_pdf: bool,
    use_llm_judge: bool,
    openai_model: str,
) -> dict[str, Any]:
    case_id = case["id"]
    case_output_dir = run_dir / case_id
    case_output_dir.mkdir(parents=True, exist_ok=True)

    input_source = case.get("input_source")
    pdf_path: Path | None = None

    if input_source:
        resolved_input_source = str(input_source)
    else:
        pdf_path = maybe_download_pdf(manifest_path, case, force_download=force_download)
        resolved_input_source = str(pdf_path)

    output_epub = case_output_dir / f"{case_id}.epub"

    conversion_result = process_input(
        input_source=resolved_input_source,
        output_epub=output_epub,
        output_dir=case_output_dir,
        cache_dir=cache_dir,
        case_id=case_id,
        force_ocr=force_ocr,
        force_pdf=force_pdf or bool(case.get("force_pdf", False)),
    )

    markdown_path = Path(conversion_result["markdown_file"])
    pandoc_math_warnings = int(
        conversion_result.get("pandoc", {}).get("math_warning_count", 0)
    )

    validation = validate_case(
        epub_path=Path(conversion_result["epub_file"]),
        markdown_path=markdown_path,
        expectations=case.get("expectations", {}),
        output_dir=case_output_dir,
        pandoc_math_warning_count=pandoc_math_warnings,
    )

    llm_judge_result: dict[str, Any] | None = None
    if use_llm_judge:
        llm_judge_result = judge_case_with_openai(
            case_id=case_id,
            markdown_path=markdown_path,
            epub_path=Path(conversion_result["epub_file"]),
            expectations=case.get("expectations", {}),
            model=openai_model,
        )

    final_pass = validation.get("pass", False)
    final_score = validation.get("score", 0)

    if llm_judge_result and llm_judge_result.get("enabled") and not llm_judge_result.get("skipped"):
        llm_pass = llm_judge_result.get("pass", False)
        llm_overall = int(llm_judge_result.get("overall_score", 1))
        final_pass = final_pass and llm_pass
        final_score = max(0, min(100, final_score - max(0, 4 - llm_overall) * 10))

    return {
        "id": case_id,
        "title": case.get("title", case_id),
        "source_input": resolved_input_source,
        "source_pdf": str(pdf_path) if pdf_path else None,
        "conversion": conversion_result,
        "critical_failures": validation.get("critical_failures", []),
        "validation": validation,
        "llm_judge": llm_judge_result,
        "pass": final_pass,
        "score": final_score,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PDF-to-EPUB benchmark suite")
    parser.add_argument(
        "--manifest",
        default="testset/manifest.json",
        help="Path to benchmark manifest",
    )
    parser.add_argument(
        "--cache-dir",
        default="testset/cache",
        help="OCR cache directory",
    )
    parser.add_argument(
        "--runs-dir",
        default="testset/runs",
        help="Benchmark runs output directory",
    )
    parser.add_argument("--force-ocr", action="store_true", help="Force OCR refresh for all cases")
    parser.add_argument("--force-download", action="store_true", help="Force PDF re-download")
    parser.add_argument(
        "--force-pdf",
        action="store_true",
        help="Force PDF OCR pipeline for all selected cases",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run only selected case ID(s), can be repeated",
    )
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="Enable OpenAI-based qualitative judgement",
    )
    parser.add_argument(
        "--openai-model",
        default="gpt-4.1-mini",
        help="OpenAI model used by --llm-judge",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)

    selected_ids = set(args.case) if args.case else None
    cases = select_cases(manifest.get("cases", []), selected_ids)

    if not cases:
        print("No cases selected. Check --case values or manifest content.")
        sys.exit(1)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs_root = Path(args.runs_dir).resolve()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(args.cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []

    print(f"Running {len(cases)} benchmark cases -> {run_dir}")

    for index, case in enumerate(cases, start=1):
        case_id = case.get("id", f"case_{index}")
        print(f"[{index}/{len(cases)}] {case_id}")

        try:
            result = run_case(
                case=case,
                manifest_path=manifest_path,
                run_dir=run_dir,
                cache_dir=cache_dir,
                force_ocr=args.force_ocr,
                force_download=args.force_download,
                force_pdf=args.force_pdf,
                use_llm_judge=args.llm_judge,
                openai_model=args.openai_model,
            )
        except Exception as exc:
            traceback.print_exc()
            fallback_source_pdf = (
                str(resolve_pdf_path(manifest_path, case))
                if case.get("pdf_file")
                else None
            )
            result = {
                "id": case_id,
                "title": case.get("title", case_id),
                "source_input": str(case.get("input_source") or fallback_source_pdf or ""),
                "source_pdf": fallback_source_pdf,
                "conversion": None,
                "critical_failures": ["benchmark_execution_failed"],
                "validation": {"error": str(exc)},
                "llm_judge": None,
                "pass": False,
                "score": 0,
            }

        results.append(result)

    report_data = write_reports(
        run_dir=run_dir,
        runs_root=runs_root,
        case_results=results,
    )

    summary = report_data["summary"]
    print(
        "Done.",
        f"passed={summary['passed_cases']}/{summary['total_cases']}",
        f"avg_score={summary['average_score']}",
    )
    print(f"Summary: {report_data['summary_path']}")
    print(f"Report:  {report_data['markdown_path']}")


if __name__ == "__main__":
    main()
