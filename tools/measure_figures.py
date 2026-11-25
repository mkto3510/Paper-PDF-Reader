"""Count how many figure and table mentions resolve to a caption on real PDFs.

For local use only (it opens PDFs that are not in the repository).

  python tools/measure_figures.py --manifest manifest.json --batch 1 2 3 --out figures.jsonl

For every mention the viewer recognizes ("Fig. 3", "Table 2", "図 4"), it asks the full figure index for the target.
A mention that finds no caption is either a missed caption (the thing to fix), a mention of another document's figure,
or a figure whose caption is part of an image. The unresolved labels are listed per document so that the common
patterns can be read.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time
from unicodedata import normalize

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def measure(path, max_pages):
    from paper_reader.captions import figure_mentions
    from paper_reader.pdf import PdfEngine

    engine = PdfEngine()
    info = engine.open(str(path))
    pages = len(info.get("pages", ()))
    if not pages or pages > max_pages:
        engine.close()
        return {"skipped": pages}
    try:
        try:
            while not engine.figure_step(200):
                pass
        except TypeError:  # older versions read one page per call
            while not engine.figure_step():
                pass
        captions = sum(len(entries) for entries in engine.figure_pages.values())
        counts, missing, labels = Counter(), Counter(), Counter()
        for page in range(pages):
            for line in engine.text(page, quads=False):
                if not line.horizontal or line.kind in ("furniture", "artifact", "caption", "table", "figure"):
                    continue
                for _, label, keys in figure_mentions(normalize("NFKC", line.text)):
                    point = (page, (line.box[0] + line.box[2]) / 2, (line.box[1] + line.box[3]) / 2)
                    for key, target in zip(keys, engine.figure_targets(point, keys)):
                        counts["resolved" if target is not None else "missing"] += 1
                        if target is None:
                            missing[str(key)] += 1
                            kind, label = key
                            base = label.rstrip("abcdefghijklmnopqrstuvwxyz")
                            known = {(g.kind, g.label) for entries in engine.figure_pages.values() for g, _ in entries}
                            counts["why:" + ("no_caption_for_figure" if (kind, base) not in known
                                             else "panel_or_other")] += 1
        return {"pages": pages, "captions": captions, "counts": dict(counts),
                "missing_keys": missing.most_common(8)}
    finally:
        engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--batch", nargs="+", default=["1"])
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--out", default="figures.jsonl")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    batches = {int(value) for value in args.batch}
    with open(args.out, "w", encoding="utf-8") as sink:
        for entry in manifest:
            if entry["batch"] not in batches or not entry.get("path"):
                continue
            started = time.perf_counter()
            try:
                record = measure(entry["path"], args.max_pages)
            except Exception as error:  # a damaged PDF must not stop the run
                record = {"error": repr(error)[:200]}
            record.update(key=entry["key"], batch=entry["batch"], seconds=round(time.perf_counter() - started, 1))
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            print(entry["key"], {k: v for k, v in record.items() if k != "key"}, flush=True)


if __name__ == "__main__":
    main()
