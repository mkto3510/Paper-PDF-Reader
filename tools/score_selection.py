"""Score what a selection sends to translation against paragraphs labeled by hand from page images.

For local use only (it opens PDFs that are not in the repository).

  python tools/score_selection.py --manifest manifest.json --labels labels/paragraphs/out [--verbose]

For each labeled paragraph the page lines that start and end it are found by their text, and that range is selected
as a user would (from its first character to its last). Then:

  split inside   blank lines (paragraph breaks) that the selection puts inside one labeled paragraph; target 0
  separated      labeled paragraphs that follow each other on a page and get a blank line between them when both are
                 selected together; a paragraph that is run into the next one is not separated

Paragraphs of kind "body" and "heading" and "reference" are scored; captions are selected as captions. A paragraph
whose start or end cannot be found on the page is counted as "not located" and is not scored.
"""

from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
import json
import re
from pathlib import Path
import sys
from unicodedata import normalize

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def key(text):
    return "".join(char for char in normalize("NFKC", text).casefold() if char.isalnum())


def similarity(a, b, length=20, tail=False):
    a, b = key(a), key(b)
    if not a or not b:
        return 0
    n = min(len(a), len(b), length)
    left, right = (a[-n:], b[-n:]) if tail else (a[:n], b[:n])
    return SequenceMatcher(None, left, right).ratio()


def close(a, b, length=20, tail=False):
    return similarity(a, b, length, tail) >= .8


def locate(lines, item, after=0):
    """Return (first, last) indexes of the lines that hold the labeled paragraph, or None."""
    # The line where the paragraph starts is the one whose text (with the next two) is most like the label. Taking the
    # first line that is close enough would start a few lines early, at an equation number in front of the paragraph.
    start, best = None, .8
    for index in range(after, len(lines)):
        joined = " ".join(line.text for line in lines[index:index + 3])
        value = similarity(joined, item["start"]) if lines[index].text.strip() else 0
        if value > best + 1e-9:
            start, best = index, value
            if value >= .999:
                break
    if start is None:
        return None
    text = ""
    for index in range(start, min(len(lines), start + 80)):
        text += " " + lines[index].text
        if close(text, item["end"], tail=True) and len(key(text)) >= min(len(key(item["end"])), 8):
            return start, index
    return None


EQUATION_NUMBER = re.compile(r"^[(\[]\s*\d+[a-z]?\s*[)\]]$")


def equation_sites(lines, first, last):
    """How many displayed equations the paragraph runs across: groups of lines made of equation pieces or numbers.
    A sentence that goes on after a displayed equation may be sent as two paragraphs, so a blank line before and
    after each one is not counted against the selection."""
    sites, inside = 0, False
    for line in lines[first:last + 1]:
        piece = line.kind == "equation" or bool(EQUATION_NUMBER.match(line.text.strip()))
        sites += piece and not inside
        inside = piece
    return sites


def select_range(select, page, lines, first, last, origin):
    last_glyph = max(len(lines[last].glyphs) - 1, 0)
    return select({page: lines}, (page, first, 0), (page, last, last_glyph), origin=origin).source


def score(engine, labels, select, scanned=False):
    result = Counter()
    notes = []
    for page_text, items in labels["pages"].items():
        page = int(page_text)
        lines = engine.text(page)
        previous = None
        after = 0
        for item in items:
            kind = item["kind"]
            if kind not in ("body", "heading", "reference", "caption") or item.get("continues_from_previous") \
                    or item.get("continues_to_next"):
                previous = None
                continue
            span = locate(lines, item, after)
            if span is None:
                result["not_located"] += 1
                notes.append((page, "not located", kind, item["start"][:40]))
                previous = None
                continue
            first, last = span
            after = first
            origin = "caption" if kind == "caption" else ""
            source = select_range(select, page, lines, first, last, origin)
            raw = source.count("\n\n")
            splits = max(0, raw - 2 * equation_sites(lines, first, last))
            result["tolerated_at_equations"] += raw - splits
            group = "scanned" if scanned else ("reference" if kind == "reference" else "text")
            result[f"{group}_paragraphs"] += 1
            result[f"{group}_split_inside"] += splits
            result[f"{group}_with_split"] += bool(splits)
            result["paragraphs"] += 1
            result["split_inside"] += splits
            result["paragraphs_with_split"] += bool(splits)
            if splits:
                notes.append((page, f"{splits} blank line(s) inside (of {raw})", kind, item["start"][:40]))
            if previous is not None and previous[1] < first:
                joint = select_range(select, page, lines, previous[0], last, "")
                separated = joint.count("\n\n") > source.count("\n\n") + previous[2]
                result["boundaries"] += 1
                result["boundaries_separated"] += separated
                if not scanned:
                    result["digital_boundaries"] += 1
                    result["digital_separated"] += separated
                if not separated:
                    notes.append((page, "run into the previous paragraph", kind, item["start"][:40]))
            previous = (first, last, raw)
    return result, notes


def main():
    from paper_reader.pdf import PdfEngine
    from paper_reader.selection import select

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
            scanned = any(engine.analysis._page_evidence(int(page), engine._analysis_page(int(page)))[4]
                          for page in labels["pages"])
            counts, notes = score(engine, labels, select, scanned)
        finally:
            engine.close()
        total.update(counts)
        print(labels["key"], f"paragraphs {counts['paragraphs']}, with a split inside {counts['paragraphs_with_split']}"
              f" ({counts['split_inside']} blank lines), separated {counts['boundaries_separated']}/{counts['boundaries']},"
              f" not located {counts['not_located']}")
        if args.verbose:
            for note in notes:
                print("   ", *note)

    def ratio(found, wanted):
        return f"{found}/{wanted} = {found / wanted:.1%}" if wanted else "n/a"

    print()
    print("paragraphs located", total["paragraphs"], "not located", total["not_located"])
    for group, label in (("text", "body, heading, caption (digital PDFs)"), ("reference", "reference entries (digital PDFs)"),
                         ("scanned", "all kinds on pages with a full-page image (scans, OCR)")):
        print(f"  {label}: blank lines inside one paragraph {total[group + '_split_inside']}"
              f" in {ratio(total[group + '_with_split'], total[group + '_paragraphs'])} of the paragraphs")
    print("split inside (blank lines inside one paragraph):", total["split_inside"],
          "in", ratio(total["paragraphs_with_split"], total["paragraphs"]), "of the paragraphs")
    print("separated from the previous paragraph:", ratio(total["boundaries_separated"], total["boundaries"]),
          "; digital PDFs only:", ratio(total["digital_separated"], total["digital_boundaries"]))


if __name__ == "__main__":
    main()
