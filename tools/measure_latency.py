"""Time the reference and figure indexes on real PDFs.

For local use only. It reads PDFs that are not part of the repository, so it is
not named check_*.py and CI never runs it.

  python tools/measure_latency.py --one PATH.pdf
  python tools/measure_latency.py --manifest manifest.json --batch 1 --out out.jsonl [--dump DIR]

--one      measure one PDF in this process and print one JSON object.
--manifest run --one in a child process per PDF (a crash or timeout in one PDF
           cannot stop the run) for the entries of the selected batches.
--dump     also write the reference entries and figure captions found in each
           PDF, so two runs (before and after a change) can be compared.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def _ms(start):
    return round((time.perf_counter() - start) * 1000, 1)


def measure(path, dump=False):
    sys.path.insert(0, str(ROOT))
    from paper_reader.pdf import PdfEngine

    engine = PdfEngine()
    result = {"file": Path(path).name, "mb": round(Path(path).stat().st_size / 1048576, 1)}
    start = time.perf_counter()
    info = engine.open(path)
    result["open_ms"] = _ms(start)
    if "pages" not in info:
        result["error"] = "password"
        return result
    pages = len(info["pages"])
    result["pages"] = pages

    steps = []
    start = time.perf_counter()
    while True:
        step = time.perf_counter()
        done = engine.reference_step()
        steps.append((time.perf_counter() - step) * 1000)
        if done:
            break
    result["ref_total_ms"] = _ms(start)
    result["ref_page_median_ms"] = round(statistics.median(steps), 1)
    result["ref_page_max_ms"] = round(max(steps), 1)
    result["ref_steps"] = len(steps)
    regions = engine.references.regions
    result["ref_regions"] = len(regions)
    result["ref_entries"] = sum(len(region[2]) for region in regions)

    steps = []
    start = time.perf_counter()
    while True:
        step = time.perf_counter()
        done = engine.figure_step()
        steps.append((time.perf_counter() - step) * 1000)
        if done:
            break
    result["fig_total_ms"] = _ms(start)
    result["fig_page_median_ms"] = round(statistics.median(steps), 1)
    result["fig_page_max_ms"] = round(max(steps), 1)
    result["fig_steps"] = len(steps)
    captions = [(page, group.kind, group.label) for page, entries in sorted(engine.figure_pages.items())
                for group, _ in entries]
    result["fig_captions"] = len(captions)

    # Hover cost: the first call classifies the page, the second one is cached.
    middle = pages // 2
    start = time.perf_counter()
    engine.citation_at(middle, 0, 0)
    result["hover_cold_ms"] = _ms(start)
    start = time.perf_counter()
    engine.citation_at(middle, 0, 0)
    result["hover_warm_ms"] = _ms(start)

    if dump:
        entries = [(number, authors, year, ref.page, ref.label)
                   for region in regions for number, authors, year, ref, _ in region[2]]
        result["dump"] = {"references": entries, "captions": captions}
        digest = hashlib.sha256(json.dumps(result["dump"], ensure_ascii=False, default=list).encode()).hexdigest()
        result["dump_sha"] = digest[:12]
    return result


def run_manifest(args):
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    batches = {int(value) for value in args.batch}
    entries = [entry for entry in manifest if entry["batch"] in batches and entry.get("path")]
    if args.limit:
        entries = entries[:args.limit]
    out = Path(args.out)
    dump_dir = Path(args.dump) if args.dump else None
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONUTF8="1", QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(ROOT))
    with out.open("w", encoding="utf-8") as sink:
        for entry in entries:
            command = [sys.executable, "-B", __file__, "--one", entry["path"]]
            if dump_dir:
                command.append("--dump-result")
            started = time.perf_counter()
            try:
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                           timeout=args.timeout, env=environment)
                record = json.loads(completed.stdout) if completed.returncode == 0 and completed.stdout.strip() \
                    else {"error": f"rc={completed.returncode}", "stderr": completed.stderr[-300:]}
            except subprocess.TimeoutExpired:
                record = {"error": f"timeout>{args.timeout}s"}
            except json.JSONDecodeError as error:
                record = {"error": f"bad output: {error}"}
            record.update(key=entry["key"], batch=entry["batch"], wall_s=round(time.perf_counter() - started, 1))
            if dump_dir and "dump" in record:
                (dump_dir / f"{entry['key']}.json").write_text(
                    json.dumps(record.pop("dump"), ensure_ascii=False, default=list), encoding="utf-8")
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            print(entry["key"], record.get("pages"), record.get("ref_total_ms"), record.get("fig_total_ms"),
                  record.get("error", ""), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--one")
    parser.add_argument("--dump-result", action="store_true", help="with --one: include the dump in the output")
    parser.add_argument("--manifest")
    parser.add_argument("--batch", nargs="+", default=["1"])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--out", default="latency.jsonl")
    parser.add_argument("--dump")
    args = parser.parse_args()
    if args.one:
        print(json.dumps(measure(args.one, args.dump_result), ensure_ascii=False, default=list))
    elif args.manifest:
        run_manifest(args)
    else:
        parser.error("give --one or --manifest")


if __name__ == "__main__":
    main()
