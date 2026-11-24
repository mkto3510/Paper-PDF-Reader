"""List superscript citations that citation_at finds on real PDFs.

For local use only (it opens PDFs that are not in the repository).

  python tools/scan_superscripts.py --manifest manifest.json --batch 1 2 3 --out superscripts.jsonl

For each raised digit run the tool asks citation_at what a click there would do, and records the text around it.
Use the output to look for false positives (exponents, footnote marks) and for documents that use superscripts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def scan(path, max_pages, per_doc):
    from paper_reader.pdf import PdfEngine

    engine = PdfEngine()
    info = engine.open(str(path))
    pages = len(info.get("pages", ()))
    if not pages or pages > max_pages:
        engine.close()
        return {"skipped": pages}
    hits, raised_runs = [], 0
    try:
        for page in range(pages):
            for line in engine.text(page, quads=False):
                if not line.horizontal:
                    continue
                glyphs = line.glyphs
                index = 0
                while index < len(glyphs):
                    if not (glyphs[index].raised and glyphs[index].text.isdigit()):
                        index += 1
                        continue
                    end = index
                    while end < len(glyphs) and glyphs[end].raised:
                        end += 1
                    raised_runs += 1
                    box = glyphs[index].box
                    point = (page, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                    citation = engine.citation_at(*point)
                    if citation and citation.kind == "reference" and len(hits) < per_doc:
                        text = line.text
                        run = "".join(g.text for g in glyphs[index:end])
                        position = len("".join(g.text for g in glyphs[:index]))
                        hits.append({"page": page, "context": text[max(0, position - 30):position] + "⟦" + run + "⟧"
                                     + text[position + len(run):position + len(run) + 15], "keys": list(citation.keys)})
                    index = end
        return {"pages": pages, "raised_runs": raised_runs, "hits": len(hits), "samples": hits}
    finally:
        engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--batch", nargs="+", default=["1"])
    parser.add_argument("--max-pages", type=int, default=60)
    parser.add_argument("--per-doc", type=int, default=400)
    parser.add_argument("--out", default="superscripts.jsonl")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    batches = {int(value) for value in args.batch}
    with open(args.out, "w", encoding="utf-8") as sink:
        for entry in manifest:
            if entry["batch"] not in batches or not entry.get("path"):
                continue
            started = time.perf_counter()
            try:
                record = scan(entry["path"], args.max_pages, args.per_doc)
            except Exception as error:  # a damaged PDF must not stop the run
                record = {"error": repr(error)[:200]}
            record.update(key=entry["key"], batch=entry["batch"], seconds=round(time.perf_counter() - started, 1))
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            print(entry["key"], {k: v for k, v in record.items() if k not in ("samples", "key")}, flush=True)


if __name__ == "__main__":
    main()
