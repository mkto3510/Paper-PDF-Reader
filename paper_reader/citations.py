"""Find citations on a PDF page and index the document's reference entries."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
import unicodedata

from .selection import Box, bounds


CitationKey = int | tuple[str, str]

DASHES = str.maketrans("‐‑‒–—−’", "------'")
NUMBER = r"[0-9]{1,5}"
YEAR = r"(?:18|19|20|21)[0-9]{2}[a-z]?"
SURNAME = r"(?:(?:de|del|van|von|der|la|le)\s+)*[^\W\d_]+(?:[-'][^\W\d_]+)*"
AUTHOR = rf"(?P<author>{SURNAME})(?:\s+(?:et\s+al\.?|(?:and|&)\s+{SURNAME}))?"
NUMBER_GROUP = re.compile(rf"\[\s*{NUMBER}(?:\s*[-,]\s*{NUMBER})*\s*\]")
PARENS = re.compile(r"\(([^()\n]+)\)")
AUTHOR_YEAR = re.compile(rf"{AUTHOR}\s*,?\s+(?P<year>{YEAR})(?:,\s*(?:pp?\.\s*)?\d+(?:-\d+)?)?", re.I)
NARRATIVE = re.compile(rf"(?<![\w'-]){AUTHOR}\s+\(\s*(?P<year>{YEAR})\s*\)", re.I)
NUMBER_ENTRY = re.compile(rf"^(?:\[({NUMBER})\]|({NUMBER})[.)])\s*(?=[^\W\d_])")
AUTHOR_ENTRY = re.compile(rf"^({SURNAME})(?:\s*,\s*|\s+)(?=[^\W\d_])", re.I)
ENTRY_YEAR = re.compile(rf"(?<![\w/])({YEAR})(?![\w/])", re.I)
AUTHOR_PREFIX = re.compile(rf"^(?:{SURNAME}|[^\W\d_]\.?)(?:[\s.,&]+(?:{SURNAME}|[^\W\d_]\.?))*[\s.,&]*$", re.I)
HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:references|bibliography|works cited|literature cited|参考文献|引用文献)\s*[:.]?$",
    re.I,
)
END = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:appendix(?:\s+[a-z\d]+)?|acknowledg[e]?ments?|"
    r"supplementary (?:material|information)|付録|謝辞)(?:\s*[:.].*)?$", re.I,
)


def normalize(text):
    return unicodedata.normalize("NFKC", text).translate(DASHES)


def author_key(author, year):
    return " ".join(author.casefold().split()), year.casefold()


@dataclass(frozen=True, slots=True)
class Citation:
    label: str
    keys: tuple[CitationKey, ...]


@dataclass(frozen=True, slots=True)
class Reference:
    page: int
    box: Box
    label: str


def number_keys(text):
    found = []
    for part in text[1:-1].split(","):
        edges = [int(value) for value in part.split("-")]
        if len(edges) > 2 or not 0 < edges[0] <= edges[-1] < edges[0] + 50:
            return ()
        found.extend(range(edges[0], edges[-1] + 1))
        if len(found) > 50:
            return ()
    return tuple(dict.fromkeys(found))


def citation_spans(text):
    for match in NUMBER_GROUP.finditer(text):
        if keys := number_keys(match[0]):
            yield match.span(), Citation(match[0], keys)
    for match in PARENS.finditer(text):
        authors = [AUTHOR_YEAR.fullmatch(part.strip()) for part in match[1].split(";")]
        if all(authors):
            keys = tuple(dict.fromkeys(author_key(item["author"], item["year"]) for item in authors))
            yield match.span(), Citation(match[0], keys)
    for match in NARRATIVE.finditer(text):
        yield match.span(), Citation(match[0], (author_key(match["author"], match["year"]),))


def citation_at(lines, x, y):
    for line in lines:
        left, top, right, bottom = line.box
        if not line.horizontal or line.kind in ("furniture", "artifact") or not (left <= x <= right and top <= y <= bottom):
            continue
        parts = []
        for glyph in line.glyphs:
            if parts and glyph.text and unicodedata.combining(glyph.text[0]):
                previous, box = parts.pop()
                parts.append((previous + glyph.text, bounds((box, glyph.box))))
            else:
                parts.append((glyph.text, glyph.box))
        characters, boxes = [], []
        for part, box in parts:
            text = normalize(part)
            characters.append(text)
            boxes.extend([box] * len(text))
        for (start, stop), citation in citation_spans("".join(characters)):
            left, top, right, bottom = bounds(boxes[start:stop])
            if left <= x <= right and top <= y <= bottom:
                return citation
    return None


def reference_keys(text, continuation):
    keys = []
    number = NUMBER_ENTRY.match(text)
    if number:
        keys.append(int(number[1] or number[2]))
        text = text[number.end():]
    author = AUTHOR_ENTRY.match(text)
    if author:
        year = ENTRY_YEAR.search(text + continuation, author.end(), 155)
        # A title or DOI before the first year gives too little evidence that
        # it is the publication year. Accept author lists and APA parentheses.
        if year:
            prefix = (text + continuation)[author.end():year.start()].strip().removesuffix("(").strip()
            if not prefix or (len(prefix) <= 85 and AUTHOR_PREFIX.fullmatch(prefix)):
                keys.append(author_key(author[1], year[1]))
    return keys


class ReferenceIndex:
    """Consume pages from the end, keeping only entries after a reference heading."""

    def __init__(self, page_count):
        self.next_page = page_count - 1
        self.start_page = None
        self.end_page = page_count
        self.entries = defaultdict(list)

    @property
    def complete(self):
        return self.next_page < 0

    def add_page(self, index, rows):
        texts = [normalize(line.text).strip() for line, _ in rows]
        entries = []
        for order in reversed(range(len(rows))):
            text, (_, box) = texts[order], rows[order]
            if HEADING.fullmatch(text):
                self.start_page = index
                break
            elif END.fullmatch(text):
                self.end_page = index
                self.entries.clear()
                entries.clear()
            else:
                continuation = ""
                if order + 1 < len(rows):
                    line, following = rows[order][0], rows[order + 1][0]
                    if line.paragraph == following.paragraph and not NUMBER_ENTRY.match(texts[order + 1]):
                        continuation = " " + texts[order + 1]
                if keys := reference_keys(text, continuation):
                    entries.append((keys, Reference(index, box, text[:110])))
        for keys, entry in entries:
            for key in keys:
                self.entries[key].append(entry)
        self.next_page = -1 if self.start_page is not None else index - 1
        if self.complete and self.start_page is None:
            self.entries.clear()

    def targets(self, position, keys):
        page, x, y = position
        if self.start_page is None or self.start_page < page < self.end_page:
            return ()
        targets = []
        for key in keys:
            entries = self.entries.get(key, ())
            if len(entries) != 1:
                continue
            entry = entries[0]
            left, top, right, bottom = entry.box
            if page == entry.page and left <= x <= right and top <= y <= bottom:
                continue
            if entry not in targets:
                targets.append(entry)
        return tuple(targets)
