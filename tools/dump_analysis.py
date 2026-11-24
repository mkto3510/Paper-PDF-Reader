"""Dump what the analysis decides on real PDFs, and compare two dumps.

For local use only (it opens PDFs that are not in the repository). It is the check for a refactoring that must not
change any decision: run it before and after, then compare.

  python tools/dump_analysis.py --manifest manifest.json --batch 1 2 3 --out before.jsonl
  python tools/dump_analysis.py --compare before.jsonl after.jsonl

A dump holds, per document, the caption index, the reference entries, and the classified lines of a sample of pages
(kind, text, box). Each part is stored in full, so a difference can be read, not only counted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _round(box):
    return [round(value, 1) for value in box]


def sample_pages(count):
    """First pages (title, abstract, figures), a middle page, and the last pages (bibliography)."""
    return sorted({*range(min(count, 5)), count // 2, *range(max(0, count - 3), count)})


def dump(path, max_pages, text_pages=True):
    from paper_reader.pdf import PdfEngine

    engine = PdfEngine()
    info = engine.open(str(path))
    count = len(info.get("pages", ()))
    if not count or count > max_pages:
        engine.close()
        return {"skipped": count}
    def classified(page):
        return [[line.kind, line.text[:60], _round(line.box)] for line in engine.text(page, quads=False)]

    try:
        pages = {}
        if text_pages:
            # Before any index scan: the page edges are then read by their own path.
            for page in sample_pages(count):
                pages[str(page)] = classified(page)
        while not engine.figure_step(200):
            pass
        while not engine.reference_step(200):
            pass
        captions = sorted([page, group.kind, group.label, group.text[:80], _round(target.box)]
                          for page, entries in engine.figure_pages.items() for group, target in entries)
        references = [[_round(region[0].box) if hasattr(region[0], "box") else str(region[0]),
                       [repr(entry) for entry in region[2]]] for region in engine.references.regions]
        scanned = {}
        if text_pages:
            engine.text_cache.clear()
            engine.citation_cache.clear()
            for page in sample_pages(count):
                scanned[str(page)] = classified(page)
        return {"pages": count, "captions": captions, "references": references, "text": pages,
                "text_scanned": scanned}
    finally:
        engine.close()


def compare(first, second):
    def load(name):
        return {record["key"]: record for record in map(json.loads, Path(name).read_text(encoding="utf-8").splitlines())}

    before, after = load(first), load(second)
    different = 0
    for key in sorted(before.keys() & after.keys()):
        a, b = before[key], after[key]
        parts = [part for part in ("captions", "references", "text", "text_scanned", "skipped", "error") if a.get(part) != b.get(part)]
        if parts:
            different += 1
            print(key, "differs in", ", ".join(parts))
            for part in parts:
                if isinstance(a.get(part), list) and isinstance(b.get(part), list):
                    left = {json.dumps(item, ensure_ascii=False) for item in a[part]}
                    right = {json.dumps(item, ensure_ascii=False) for item in b[part]}
                    for item in sorted(left - right)[:3]:
                        print("   only before:", item[:160])
                    for item in sorted(right - left)[:3]:
                        print("   only after: ", item[:160])
    print(f"{len(before.keys() & after.keys())} documents compared, {different} differ"
          f" ({len(before.keys() ^ after.keys())} present in one dump only)")
    return different


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest")
    parser.add_argument("--batch", nargs="+", default=["1"])
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--out", default="dump.jsonl")
    parser.add_argument("--no-text", action="store_true", help="skip the classified lines of sample pages")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.compare:
        sys.exit(1 if compare(*args.compare) else 0)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    batches = {int(value) for value in args.batch}
    with open(args.out, "w", encoding="utf-8") as sink:
        for entry in manifest:
            if entry["batch"] not in batches or not entry.get("path"):
                continue
            started = time.perf_counter()
            try:
                record = dump(entry["path"], args.max_pages, not args.no_text)
            except Exception as error:  # a damaged PDF must not stop the run
                record = {"error": repr(error)[:200]}
            record.update(key=entry["key"], batch=entry["batch"], seconds=round(time.perf_counter() - started, 1))
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            print(entry["key"], record.get("pages"), record["seconds"], flush=True)


if __name__ == "__main__":
    main()
