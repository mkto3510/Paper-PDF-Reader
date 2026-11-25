"""Check that figure_nearby agrees with the full figure scan on real PDFs.

For local use only (it opens PDFs that are not in the repository).

  python tools/compare_figure_nearby.py --manifest manifest.json --batch 1 2 --out compare.jsonl

For every figure or table mention found in the text, it compares the target from
figure_nearby (pages around the mention) with the target from the full scan.
Outcomes per key: same, fallback (nearby gave None where the full scan found a
target), conflict (both gave a target and they differ), extra (nearby found a
target where the full scan did not).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from unicodedata import normalize

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def compare(path, max_pages):
    from paper_reader.captions import figure_mentions
    from paper_reader.pdf import PdfEngine

    engine = PdfEngine()
    info = engine.open(str(path))
    pages = len(info.get("pages", ()))
    if not pages or pages > max_pages:
        engine.close()
        return {"skipped": pages}
    counts = {"same": 0, "fallback": 0, "conflict": 0, "extra": 0, "none": 0}
    examples = []
    try:
        while not engine.figure_step(200):
            pass
        for page in range(pages):
            for line in engine.text(page, quads=False):
                if not line.horizontal:
                    continue
                for _, _, keys in figure_mentions(normalize("NFKC", line.text)):
                    point = (page, (line.box[0] + line.box[2]) / 2, (line.box[1] + line.box[3]) / 2)
                    full = engine.figure_targets(point, keys)
                    near = engine.figure_nearby(point, keys)
                    for key, a, b in zip(keys, full, near):
                        if a is None and b is None:
                            counts["none"] += 1
                        elif a is not None and b is None:
                            counts["fallback"] += 1
                        elif a is None:
                            counts["extra"] += 1
                            examples.append(("extra", page, key, b.page, b.label[:40]))
                        elif (a.page, a.box) == (b.page, b.box):
                            counts["same"] += 1
                        else:
                            counts["conflict"] += 1
                            examples.append(("conflict", page, key, a.page, b.page))
        return {"pages": pages, **counts, "examples": examples[:5]}
    finally:
        engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--batch", nargs="+", default=["1"])
    parser.add_argument("--max-pages", type=int, default=120)
    parser.add_argument("--out", default="compare.jsonl")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    batches = {int(value) for value in args.batch}
    with open(args.out, "w", encoding="utf-8") as sink:
        for entry in manifest:
            if entry["batch"] not in batches or not entry.get("path"):
                continue
            started = time.perf_counter()
            try:
                record = compare(entry["path"], args.max_pages)
            except Exception as error:  # a damaged PDF must not stop the run
                record = {"error": repr(error)[:200]}
            record.update(key=entry["key"], batch=entry["batch"], seconds=round(time.perf_counter() - started, 1))
            sink.write(json.dumps(record, ensure_ascii=False, default=list) + "\n")
            sink.flush()
            print(entry["key"], {k: v for k, v in record.items() if k not in ("examples", "key")}, flush=True)


if __name__ == "__main__":
    main()
