import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_previous_summary(runs_dir: Path, current_run_dir: Path) -> dict[str, Any] | None:
    if not runs_dir.exists():
        return None

    candidate_dirs = [path for path in runs_dir.iterdir() if path.is_dir() and path != current_run_dir]
    if not candidate_dirs:
        return None

    candidate_dirs.sort(key=lambda value: value.name)
    previous_dir = candidate_dirs[-1]
    summary_path = previous_dir / "summary.json"

    if not summary_path.exists():
        return None

    return json.loads(summary_path.read_text(encoding="utf-8"))


def _build_regression_section(
    current_cases: list[dict[str, Any]],
    previous_summary: dict[str, Any] | None,
) -> dict[str, list[str]]:
    if not previous_summary:
        return {"regressions": [], "improvements": []}

    previous_cases = {
        case["id"]: case for case in previous_summary.get("cases", [])
    }

    regressions: list[str] = []
    improvements: list[str] = []

    for case in current_cases:
        previous = previous_cases.get(case["id"])
        if not previous:
            continue

        previous_pass = previous.get("pass", False)
        current_pass = case.get("pass", False)

        if previous_pass and not current_pass:
            regressions.append(case["id"])
        if not previous_pass and current_pass:
            improvements.append(case["id"])

    return {
        "regressions": regressions,
        "improvements": improvements,
    }


def _create_markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# PDF to EPUB Benchmark Report",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Created at: `{summary['created_at']}`",
        f"- Total cases: `{summary['total_cases']}`",
        f"- Passed: `{summary['passed_cases']}`",
        f"- Failed: `{summary['failed_cases']}`",
        f"- Average score: `{summary['average_score']}`",
        "",
        "## Cases",
        "",
        "| Case | Pass | Score | Critical Failures |",
        "|---|---:|---:|---|",
    ]

    for case in summary["cases"]:
        failures = ", ".join(case.get("critical_failures", [])) or "-"
        lines.append(
            f"| `{case['id']}` | `{case['pass']}` | `{case['score']}` | {failures} |"
        )

    regression_info = summary.get("regression_info", {})
    regressions = regression_info.get("regressions", [])
    improvements = regression_info.get("improvements", [])

    lines.extend(
        [
            "",
            "## Regressions",
            "",
            "- " + ", ".join(regressions) if regressions else "- none",
            "",
            "## Improvements",
            "",
            "- " + ", ".join(improvements) if improvements else "- none",
            "",
        ]
    )

    return "\n".join(lines)


def write_reports(
    run_dir: Path,
    runs_root: Path,
    case_results: list[dict[str, Any]],
) -> dict[str, Any]:
    passed_cases = sum(1 for case in case_results if case.get("pass"))
    total_cases = len(case_results)
    failed_cases = total_cases - passed_cases

    average_score = 0.0
    if total_cases:
        average_score = round(
            sum(float(case.get("score", 0)) for case in case_results) / total_cases,
            2,
        )

    previous_summary = _load_previous_summary(runs_root, run_dir)
    regression_info = _build_regression_section(case_results, previous_summary)

    summary = {
        "run_id": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_cases": total_cases,
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "average_score": average_score,
        "cases": case_results,
        "regression_info": regression_info,
        "previous_run": previous_summary.get("run_id") if previous_summary else None,
    }

    summary_path = run_dir / "summary.json"
    markdown_path = run_dir / "report.md"

    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    markdown_path.write_text(_create_markdown_report(summary), encoding="utf-8")

    return {
        "summary_path": str(summary_path),
        "markdown_path": str(markdown_path),
        "summary": summary,
    }
