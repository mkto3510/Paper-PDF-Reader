"""List the paragraphs of a document: the units that are handed to translation.

A paragraph is a run of lines of body text, or of one caption, that reads as a single piece of prose. It may continue
across a column or a page. Lines classified as running heads, footnotes, equations, table cells and the like are not
part of any paragraph, as in a selection. This module only groups lines that are already classified; where those
lines come from (and so which of them are body) is decided in analysis_rules.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from .captions import caption_heading
from .citations import AUTHOR_START, BARE_ENTRY, END, HEADING, NUMBER_ENTRY, normalize
from .paragraph_breaks import bold_tells, line_gap, starts_paragraph, usual_gap
from .selection import Box, Line, clean_source


@dataclass(frozen=True, slots=True)
class Paragraph:
    kind: str                       # "body", "caption" or "reference" (one entry of a bibliography)
    text: str                       # the source text, as a selection would give it
    first: tuple[int, int]          # (page, line index) of the first line
    last: tuple[int, int]           # (page, line index) of the last line
    boxes: dict[int, tuple[Box, ...]]  # the boxes of its lines, by page


def _text(line: Line, rules) -> str:
    skip = rules is None or rules.get("artifact", True)
    return "".join(glyph.text for glyph in line.glyphs if not (skip and glyph.artifact)).strip()


def _kept(line: Line, rules) -> str | None:
    """Return "body" or "caption" for a line that belongs to some paragraph, otherwise None."""
    if not line.horizontal:
        return None  # vertical text is not enumerated
    if line.kind == "caption":
        return "caption"
    if not line.kind:
        return "body"
    # A kind whose rule is switched off is read as text, as in a selection.
    return "body" if rules is not None and not rules.get(line.kind, True) else None


def _line_gaps(entries) -> dict[int, float]:
    """The usual space between two lines of a paragraph, for each page (see paragraph_breaks.usual_gap)."""
    spaces: dict[int, list[float]] = {}
    for (page_a, _, a, _, kind_a), (page_b, _, b, _, kind_b) in zip(entries, entries[1:]):
        gap = line_gap(a, b)
        if page_a == page_b and kind_a == kind_b == "body" and gap is not None:
            spaces.setdefault(page_a, []).append(gap)
    return {page: usual_gap(values) for page, values in spaces.items() if len(values) >= 5}


def _section_marks(lines, rules) -> dict[int, bool]:
    """Find where a bibliography starts (True) and where a section that ends it starts (False).

    A heading is read whatever the line was classified as, and also when it is printed with a gap between letters
    ("R E F E R E N C E S") or as one line per character (文 / 献)."""
    marks: dict[int, bool] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.horizontal or line.kind == "caption":
            index += 1
            continue
        text = normalize(_text(line, rules)).strip()
        words = text.split()
        candidates = [(text, 1)]
        if len(words) >= 4 and sum(len(word) <= 2 for word in words) >= len(words) * .7:
            candidates.append(("".join(words), 1))
        if 0 < len(text) <= 3:
            # One line per character: join the neighbours on the same row.
            joined, end = text, index + 1
            while end < len(lines) and lines[end].horizontal and 0 < len(lines[end].text.strip()) <= 3 and abs(
                    (lines[end].box[1] + lines[end].box[3]) - (line.box[1] + line.box[3])) < line.size * .8:
                joined += normalize(lines[end].text).strip()
                end += 1
            candidates.append((joined, end - index))
        for candidate, span in candidates:
            if HEADING.fullmatch(candidate):
                marks[index] = True
                index += span - 1
                break
            if END.fullmatch(candidate):
                marks[index] = False
                index += span - 1
                break
        index += 1
    return marks


def _join_fragments(entries: list) -> list:
    """Join text objects that sit side by side on one printed line into a single line.

    Some PDFs (OCR output, old typesetting) store each word or phrase as an object of its own. Neighbours of the same
    kind on the same baseline, closer than a word space or two, are one line; the joined line keeps the box that
    covers them and the text of the parts in order."""
    result: list = []
    for entry in entries:
        page, index, line, text, kind = entry
        if result:
            page_a, index_a, a, text_a, kind_a = result[-1]
            size = max(a.size, line.size)
            same_row = (page_a == page and kind_a == kind and abs((a.box[1] + a.box[3]) - (line.box[1] + line.box[3]))
                        <= size * .5)
            gap = line.box[0] - a.box[2]
            if same_row and index_a < index and -size * .3 <= gap <= size * 3:
                merged = replace(a, box=(min(a.box[0], line.box[0]), min(a.box[1], line.box[1]),
                                         max(a.box[2], line.box[2]), max(a.box[3], line.box[3])),
                                 size=max(a.size, line.size))
                result[-1] = (page_a, index_a, merged, f"{text_a} {text}", kind_a)
                continue
        result.append(entry)
    return result


# Other ways a bibliography entry begins: "Dieterich JH, Kilgore B. 1996", "江口元起・庄司力偉 (1955):",
# "NEDO (1996):", "勝井義雄ほか, 1986," (names, then a year).
SURNAME_INITIALS = re.compile(r"^[^\W\d_]+(?:[-'][^\W\d_]+)*\s+[A-Z]{1,3}[,.]")
NAMES_YEAR = re.compile(r"^[^\d()（）]{1,60}?\s*[,，(（]\s*(?:18|19|20)\d{2}[a-z]?")


def _entry_start(text: str) -> bool:
    return bool(NUMBER_ENTRY.match(text) or AUTHOR_START.match(text) or BARE_ENTRY.match(text)
                or SURNAME_INITIALS.match(text) or NAMES_YEAR.match(text))


def _breaks(previous: tuple, current: tuple, rules, gaps=None, bold=None) -> bool:
    (page_a, index_a, a, text_a, kind_a), (page_b, index_b, b, text_b, kind_b) = previous, current
    if kind_a != kind_b:
        return True
    if kind_b == "reference":
        # An entry starts with a number or an author, or sticks out to the left of the lines that wrap below it.
        below = b.box[1] - a.box[3]
        outdent = (page_a == page_b and -max(a.size, b.size) * .6 <= below <= max(a.size, b.size) * 2.5
                   and 3 < a.box[0] - b.box[0]
                   < max(a.size, b.size) * 3)
        return _entry_start(normalize(text_b)) or outdent
    if kind_b == "caption":
        return bool(caption_heading(text_b))  # a new label starts the next caption
    return starts_paragraph(a, b, text_a, text_b, (gaps or {}).get(page_a, 0), page_a == page_b,
                            (bold or {}).get(page_a, True))


def enumerate_paragraphs(pages: dict[int, tuple[Line, ...]], rules=None) -> list[Paragraph]:
    """Group the classified lines of `pages` (page index -> lines in reading order) into paragraphs."""
    entries = []
    hyphenated: set[str] = set()
    in_references = False
    for page in sorted(pages):
        marks = _section_marks(pages[page], rules)
        for index, line in enumerate(pages[page]):
            hyphenated.update(word.casefold() for word in re.findall(r"\w+-\w+", line.text))
            kind = _kept(line, rules)
            heading = index in marks
            if heading:
                in_references = marks[index]
            if kind is None:
                continue
            text = _text(line, rules)
            if not text:
                continue
            if kind == "body" and in_references and not (heading and marks[index]):
                kind = "reference"
            entries.append((page, index, line, text, kind))
    result: list[Paragraph] = []
    group: list[tuple] = []

    def close():
        if not group:
            return
        boxes: dict[int, list[Box]] = {}
        for page, _, line, _, _ in group:
            boxes.setdefault(page, []).append(line.box)
        raw = "\n".join(text for _, _, _, text, _ in group)
        text = clean_source(raw, hyphenated)
        if len(re.findall(r"\w", text)) >= 3:  # a lone symbol or a label letter from a figure is not a paragraph
            result.append(Paragraph(group[0][4], text, group[0][:2], group[-1][:2],
                                    {page: tuple(items) for page, items in boxes.items()}))
        group.clear()

    entries = _join_fragments(entries)
    gaps = _line_gaps(entries)
    bold = {page: bold_tells([line for p, _, line, _, kind in entries if p == page and kind == "body"])
            for page in {entry[0] for entry in entries}}
    for entry in entries:
        if group and _breaks(group[-1], entry, rules, gaps, bold):
            close()
        group.append(entry)
    close()
    return result
