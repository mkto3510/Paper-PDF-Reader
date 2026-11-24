"""Dump what the analysis decides on real PDFs, and compare two dumps.

For local use only (it opens PDFs that are not in the repository). It is the check for a change that must not alter
the decisions on other documents: run it before and after, then compare.

  python tools/dump_analysis.py --manifest manifest.json --batch 1 2 3 --out before.jsonl
  python tools/dump_analysis.py --manifest manifest.json --batch 1 --all-pages --out before.jsonl
  python tools/dump_analysis.py --compare before.jsonl after.jsonl

A dump holds, per document, the caption index, the reference entries, the classified lines of the pages
(kind, text, box, in reading order), and the text that a selection of the whole page sends to translation
(its blank lines show where paragraphs are split). By default only a sample of pages is dumped (the first five, a
middle one, the last three); --all-pages dumps every page. Each part is stored in full, so a difference can be
read, not only counted. A page whose lines are the same but in another order is reported as "order only".
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


def dump(path, max_pages, text_pages=True, all_pages=False):
    from paper_reader.pdf import PdfEngine
    from paper_reader.selection import select

    engine = PdfEngine()
    info = engine.open(str(path))
    count = len(info.get("pages", ()))
    if not count or count > max_pages:
        engine.close()
        return {"skipped": count}
    pages_to_dump = range(count) if all_pages else sample_pages(count)

    def classified(page):
        return [[line.kind, line.text[:60], _round(line.box)] for line in engine.text(page, quads=False)]

    def selected(page):
        lines = engine.text(page)
        if not lines:
            return ""
        return select({page: lines}, (page, 0, 0), (page, len(lines) - 1, max(len(lines[-1].glyphs) - 1, 0)),
                      origin="").source

    try:
        pages = {}
        if text_pages:
            # Before any index scan: the page edges are then read by their own path.
            for page in pages_to_dump:
                pages[str(page)] = classified(page)
        while not engine.figure_step(200):
            pass
        while not engine.reference_step(200):
            pass
        captions = sorted([page, group.kind, group.label, group.text[:80], _round(target.box)]
                          for page, entries in engine.figure_pages.items() for group, target in entries)
        references = [[_round(region[0].box) if hasattr(region[0], "box") else str(region[0]),
                       [repr(entry) for entry in region[2]]] for region in engine.references.regions]
        scanned, selections = {}, {}
        if text_pages:
            engine.text_cache.clear()
            engine.citation_cache.clear()
            for page in pages_to_dump:
                scanned[str(page)] = classified(page)
                selections[str(page)] = selected(page)
        return {"pages": count, "captions": captions, "references": references, "text": pages,
                "text_scanned": scanned, "selection": selections}
    finally:
        engine.close()


def _page_differences(a, b):
    """Return (pages whose lines differ, pages whose lines are the same in another order)."""
    changed, order_only = [], []
    for page in sorted(a.keys() | b.keys(), key=int):
        left, right = a.get(page), b.get(page)
        if left == right:
            continue
        if left is not None and right is not None and sorted(map(json.dumps, left)) == sorted(map(json.dumps, right)):
            order_only.append(page)
        else:
            changed.append(page)
    return changed, order_only


def compare(first, second, show=3):
    def load(name):
        return {record["key"]: record for record in map(json.loads, Path(name).read_text(encoding="utf-8").splitlines())}

    before, after = load(first), load(second)
    different = skipped = compared = 0
    for key in sorted(before.keys() & after.keys()):
        a, b = before[key], after[key]
        if "skipped" in a or "skipped" in b or "error" in a or "error" in b:
            skipped += 1
            if a.get("skipped") != b.get("skipped") or a.get("error") != b.get("error"):
                print(key, "differs in skipped/error")
            continue
        compared += 1
        parts = [part for part in ("captions", "references", "text", "text_scanned", "selection")
                 if a.get(part) != b.get(part)]
        if not parts:
            continue
        different += 1
        print(key, "differs in", ", ".join(parts))
        for part in ("text", "text_scanned", "selection"):
            if part in parts and isinstance(a.get(part), dict) and isinstance(b.get(part), dict):
                if part == "selection":
                    pages = [page for page in sorted(a[part].keys() | b[part].keys(), key=int)
                             if a[part].get(page) != b[part].get(page)]
                    print(f"   {part}: pages that differ {pages[:12]}")
                else:
                    changed, order_only = _page_differences(a[part], b[part])
                    print(f"   {part}: lines changed on pages {changed[:12]}; order only on pages {order_only[:12]}")
        for part in ("captions", "references"):
            if part in parts and isinstance(a.get(part), list) and isinstance(b.get(part), list):
                left = {json.dumps(item, ensure_ascii=False) for item in a[part]}
                right = {json.dumps(item, ensure_ascii=False) for item in b[part]}
                for item in sorted(left - right)[:show]:
                    print("   only before:", item[:160])
                for item in sorted(right - left)[:show]:
                    print("   only after: ", item[:160])
    print(f"{compared} documents compared, {different} differ, {skipped} not comparable (skipped or error)"
          f" ({len(before.keys() ^ after.keys())} present in one dump only)")
    return different


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest")
    parser.add_argument("--batch", nargs="+", default=["1"])
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--out", default="dump.jsonl")
    parser.add_argument("--no-text", action="store_true", help="skip the classified lines and selections")
    parser.add_argument("--all-pages", action="store_true", help="dump every page, not a sample")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.compare:
        sys.exit(1 if compare(*args.compare) else 0)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    batches = {int(value) for value in args.batch}
    done = skipped = 0
    with open(args.out, "w", encoding="utf-8") as sink:
        for entry in manifest:
            if entry["batch"] not in batches or not entry.get("path"):
                continue
            started = time.perf_counter()
            try:
                record = dump(entry["path"], args.max_pages, not args.no_text, args.all_pages)
            except Exception as error:  # a damaged PDF must not stop the run
                record = {"error": repr(error)[:200]}
            record.update(key=entry["key"], batch=entry["batch"], seconds=round(time.perf_counter() - started, 1))
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            skipped += "skipped" in record or "error" in record
            done += 1
            print(entry["key"], record.get("pages"), record["seconds"], flush=True)
    print(f"{done} documents, {skipped} skipped (over {args.max_pages} pages) or with an error", flush=True)


if __name__ == "__main__":
    main()
