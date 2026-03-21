import argparse
import json
from pathlib import Path

import requests


def download_file(url: str, destination: Path, force: bool = False) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and not force:
        return {"status": "skipped", "path": str(destination), "reason": "already_exists"}

    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if chunk:
                    handle.write(chunk)

    return {"status": "downloaded", "path": str(destination)}


def load_manifest(manifest_path: Path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Download benchmark PDFs listed in the manifest")
    parser.add_argument(
        "--manifest",
        default="testset/manifest.json",
        help="Path to benchmark manifest",
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if file exists")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)

    downloaded = 0
    skipped = 0

    for case in manifest.get("cases", []):
        url = case.get("source_url")
        pdf_file = case.get("pdf_file")

        if not url or not pdf_file:
            print(f"[WARN] Missing source_url/pdf_file for case {case.get('id', 'unknown')}")
            continue

        destination = (manifest_path.parent / pdf_file).resolve()
        result = download_file(url, destination, force=args.force)

        case_id = case.get("id", "unknown")
        print(f"[{result['status'].upper()}] {case_id} -> {destination}")

        if result["status"] == "downloaded":
            downloaded += 1
        else:
            skipped += 1

    print(f"Done. downloaded={downloaded} skipped={skipped}")


if __name__ == "__main__":
    main()
