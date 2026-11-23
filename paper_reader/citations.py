"""Find clickable references and index the bibliography with PDF coordinates."""

from collections import defaultdict
from dataclasses import dataclass
import re
import unicodedata

from .captions import figure_mentions
from .selection import Box, bounds


SPACING_ACCENTS = {"´": "\u0301", "¨": "\u0308", "ˇ": "\u030c", "ˆ": "\u0302", "˜": "\u0303"}
DASHES = str.maketrans("‐‑‒–—−’", "------'")
NUMBER = r"[0-9]{1,5}"
YEAR = r"(?:18|19|20|21)[0-9]{2}[a-z]?"
KANJI = r"[\u3400-\u9fff々]"
SURNAME = r"(?:(?:de|del|van|von|der|la|le)\s+)*[^\W\d_]+(?:[-'][^\W\d_]+)*"
AUTHOR = (rf"(?P<author>{SURNAME})(?:\s+(?:et\s+al\.?|(?:and|&)\s+(?P<second>{SURNAME}))"
          rf"|[・、]\s*(?P<jp_second>{SURNAME})|ら|ほか)?")
NUMBER_GROUP = re.compile(rf"\[\s*{NUMBER}(?:\s*[-,]\s*{NUMBER})*\s*\]")
STRUCTURE = re.compile(r"(?:\b(?:fig(?:ure)?s?|tables?|eq(?:uation)?s?|sec(?:tion)?s?|algorithm|appendix)\.?"
                       r"|[図表式])\s*$", re.I)
PARENS = re.compile(r"\(([^()\n]+)\)")
AUTHOR_YEAR = re.compile(rf"{AUTHOR}(?:\s*,\s*|\s+)(?P<year>{YEAR})(?:,\s*(?:pp?\.\s*)?\d+(?:-\d+)?)?", re.I)
NARRATIVE = re.compile(rf"(?<![A-Za-zÀ-ž0-9_'-]){AUTHOR}\s*(?P<date>\(\s*(?P<year>{YEAR})\s*\))", re.I)
LEAD_IN = re.compile(r"^(?:see(?: also)?|e\.g\.,?|cf\.?)\s+", re.I)
NUMBER_LABEL = rf"(?:\[({NUMBER})\]|({NUMBER})[.)])"
NUMBER_ENTRY = re.compile(rf"^{NUMBER_LABEL}\s*(?=[^\W\d_])")
SPLIT_NUMBER = re.compile(NUMBER_LABEL)
ENTRY_YEAR = re.compile(rf"(?<![\w/])({YEAR})(?![\w/])", re.I)
HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:references|bibliography|works cited|literature cited|(?:参\s*考|引\s*用)?\s*文\s*献)\s*[:.]?$",
    re.I,
)
END = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:appendix(?:\s+(?:[a-z]|[ivx]+|\d+)(?:\s+\S.*)?)?"
    r"|acknowledg[e]?ments?|supplementary (?:material|information)|付録(?:\s*(?:[a-z]|\d+)(?:\s+\S.*)?)?"
    r"|謝辞)(?:\s*[:.].*)?$", re.I,
)
AUTHOR_END = re.compile(r"(?<=[^\W\d_]{2})\.\s+|\s*[「『“\"]")
INITIAL = re.compile(r"(?:[^\W\d_]\.?|[A-Z]{2,3})")
NAME_WORD = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*")
PARTICLES = {"de", "del", "van", "von", "der", "den", "la", "le", "di", "du"}
JAPANESE_PREFIX = re.compile(rf"{KANJI}{{2,4}}")
BIBLIOGRAPHIC = re.compile(r"\b(?:doi|journal|proceedings|press|vol\.?\s+\d|pp\.\s*\d|https?://)|(?:学会誌|論文集|出版社|第\s*\d+\s*巻|\d+\s*頁)", re.I)
REFERENCE_KEY = int | tuple[str, str] | tuple[str, str, str]


def normalize(text):
    text = re.sub(r"([´¨ˇˆ˜])([^\W\d_])", lambda hit: hit[2] + SPACING_ACCENTS[hit[1]], text)
    return unicodedata.normalize("NFKC", text).translate(DASHES)


def clean_name(name):
    return " ".join(name.strip().casefold().split())


@dataclass(frozen=True, slots=True)
class Citation:
    label: str
    keys: tuple[REFERENCE_KEY, ...]
    kind: str = "reference"


@dataclass(frozen=True, slots=True)
class Reference:
    page: int
    box: Box
    label: str
    object_box: Box | None = None


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


def _author_key(match):
    printed_author = match["author"]
    author = re.sub(r"(?:ら|ほか)$", "", printed_author)
    if trailing := re.search(rf"{KANJI}{{2,}}$", author):
        author = trailing[0]
    elif mixed := re.search(r"(?<=[\u3040-\u30ff\u3400-\u9fff々])[A-Za-zÀ-ž][A-Za-zÀ-ž'-]*$", author):
        author = mixed[0]
    key = clean_name(author), match["year"].casefold()
    second = match["second"] or match["jp_second"]
    if second:
        return *key, clean_name(second)
    if printed_author != re.sub(r"(?:ら|ほか)$", "", printed_author) or re.search(r"\bet\s+al\b", match[0], re.I):
        return *key, "*"
    return key


def citation_spans(text):
    for match in NUMBER_GROUP.finditer(text):
        if not STRUCTURE.search(text[:match.start()]) and (keys := number_keys(match[0])):
            yield match.span(), Citation(match[0], keys)
    for match in PARENS.finditer(text):
        authors = [AUTHOR_YEAR.fullmatch(LEAD_IN.sub("", part.strip())) for part in match[1].split(";")]
        if all(authors):
            keys = tuple(dict.fromkeys(_author_key(item) for item in authors))
            yield match.span(), Citation(match[0], keys)
    for match in NARRATIVE.finditer(text):
        if not STRUCTURE.search(text[match.start():match.start("date")]):
            yield match.span(), Citation(match[0], (_author_key(match),))


def _clickable(line):
    return line.horizontal and line.kind not in ("furniture", "artifact", "equation", "figure", "code")


def _wrapped(a, b):
    if a.paragraph != b.paragraph or a.kind != b.kind or not (_clickable(a) and _clickable(b)):
        return False
    # Boxes may already be rotated with the PDF page. Test both progression axes.
    size = max(a.size, b.size)
    for axis in (0, 1):
        across = 1 - axis
        separation = max(b.box[axis] - a.box[axis + 2], a.box[axis] - b.box[axis + 2])
        if (min(a.box[across + 2], b.box[across + 2]) > max(a.box[across], b.box[across])
                and size * .4 < abs(a.box[axis] - b.box[axis]) < size * 2.4
                and -size <= separation <= size):
            return True
    return False


def citation_at(lines, x, y):
    for index, line in enumerate(lines):
        left, top, right, bottom = line.box
        if not _clickable(line) or not (left <= x <= right and top <= y <= bottom):
            continue
        first, last = index, index + 1
        while first > max(0, index - 3) and _wrapped(lines[first - 1], lines[first]):
            first -= 1
        while last < min(len(lines), index + 4) and _wrapped(lines[last - 1], lines[last]):
            last += 1
        text, positions = [], []
        for row in lines[first:last]:
            if text:
                if (text[-1] in ("-", "‐", "\u00ad") and len(text) > 1
                        and text[-2][-1:].isalpha() and row.text.lstrip()[:1].isalpha()):
                    text.pop()
                    positions.pop()
                else:
                    text.append(" ")
                    positions.append(None)
            segments = []
            for glyph in row.glyphs:
                if segments and glyph.text and unicodedata.combining(glyph.text[0]):
                    previous, box = segments.pop()
                    segments.append((previous + glyph.text, bounds((box, glyph.box))))
                else:
                    segments.append((glyph.text, glyph.box))
            for glyph_text, box in segments:
                part = unicodedata.normalize("NFKC", glyph_text)
                if part:
                    text.append(part)
                    positions.extend([box if row is line else None] * len(part))

        def hit(start, end):
            visible = [box for box in positions[start:end] if box is not None]
            if not visible:
                return False
            x0, y0, x1, y1 = bounds(visible)
            return x0 <= x <= x1 and y0 <= y <= y1

        normalized = "".join(text)
        figures = [(span, Citation(label, keys, "figure")) for span, label, keys in figure_mentions(normalized)]
        within_object = line.kind in ("table", "caption")
        if not within_object:
            for (start, end), citation in figures:
                if hit(start, end):
                    return citation
        for (start, end), citation in citation_spans(normalized.translate(DASHES)):
            if hit(start, end) and not any(a < end and start < b for (a, b), _ in figures):
                # A reference with surrounding text is meaningful in a
                # caption or table cell. A naked number in a cell is not.
                if not within_object or re.search(r"[^\W\d_]", normalized[:start] + normalized[end:]):
                    return citation
    return None


def _surname(group):
    words = NAME_WORD.findall(group)
    if not words:
        return ""
    name_end = 1
    while name_end < len(words) and words[name_end - 1].casefold() in PARTICLES:
        name_end += 1
    if all(INITIAL.fullmatch(word) for word in words[name_end:]):
        return clean_name(" ".join(words[:name_end]))
    first = len(words) - 1
    while first > 0 and words[first - 1].casefold() in PARTICLES:
        first -= 1
    return clean_name(" ".join(words[first:]))


def _entry_authors(text):
    prefix = text.strip(" .:(")
    prefix = AUTHOR_END.split(prefix, maxsplit=1)[0]
    fields = [part.strip() for part in re.split(r"\s*(?:[,;&、，・]|\band\b)\s*", prefix) if part.strip()]
    if not fields or len(fields[0]) > 70 or len(NAME_WORD.findall(fields[0])) > 7:
        return ()
    first = _surname(fields[0])
    if (len(fields) >= 2 and first == clean_name(fields[0])
            and prefix[len(fields[0]):].lstrip().startswith(",")
            and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", fields[0])):
        # The next field is the given name or initials in inverted names.
        fields = fields[::2]
    authors = []
    for field in fields:
        name = _surname(field)
        if not name:
            continue
        if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", field):
            spellings = NAME_WORD.findall(field)
            if not all(word[0].isupper() or word.casefold() in PARTICLES for word in spellings):
                if not authors:
                    return ()
                # Keep the author order when a later OCR field is unreadable.
                authors.append("")
                continue
        authors.append(name)
    return tuple(authors)


def _entry_identity(text):
    number_match = NUMBER_ENTRY.match(text)
    number = int(number_match[1] or number_match[2]) if number_match else None
    content = text[number_match.end():] if number_match else text
    year = ENTRY_YEAR.search(content, 0, 210)
    authors = _entry_authors(content[:year.start()]) if year else ()
    return number, authors, year[1].casefold() if year else "", bool(authors and year or BIBLIOGRAPHIC.search(content))


def _author_matches(author, value):
    return author == value or (bool(JAPANESE_PREFIX.fullmatch(author)) and value.startswith(author)
                               and bool(re.fullmatch(r"[\u3040-\u30ff\u3400-\u9fff々]{1,5}", value[len(author):])))


def _without_accents(name):
    return "".join(char for char in unicodedata.normalize("NFD", name) if not unicodedata.combining(char))


def _author_targets(key, entries):
    candidates = [(authors, entry) for _, authors, year, entry, _ in entries if authors and year == key[1]]
    first = [(authors, entry) for authors, entry in candidates if _author_matches(key[0], authors[0])]
    if not first:
        first = [(authors, entry) for authors, entry in candidates
                 if _without_accents(key[0]) == _without_accents(authors[0])]
    if len(key) == 3 and key[2] != "*":
        matches = [entry for authors, entry in first if len(authors) > 1 and _author_matches(key[2], authors[1])]
        return matches or [entry for authors, entry in first if len(authors) > 1
                           and _without_accents(key[2]) == _without_accents(authors[1])]
    if len(key) == 3:
        return [entry for authors, entry in first if any(authors[1:])]
    return [entry for authors, entry in first if not any(authors[1:])]


class ReferenceIndex:
    """Collect page rows from the end and publish only complete bibliography regions."""

    def __init__(self, page_count, outline=()):
        self.next_page = page_count - 1
        self.page_count = page_count
        self.pages = {}
        self.regions = []
        self.chapters = sorted(page - 1 for depth, title, page, _ in outline
                               if depth == 1 and not HEADING.fullmatch(normalize(title.strip())))

    @property
    def complete(self):
        return self.next_page < 0

    def add_page(self, index, rows):
        positions = []
        for order, (line, box) in enumerate(rows):
            if not line.horizontal:
                continue
            text = normalize(line.text).strip()
            if positions and SPLIT_NUMBER.fullmatch(positions[-1][2]):
                p, previous_order, label, previous_box, raw, _ = positions[-1]
                left, top, _, _ = line.box
                if -2 <= left - raw[2] <= line.size * 3 and abs(top - raw[1]) < line.size * .55:
                    positions[-1] = (p, previous_order, label + " " + text, bounds((previous_box, box)),
                                     bounds((raw, line.box)), line.paragraph)
                    continue
            positions.append((index, order, text, box, line.box, line.paragraph))
        self.pages[index] = positions
        if index == 0:
            self._finish()
            self.next_page = -1
        else:
            self.next_page = index - 1

    def _finish(self):
        rows = [row for index in sorted(self.pages) for row in self.pages[index]]
        heads = [index for index, row in enumerate(rows)
                 if HEADING.fullmatch(row[2]) and not self._contents_row(row)]
        for index, head in enumerate(heads):
            stop = heads[index + 1] if index + 1 < len(heads) else len(rows)
            stop = next((end for end in range(head + 1, stop) if END.fullmatch(rows[end][2])), stop)
            section = rows[head + 1:stop]
            if section:
                self._add_region(rows[head], section, self._entries(section))
        if not heads:
            self._unheaded(rows)
        self.pages.clear()

    def _contents_row(self, heading):
        # Books sometimes store each dotted contents row as two separate spans.
        x0, y0, x1, y1 = heading[4]
        return any(other is not heading and re.fullmatch(r"[\d .·…]+", other[2])
                   and other[4][0] - x1 > y1 - y0 and abs(other[4][1] - y0) <= (y1 - y0) * .5
                   for other in self.pages[heading[0]])

    def _unheaded(self, rows):
        start = int(self.page_count * .5)
        candidates = []
        for row in rows:
            if row[0] < start:
                continue
            if END.fullmatch(row[2]):
                candidates.append(None)
            elif match := NUMBER_ENTRY.match(row[2]):
                candidates.append((int(match[1] or match[2]), row))
        runs = []
        for item in candidates:
            if item is None:
                runs.append([])
                continue
            if (not runs or not runs[-1] or item[0] != runs[-1][-1][0] + 1
                    or (item[1][0] == runs[-1][-1][1][0]
                        and abs(item[1][4][0] - runs[-1][-1][1][4][0]) > 18
                        and item[1][4][1] >= runs[-1][-1][1][4][1])):
                runs.append([])
            runs[-1].append(item)
        offsets = {(row[0], row[1]): index for index, row in enumerate(rows)}
        for run in runs:
            if len(run) < 3:
                continue
            first, last = run[0][1], run[-1][1]
            stop = offsets[last[:2]] + 1
            for part in rows[stop:stop + 3]:
                if (NUMBER_ENTRY.match(part[2]) or END.fullmatch(part[2]) or part[0] > last[0] + 1
                        or not (part[0] == last[0] and part[5] == last[5]
                                or 3 <= part[4][0] - last[4][0] <= 35)):
                    break
                stop += 1
            next_chapter = next((page for page in self.chapters if page > last[0]), self.page_count)
            # Refuse numbered runs embedded in later body prose. A closing
            # heading may follow a bibliography even before the document ends.
            next_row = rows[stop] if stop < len(rows) else None
            if last[0] < next_chapter - 2 and not (next_row and END.fullmatch(next_row[2])):
                continue
            section = rows[offsets[first[:2]]:stop]
            entries = self._entries(section)
            if sum(evidence for _, _, _, _, evidence in entries) >= 2:
                self._add_region(first, section, entries)

    def _add_region(self, first, section, entries):
        last = section[-1]
        boundary_rows = [(row[0], row[3]) for row in (first, *section)
                         if row[0] in (first[0], last[0])]
        self.regions.append((first, last, entries, boundary_rows))

    @staticmethod
    def _entries(rows):
        entries = []
        for i, row in enumerate(rows):
            page, _, label, box, raw, paragraph = row
            if not label or END.fullmatch(label):
                continue
            previous = rows[i - 1] if i else None
            hanging = previous and previous[0] == page and 3 < raw[0] - previous[4][0] < 30
            explicit_author_year = re.match(rf"^.{{1,120}}\({YEAR}\)\s*[:.]", label)
            if hanging and not NUMBER_ENTRY.match(label) and not re.match(rf"^{SURNAME},", label) and not explicit_author_year:
                continue
            text = label
            for subsequent in rows[i + 1:i + 4]:
                if NUMBER_ENTRY.match(subsequent[2]) or HEADING.fullmatch(subsequent[2]) or END.fullmatch(subsequent[2]):
                    break
                if subsequent[0] != page:
                    if not (subsequent[0] == page + 1 and subsequent[1] < 2
                            and 3 <= subsequent[4][0] - raw[0] <= 35):
                        break
                elif not (subsequent[5] == paragraph or 3 <= subsequent[4][0] - raw[0] <= 35):
                    break
                text += " " + subsequent[2]
            number, authors, year, evidence = _entry_identity(text)
            if number is not None or (authors and year):
                entries.append((number, authors, year, Reference(page, box, label[:110]), evidence))
        return entries

    def targets(self, position, keys, *, ordered=False):
        page, x, y = position
        eligible = self.regions
        if len(eligible) > 1 and self.chapters:
            chapter = lambda p: max((start for start in self.chapters if start <= p), default=-1)
            eligible = [section for section in eligible if chapter(section[0][0]) == chapter(page)]
        if not eligible:
            return (None,) * len(keys) if ordered else ()
        # Numbering can restart in each section. Author/year is document-wide
        # when no outline identifies a section, and still requires a unique row.
        entries = [entry for _, _, section, _ in eligible for entry in section]
        # Text on a boundary page can still be valid body prose outside the section.
        if any(first[0] < page < last[0] or any(
                p == page and box[0] <= x <= box[2] and box[1] <= y <= box[3]
                for p, box in boundary_rows) for first, last, _, boundary_rows in eligible):
            return (None,) * len(keys) if ordered else ()
        targets = []
        for key in keys:
            if isinstance(key, int):
                matches = [entry for number, _, _, entry, _ in entries if number == key] if len(eligible) == 1 else []
            else:
                matches = _author_targets(key, entries)
            targets.append(matches[0] if len(matches) == 1 else None)
        return tuple(targets) if ordered else tuple(dict.fromkeys(target for target in targets if target))
