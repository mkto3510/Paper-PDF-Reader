"""Count how many in-text citations resolve to a reference entry on real PDFs.

For local use only (it opens PDFs that are not in the repository).

  python tools/measure_citations.py --manifest manifest.json --batch 1 2 3 --out citations.jsonl

For every citation the viewer would recognize (numbers, author-year, superscripts) it asks the reference index for
the target, and counts: resolved (one entry for every key), partial, missing (a reference region exists but
no entry matched) and no_region (the document has no reference region at all). Unresolved samples are written
out so the common patterns can be read and fixed. Documents without a reference region are reported separately,
because nothing can resolve there.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def kind_of(citation, keys):
    if citation.label.startswith("["):
        return "bracket"
    if all(isinstance(key, int) for key in keys):
        return "superscript"
    return "author-year"


def _reason(engine, keys):
    """Say why an author-year citation found no entry: no such author, other year, other co-author, or ambiguous."""
    from paper_reader.citations import _author_matches, _author_targets

    entries = [entry for region in engine.references.regions for entry in region[2]]
    for key in keys:
        if not isinstance(key, tuple) or len(_author_targets(key, entries)) == 1:
            continue
        found = _author_targets(key, entries)
        if len(found) > 1:
            return "ambiguous"
        same_year = [entry for entry in entries if entry[1] and entry[2] == key[1]]
        first = [entry for entry in entries if entry[1] and _author_matches(key[0], entry[1][0])]
        if any(entry[2] == key[1] for entry in first):
            return "coauthor"
        if first:
            return "year"
        if any(key[0] in " ".join(entry[1]) for entry in entries if entry[1]):
            return "not_first_author"
        if any(key[0] in entry[3].label.casefold() for entry in entries):
            return "in_label_only"
        return "no_author" if same_year or entries else "no_entries"
    return "unknown"


def measure(path, max_pages, samples):
    from paper_reader.citations import citation_spans, superscript_spans
    from paper_reader.pdf import PdfEngine

    engine = PdfEngine()
    info = engine.open(str(path))
    pages = len(info.get("pages", ()))
    if not pages or pages > max_pages:
        engine.close()
        return {"skipped": pages}
    try:
        while not engine.reference_step(200):
            pass
        regions = len(engine.references.regions)
        inside = {page for region in engine.references.regions for page in range(region[0][0], region[1][0] + 1)}
        counts = Counter()
        unresolved = []
        for page in range(pages):
            previous = None
            for line in engine.text(page, quads=False):
                starts, previous = previous is None or previous.paragraph != line.paragraph, line
                if (page in inside or not line.horizontal
                        or line.kind in ("furniture", "artifact", "equation", "figure", "code", "table")):
                    continue
                text, boxes, lifted = [], [], []
                for glyph in line.glyphs:
                    part = unicodedata.normalize("NFKC", glyph.text)
                    text.append(part)
                    boxes.extend([glyph.box] * len(part))
                    lifted.extend([glyph.raised or glyph.text in "⁰¹²³⁴⁵⁶⁷⁸⁹"] * len(part))
                joined = "".join(text)
                found = list(citation_spans(joined, starts))
                found += list(superscript_spans(joined, lifted))
                for (start, end), citation in found:
                    box = boxes[start] if start < len(boxes) else line.box
                    point = (page, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                    kind = kind_of(citation, citation.keys)
                    targets = engine.reference_targets(point, citation.keys, ordered=True)
                    hit = sum(target is not None for target in targets)
                    if not regions:
                        outcome = "no_region"
                    elif hit == len(citation.keys):
                        outcome = "resolved"
                    elif hit:
                        outcome = "partial"
                    else:
                        outcome = "missing"
                    counts[f"{kind}:{outcome}"] += 1
                    if kind == "author-year" and outcome in ("missing", "partial"):
                        counts[f"why:{_reason(engine, citation.keys)}"] += 1
                    if outcome in ("missing", "partial") and len(unresolved) < samples:
                        unresolved.append({"page": page, "kind": kind, "label": citation.label[:60],
                                           "why": _reason(engine, citation.keys) if kind == "author-year" else "",
                                           "keys": [list(k) if isinstance(k, tuple) else k for k in citation.keys][:4]})
        return {"pages": pages, "regions": regions,
                "entries": sum(len(region[2]) for region in engine.references.regions),
                "counts": dict(counts), "unresolved": unresolved}
    finally:
        engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--batch", nargs="+", default=["1"])
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--out", default="citations.jsonl")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    batches = {int(value) for value in args.batch}
    with open(args.out, "w", encoding="utf-8") as sink:
        for entry in manifest:
            if entry["batch"] not in batches or not entry.get("path"):
                continue
            started = time.perf_counter()
            try:
                record = measure(entry["path"], args.max_pages, args.samples)
            except Exception as error:  # a damaged PDF must not stop the run
                record = {"error": repr(error)[:200]}
            record.update(key=entry["key"], batch=entry["batch"], seconds=round(time.perf_counter() - started, 1))
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            print(entry["key"], {k: v for k, v in record.items() if k not in ("unresolved", "key")}, flush=True)


if __name__ == "__main__":
    main()
