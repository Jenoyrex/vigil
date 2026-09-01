"""Downloads the WikiQA dataset into a local cache, for offline use by
`validation/wikiqa.py`.

This is a deliberate, separate step from evaluation -- see
`validation/wikiqa.py`'s module docstring and
`validation/reports/wikiqa_baseline.md`'s "Licensing" section for why: the
WikiQA corpus is distributed under the Microsoft Research Data License
Agreement (research/technology-development use; explicitly prohibits
"renting, leasing, [or] transferring rights to third parties"). Vigil does
not redistribute a copy of the dataset in this repository -- this script
downloads a fresh copy directly from Hugging Face's public dataset-server
API into a git-ignored local cache, so each user/environment obtains its
own copy directly from the source, under the license's own
research-and-development allowance. See the root .gitignore /
services/evaluator/.gitignore for the ignore rule covering the cache
directory this script writes to.

Source: the Hugging Face "datasets-server" REST API
(https://datasets-server.huggingface.co), which mirrors
https://huggingface.co/datasets/microsoft/wiki_qa. Uses only the Python
standard library deliberately -- avoids adding the `datasets` or
`huggingface_hub` packages (both pull in `requests`/`httpx`, which this
project's dependency discipline forbids without an unavoidable reason;
this plain-JSON REST API needs neither).

Usage:
    uv run python scripts/download_wikiqa.py
    uv run python scripts/download_wikiqa.py --force   # re-download even if cached
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATASET_ID = "microsoft/wiki_qa"
DATASET_CONFIG = "default"
SPLITS = ("train", "validation", "test")
PAGE_SIZE = 100  # the datasets-server API's maximum "length" per request.

ROWS_URL_TEMPLATE = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset={dataset}&config={config}&split={split}&offset={offset}&length={length}"
)
DATASET_INFO_URL = "https://huggingface.co/api/datasets/{dataset}"

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "wikiqa"

_MAX_RETRIES = 8
_RETRY_BACKOFF_SECONDS = 3.0
_REQUEST_TIMEOUT_SECONDS = 30.0
_POLITE_DELAY_SECONDS = 1.5  # delay between paginated requests, to avoid rate limiting


def _fetch_json(url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = float(retry_after) if retry_after else _RETRY_BACKOFF_SECONDS * (2**attempt)
            time.sleep(min(delay, 60.0))
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url!r} after {_MAX_RETRIES} attempts") from last_error


def _download_split(split: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        url = ROWS_URL_TEMPLATE.format(
            dataset=DATASET_ID.replace("/", "%2F"),
            config=DATASET_CONFIG,
            split=split,
            offset=offset,
            length=PAGE_SIZE,
        )
        payload = _fetch_json(url)
        total = payload["num_rows_total"]
        page_rows = [entry["row"] for entry in payload["rows"]]
        rows.extend(page_rows)
        offset += len(page_rows)
        if payload.get("partial"):
            print(
                f"warning: datasets-server reports a PARTIAL response for split={split!r} "
                f"at offset={offset} -- the cached copy may be incomplete.",
                file=sys.stderr,
            )
        print(f"  {split}: {offset}/{total} rows", end="\r")
        if not page_rows:
            break  # avoid an infinite loop if the API ever returns an empty page early
        time.sleep(_POLITE_DELAY_SECONDS)
    print(f"  {split}: {len(rows)}/{total} rows")
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


def download(*, force: bool = False) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    split_paths = {split: CACHE_DIR / f"{split}.jsonl" for split in SPLITS}
    if not force and all(p.exists() for p in split_paths.values()):
        print(f"Already cached at {CACHE_DIR} (use --force to re-download). Nothing to do.")
        return

    print(f"Fetching dataset metadata for {DATASET_ID!r} ...")
    info = _fetch_json(DATASET_INFO_URL.format(dataset=DATASET_ID))
    dataset_sha = info.get("sha")

    # Resumable per split: a split already cached from a prior (possibly
    # interrupted) run is skipped rather than re-downloaded, unless
    # `force` is set. Row counts for skipped splits are read back from the
    # cached file so `_metadata.json` still reports accurate totals.
    counts: dict[str, int] = {}
    for split in SPLITS:
        if not force and split_paths[split].exists():
            counts[split] = sum(1 for _ in split_paths[split].open(encoding="utf-8"))
            print(f"Skipping split={split!r} -- already cached ({counts[split]} rows).")
            continue
        print(f"Downloading split={split!r} ...")
        rows = _download_split(split)
        _write_jsonl(split_paths[split], rows)
        counts[split] = len(rows)

    metadata = {
        "dataset_id": DATASET_ID,
        "dataset_config": DATASET_CONFIG,
        "dataset_revision_sha": dataset_sha,
        "source_api": "https://datasets-server.huggingface.co",
        "license": (
            "Microsoft Research Data License Agreement for Microsoft Research WikiQA Corpus "
            "-- research/technology-development use only; see "
            "validation/reports/wikiqa_baseline.md for the full analysis. NOT redistributed by "
            "Vigil -- this file records provenance for a copy downloaded directly by this "
            "script's caller, from the original source."
        ),
        "downloaded_at": datetime.now(UTC).isoformat(),
        "row_counts": counts,
    }
    (CACHE_DIR / "_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Done. Cached {sum(counts.values())} rows across {len(SPLITS)} splits at {CACHE_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="Re-download even if a local cache already exists."
    )
    args = parser.parse_args()
    download(force=args.force)


if __name__ == "__main__":
    main()
