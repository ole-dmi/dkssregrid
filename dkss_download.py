#!/usr/bin/env python3
"""Download DKSS forecast GRIB files from the DMI open data API.

The API needs no key.  Each DKSS collection publishes 121 hourly GRIB files per
model run (+0 h ... +120 h), one file per forecast valid time.  Files are stored
under

    <data-dir>/<collection>/<model run>/<original filename>.grib

together with a manifest.json describing the run.  Re-running is cheap: a file
whose size already matches the one on the server is skipped.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://opendataapi.dmi.dk/v1/forecastdata"
USER_AGENT = "dkssregrid-downloader"
TIMEOUT = 60
RETRIES = 3
CHUNK = 1 << 20

DEFAULT_COLLECTIONS = [
    "dkss_nsbs",
    "dkss_idw",
    "dkss_ws",
    "dkss_lf",
    "dkss_lb",
    "dkss_if",
]

RUN_FMT = "%Y-%m-%dT%H:%M:%SZ"


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


def human(nbytes: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024 or unit == "TB":
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _open(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _with_retry(fn, what: str):
    """Call fn(), retrying transient failures with 1 s, 4 s, 9 s backoff."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404):
                raise RuntimeError(f"{what}: HTTP {exc.code} {exc.reason}") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < RETRIES:
            time.sleep(attempt**2)
    raise RuntimeError(f"{what}: {last}")


def api_get(url: str) -> dict:
    def once():
        with _open(url) as resp:
            return json.load(resp)

    return _with_retry(once, f"GET {url}")


def remote_size(url: str) -> int | None:
    """Content-Length of the asset, or None if the server does not say."""
    try:
        with _open(url, method="HEAD") as resp:
            length = resp.headers.get("Content-Length")
        return int(length) if length is not None else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def list_collections() -> list[str]:
    doc = api_get(f"{API}/collections")
    return [c["id"] for c in doc.get("collections", [])]


def normalise_run(text: str) -> str:
    """Accept 2026-08-21T00, ...T00:00, ...T00:00:00Z, 2026-08-21 00 etc."""
    s = text.strip().replace(" ", "T").rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime(RUN_FMT)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"cannot parse model run {text!r}; use e.g. 2026-08-21T00 or 2026-08-21T00:00:00Z"
    )


def run_dirname(run: str) -> str:
    """2026-08-21T00:00:00Z -> 2026-08-21T000000Z, matching the file names."""
    return re.sub(r"[:]", "", run)


def latest_model_run(collection: str) -> str:
    doc = api_get(f"{API}/collections/{collection}/items?limit=1")
    features = doc.get("features") or []
    if not features:
        raise RuntimeError(f"{collection}: API returned no items")
    return features[0]["properties"]["modelRun"]


def list_items(collection: str, run: str | None = None) -> list[dict]:
    """All items of one model run (or the newest page when run is None)."""
    url = f"{API}/collections/{collection}/items?limit=1000"
    if run:
        url += f"&modelRun={run}"
    doc = api_get(url)
    items = []
    for feat in doc.get("features", []):
        props = feat.get("properties", {})
        items.append(
            {
                "id": feat["id"],
                "datetime": props.get("datetime"),
                "modelRun": props.get("modelRun"),
                "created": props.get("created"),
                "href": feat["asset"]["data"]["href"],
            }
        )
    items.sort(key=lambda it: it["datetime"] or "")
    return items


def lead_hours(item: dict) -> float:
    valid = datetime.strptime(item["datetime"], RUN_FMT).replace(tzinfo=timezone.utc)
    run = datetime.strptime(item["modelRun"], RUN_FMT).replace(tzinfo=timezone.utc)
    return (valid - run).total_seconds() / 3600.0


def download_one(item: dict, dest: Path, force: bool) -> tuple[str, int]:
    """Fetch one GRIB file.  Returns (status, bytes) with status skipped|downloaded."""
    target = dest / item["id"]
    size = remote_size(item["href"])
    if not force and target.exists():
        local = target.stat().st_size
        if size is None or local == size:
            return "skipped", local

    part = target.with_suffix(target.suffix + ".part")

    def once() -> int:
        written = 0
        with _open(item["href"]) as resp, open(part, "wb") as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
        if size is not None and written != size:
            part.unlink(missing_ok=True)
            raise OSError(f"short read: got {written} of {size} bytes")
        return written

    try:
        written = _with_retry(once, f"download {item['id']}")
    except Exception:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, target)
    return "downloaded", written


def fetch_run(
    collection: str,
    run: str,
    items: list[dict],
    data_dir: Path,
    jobs: int,
    force: bool,
    quiet: bool,
    all_items: list[dict] | None = None,
) -> tuple[int, int, int, int]:
    """Download `items` of a run.  Returns (downloaded, skipped, failed, bytes).

    The manifest always describes the full run (`all_items`), so a partial fetch
    with --max-lead-hours does not shrink the manifest of an earlier full fetch.
    """
    dest = data_dir / collection / run_dirname(run)
    dest.mkdir(parents=True, exist_ok=True)

    manifest = {
        "collection": collection,
        "modelRun": run,
        "fetched": datetime.now(timezone.utc).strftime(RUN_FMT),
        "items": all_items if all_items is not None else items,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")

    downloaded = skipped = failed = total = 0
    n = len(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(download_one, it, dest, force): it for it in items}
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            item = futures[future]
            try:
                status, nbytes = future.result()
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                failed += 1
                print(f"[{done:4d}/{n}] FAILED {item['id']}: {exc}", file=sys.stderr)
                continue
            total += nbytes
            if status == "downloaded":
                downloaded += 1
            else:
                skipped += 1
            log(f"[{done:4d}/{n}] {status:10s} {item['id']} ({human(nbytes)})", quiet)

    return downloaded, skipped, failed, total


def cmd_list_runs(collections: list[str]) -> int:
    for collection in collections:
        items = list_items(collection)
        runs: dict[str, int] = {}
        for item in items:
            runs[item["modelRun"]] = runs.get(item["modelRun"], 0) + 1
        print(f"{collection}:")
        for run in sorted(runs, reverse=True):
            print(f"    {run}  {runs[run]:4d} files")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Download DKSS forecast GRIB files from the DMI open data API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--collections",
        default=",".join(DEFAULT_COLLECTIONS),
        help="comma-separated collection ids",
    )
    parser.add_argument(
        "--run",
        type=normalise_run,
        help="model run to fetch, e.g. 2026-08-21T00 (default: latest per collection)",
    )
    parser.add_argument("--data-dir", type=Path, default=here / "data")
    parser.add_argument("--jobs", type=int, default=4, help="parallel downloads")
    parser.add_argument(
        "--max-lead-hours",
        type=float,
        help="only fetch forecast hours up to this lead time",
    )
    parser.add_argument(
        "--list-runs", action="store_true", help="list available model runs and exit"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be fetched, then exit"
    )
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    wanted = [c.strip() for c in args.collections.split(",") if c.strip()]
    available = list_collections()
    unknown = [c for c in wanted if c not in available]
    if unknown:
        print(
            f"unknown collection(s): {', '.join(unknown)}\n"
            f"available: {', '.join(available)}",
            file=sys.stderr,
        )
        return 2

    if args.list_runs:
        return cmd_list_runs(wanted)

    if args.jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
        return 2

    started = time.time()
    grand = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

    for collection in wanted:
        run = args.run or latest_model_run(collection)
        all_items = list_items(collection, run)
        items = all_items
        if args.max_lead_hours is not None:
            items = [it for it in all_items if lead_hours(it) <= args.max_lead_hours]
        if not items:
            print(f"{collection}: no files for model run {run}", file=sys.stderr)
            grand["failed"] += 1
            continue

        dest = args.data_dir / collection / run_dirname(run)
        log(f"\n{collection}  run {run}  {len(items)} files -> {dest}", args.quiet)

        if args.dry_run:
            total = 0
            for item in items:
                size = remote_size(item["href"]) or 0
                total += size
                log(f"    {item['id']} ({human(size)})", args.quiet)
            print(f"{collection}: would fetch {len(items)} files, {human(total)}")
            continue

        downloaded, skipped, failed, nbytes = fetch_run(
            collection,
            run,
            items,
            args.data_dir,
            args.jobs,
            args.force,
            args.quiet,
            all_items=all_items,
        )
        grand["downloaded"] += downloaded
        grand["skipped"] += skipped
        grand["failed"] += failed
        grand["bytes"] += nbytes
        print(
            f"{collection}: {downloaded} downloaded, {skipped} skipped, "
            f"{failed} failed, {human(nbytes)}"
        )

    if args.dry_run:
        return 0

    elapsed = time.time() - started
    print(
        f"\ntotal: {grand['downloaded']} downloaded, {grand['skipped']} skipped, "
        f"{grand['failed']} failed, {human(grand['bytes'])} in {elapsed:.1f} s"
    )
    return 1 if grand["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
