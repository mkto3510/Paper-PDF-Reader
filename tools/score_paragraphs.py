"""Score the paragraph list against paragraphs labeled by hand from page images.

For local use only (it opens PDFs that are not in the repository).

  python tools/score_paragraphs.py --manifest manifest.json --labels labels/paragraphs/out

A label file holds, per page, the paragraphs in reading order with their kind, the first and the last words, and
whether the paragraph continues from the previous page or to the next one. The scorer asks the engine for the
paragraphs of the whole document (a bibliography is known only from its heading, pages before) and checks, by
text, where paragraphs start and where they end on the labeled pages.

  start recall     labeled starts that some listed paragraph starts with
  start precision  listed paragraphs that start on the page and match a labeled start
  end recall       labeled ends (paragraphs that finish on the page) that some listed paragraph ends with
  exact            a listed paragraph that matches both the start and the end of a labeled paragraph
Matching ignores case, spaces and punctuation, and tolerates a few misread characters.
"""

from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sys
from unicodedata import normalize

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def key(text):
    return "".join(char for char in normalize("NFKC", text).casefold() if char.isalnum())


def similar(a, b, length=24):
    a, b = key(a), key(b)
    if not a or not b:
        return False
    n = min(len(a), len(b), length)
    return SequenceMatcher(None, a[:n], b[:n]).ratio() >= .8


def similar_end(a, b, length=24):
    a, b = key(a), key(b)
    if not a or not b:
        return False
    n = min(len(a), len(b), length)
    return SequenceMatcher(None, a[-n:], b[-n:]).ratio() >= .8


def score(listed_all, labels):
    result = Counter()
    notes = []
    for page_text, items in labels["pages"].items():
        page = int(page_text)
        starting = [p for p in listed_all if p.first[0] == page]
        ending = [p for p in listed_all if p.last[0] == page]
        matched_listed = set()
        for item in items:
            kind = item["kind"]
            if not item.get("continues_from_previous"):
                result[f"start_labeled:{kind}"] += 1
                hit = next((p for p in starting if similar(p.text, item["start"])), None)
                if hit:
                    result[f"start_found:{kind}"] += 1
                    matched_listed.add(id(hit))
                else:
                    notes.append((page, "missed start", kind, item["start"][:50]))
            if not item.get("continues_to_next"):
                result[f"end_labeled:{kind}"] += 1
                if any(similar_end(p.text, item["end"]) for p in ending):
                    result[f"end_found:{kind}"] += 1
                else:
                    notes.append((page, "missed end", kind, item["end"][-50:]))
            if not item.get("continues_from_previous") and not item.get("continues_to_next"):
                result["exact_labeled"] += 1
                if any(similar(p.text, item["start"]) and similar_end(p.text, item["end"]) for p in starting):
                    result["exact_found"] += 1
        for paragraph in starting:
            result["listed_starts"] += 1
            if id(paragraph) in matched_listed:
                result["listed_matched"] += 1
            else:
                notes.append((page, "extra paragraph", paragraph.kind, paragraph.text[:50]))
    return result, notes


def main():
    from paper_reader.pdf import PdfEngine

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    manifest = {entry["key"]: entry for entry in json.loads(Path(args.manifest).read_text(encoding="utf-8"))}
    total = Counter()
    for path in sorted(Path(args.labels).glob("*.json")):
        labels = json.loads(path.read_text(encoding="utf-8"))
        engine = PdfEngine()
        engine.open(manifest[labels["key"]]["path"])
        try:
            counts, notes = score(engine.paragraphs(), labels)
        finally:
            engine.close()
        total.update(counts)
        starts = counts["start_found:body"] + counts["start_found:heading"]
        wanted = counts["start_labeled:body"] + counts["start_labeled:heading"]
        print(labels["key"], f"body+heading starts {starts}/{wanted}",
              f"exact {counts['exact_found']}/{counts['exact_labeled']}",
              f"extra {counts['listed_starts'] - counts['listed_matched']}")
        if args.verbose:
            for note in notes:
                print("   ", *note)

    def ratio(found, wanted):
        return f"{found}/{wanted} = {found / wanted:.1%}" if wanted else "n/a"

    print()
    kinds = sorted({name.split(":")[1] for name in total if ":" in name})
    for kind in kinds:
        print(f"{kind:10s} start recall {ratio(total[f'start_found:{kind}'], total[f'start_labeled:{kind}'])};"
              f" end recall {ratio(total[f'end_found:{kind}'], total[f'end_labeled:{kind}'])}")
    print("start precision", ratio(total["listed_matched"], total["listed_starts"]))
    print("exact paragraphs", ratio(total["exact_found"], total["exact_labeled"]))


if __name__ == "__main__":
    main()
