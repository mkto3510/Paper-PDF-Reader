"""Find clickable references and index the bibliography with PDF coordinates."""

from collections import defaultdict
from dataclasses import dataclass, replace
import re
import unicodedata

from .captions import LABEL, caption_heading, figure_label, figure_mentions
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
# AGU journals write author-year citations in square brackets: "[Hanks and Kanamori, 1979]" and "Hess [1965]".
PARENS = re.compile(r"\(([^()\n]+)\)|\[([^\[\]\n]+)\]")
# One author can have several years in a row: "1990a,b", "1990, 1992", "1990; 1992".
YEARS = rf"(?P<year>{YEAR})(?P<more>(?:\s*[,;]\s*(?:{YEAR}|[a-z](?![\w.])))*)"
YEAR_ONLY = re.compile(YEARS, re.I)
PAGE = r"(?:,\s*(?:pp?\.\s*\d+(?:-\d+)?|(?!(?:18|19|20|21)\d{2}\b)\d+(?:-\d+)?))?"
AUTHOR_YEAR = re.compile(rf"{AUTHOR}(?:\s*,\s*|\s+){YEARS}{PAGE}", re.I)
NARRATIVE = re.compile(rf"(?<![A-Za-zÀ-ž0-9_'-]){AUTHOR}\s*(?P<date>[(\[]\s*{YEARS}\s*[)\]])", re.I)
LEAD_IN = re.compile(r"^(?:see(?: also)?|e\.g\.,?|cf\.?)\s+", re.I)
NUMBER_LABEL = rf"(?:\[({NUMBER})\]|({NUMBER})[.)])"
NUMBER_ENTRY = re.compile(rf"^{NUMBER_LABEL}\s*(?=[^\W\d_])")
SPLIT_NUMBER = re.compile(NUMBER_LABEL)
# Some older journals number their entries with a bare numeral ("2 X. Le Pichon and ..."). That is only read as a
# label inside a bibliography whose entries count upward, so ordinary lines that start with a number stay prose.
BARE_ENTRY = re.compile(r"^(\d{1,3})\s+(?=[A-Z])")
ENTRY_YEAR = re.compile(rf"(?<![\w/])({YEAR})(?![\w/])", re.I)
END_YEAR = re.compile(rf"(?<![\w/-])({YEAR})[.,]?\s*$", re.I)
AUTHOR_START = re.compile(rf"^{SURNAME}\s*,\s*(?:[^\W\d_]\.|[A-Z]{{2,3}}[,.\s])")
HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:references(?:\s+cited)?|bibliography|works cited|literature cited|(?:参\s*考|引\s*用)?\s*文\s*献)\s*[:.]?$",
    re.I,
)
END = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:appendix(?:\s+(?:[a-z]|[ivx]+|\d+)(?:\s+\S.*)?)?"
    r"|acknowledg[e]?ments?|supplementary (?:material|information)|付\s*録(?:\s*(?:[a-z]|\d+)(?:\s+\S.*)?)?"
    r"|謝\s*辞)(?:\s*[:.].*)?$", re.I,
)
# The reference scan starts at the last page. Once a bibliography heading has been read, it also reads this many
# earlier pages (a heading can sit on a page that also holds the end of the text) and then stops.
EARLY_STOP_MARGIN = 1
# A longer document is treated as a book that may hold a bibliography per chapter, so every page is read.
BOOK_PAGES = 60
# Stopping early needs at least this many entries behind the heading.
EARLY_STOP_ENTRIES = 3
# A numbered run this long with mostly bibliographic entries is a reference list wherever it sits.
STRONG_RUN = 8
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
    box: Box | None = None


@dataclass(frozen=True, slots=True)
class Reference:
    page: int
    box: Box
    label: str
    object_box: Box | None = None


def number_keys(text):
    found = []
    for part in text[1:-1].split(","):
        if re.search(r"(?<!\d)0\d", part):
            return ()  # "[001]" is a crystal direction, not reference number 1
        edges = [int(value) for value in part.split("-")]
        if len(edges) > 2 or not 0 < edges[0] <= edges[-1] < edges[0] + 50:
            return ()
        found.extend(range(edges[0], edges[-1] + 1))
        if len(found) > 50:
            return ()
    return tuple(dict.fromkeys(found))


def _years(match):
    """The years a citation names for its author: "1990a,b" is 1990a and 1990b."""
    first = match["year"]
    result = [first]
    for token in re.findall(rf"{YEAR}|[a-z]", match["more"] or "", re.I):
        result.append(token if len(token) > 1 else first[:4] + token)
    return result


def _author_key(match, year=None):
    printed_author = match["author"]
    author = re.sub(r"(?:ら|ほか)$", "", printed_author)
    if trailing := re.search(rf"{KANJI}{{2,}}$", author):
        author = trailing[0]
    elif mixed := re.search(r"(?<=[\u3040-\u30ff\u3400-\u9fff々])[A-Za-zÀ-ž][A-Za-zÀ-ž'-]*$", author):
        author = mixed[0]
    key = clean_name(author), (year or match["year"]).casefold()
    second = match["second"] or match["jp_second"]
    if second:
        return *key, clean_name(second)
    if printed_author != re.sub(r"(?:ら|ほか)$", "", printed_author) or re.search(r"\bet\s+al\b", match[0], re.I):
        return *key, "*"
    return key


NOT_AUTHORS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december", "received", "accepted", "revised", "published", "submitted", "version"}


def _plausible(match):
    """Reject names that cannot be authors: words that start in lower case ("law (1979)") and dates or
    manuscript states ("December 1987", "Received 1975")."""
    for group in ("author", "second"):
        words = (match[group] or "").split()
        if words and (words[-1].casefold() in NOT_AUTHORS or words[-1][0].islower()):
            return False
    return True


def citation_spans(text, paragraph_start=False):
    """Yield (span, Citation) pairs. paragraph_start tells that text begins a paragraph: AGU journals number
    their paragraphs "[1]", "[2]", and those numbers are not citations."""
    for match in NUMBER_GROUP.finditer(text):
        if paragraph_start and not text[:match.start()].strip():
            continue
        if not STRUCTURE.search(text[:match.start()]) and (keys := number_keys(match[0])):
            if len(keys) == 1 and keys[0] >= 1800:
                continue  # "Hess [1965]" is a year, not a reference number
            yield match.span(), Citation(match[0], keys)
    for match in PARENS.finditer(text):
        items = []
        for part in (match[1] or match[2]).split(";"):
            part = LEAD_IN.sub("", part.strip())
            if hit := AUTHOR_YEAR.fullmatch(part):
                items.append((hit, _years(hit)))
            elif items and (more := YEAR_ONLY.fullmatch(part)):
                items.append((items[-1][0], _years(more)))  # "(Smith 1990; 1992)": the same author again
            else:
                break
        else:
            if all(_plausible(item) for item, _ in items):
                keys = tuple(dict.fromkeys(_author_key(item, year) for item, years in items for year in years))
                yield match.span(), Citation(match[0], keys)
    for match in NARRATIVE.finditer(text):
        if not STRUCTURE.search(text[match.start():match.start("date")]) and _plausible(match):
            keys = tuple(dict.fromkeys(_author_key(match, year) for year in _years(match)))
            yield match.span(), Citation(match[0], keys)


SUPERSCRIPT_DIGITS = frozenset("⁰¹²³⁴⁵⁶⁷⁸⁹")
RAISED_NUMBERS = re.compile(r"\d{1,3}(?:\s*[,-]\s*\d{1,3})*")
LATIN = "A-Za-zÀ-ž"
BEFORE_MARK = ".,;:!?”’\"]"
MATH_WORDS = {"sin", "cos", "tan", "cot", "sec", "csc", "exp", "log", "ln", "max", "min", "lim", "det", "mod"}


def _mathy(token):
    """Tell whether a word holds mathematical symbols or Greek letters, as formulas do."""
    return any(unicodedata.category(char) in ("Sm", "Cc") or char in "/^_" or ord(char) >= 0x1D400
               or "GREEK" in unicodedata.name(char, "") for char in token)


def _has_prose(plain):
    """Tell whether text holds running sentences. Author lines with affiliation numbers have none."""
    words = re.findall(rf"(?<![{LATIN}])[a-zà-ž]{{4,}}", plain)
    return len(words) >= 2 or len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", plain)) >= 8


def superscript_spans(text, raised):
    """Find citation numbers printed as superscripts, such as "…as shown¹,²".

    Every character of the number must be raised, and it must follow a word or sentence punctuation in running
    text. Exponents (m², 10³, cos²θ, σ⁻²) and author-affiliation numbers are skipped.
    """
    if not _has_prose("".join(char for char, lifted in zip(text, raised) if not lifted)):
        return
    index, size = 0, len(text)
    while index < size:
        if not raised[index] or not text[index].isdigit():
            index += 1
            continue
        end = index + 1
        while end < size and (raised[end] or (text[end] in ",-" and end + 1 < size and raised[end + 1])):
            end += 1
        run, before = text[index:end], text[:index]
        previous = before[-1:]
        following = text[end:end + 1]
        if previous.isalpha():
            word = re.search(rf"[{LATIN}]*$", before)[0]
            exponent = bool(re.match(rf"[{LATIN}]", previous)) and (len(word) <= 2 or word.lower() in MATH_WORDS)
        else:
            exponent = not previous or previous not in BEFORE_MARK or (previous in "\"”’" and before[-2:-1].isdigit())
        # Isotopes (87Sr), symbols with subscripts-like numbers (σ12, ε54Cr) and formulas are not citations.
        exponent = (exponent or bool(re.match(rf"[{LATIN}\u0370-\u03ff]", following))
                    or _mathy(before.split(" ")[-1]))
        if (not exponent and RAISED_NUMBERS.fullmatch(run)
                and (keys := number_keys(f"[{run.replace(' ', '')}]"))):
            yield (index, end), Citation(run, keys)
        index = end


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
        text, positions, raised = [], [], []
        for row in lines[first:last]:
            if text:
                if (text[-1] in ("-", "‐", "\u00ad") and len(text) > 1
                        and text[-2][-1:].isalpha() and row.text.lstrip()[:1].isalpha()):
                    text.pop()
                    positions.pop()
                    raised.pop()
                else:
                    text.append(" ")
                    positions.append(None)
                    raised.append(False)
            segments = []
            for glyph in row.glyphs:
                lifted = glyph.raised or glyph.text in SUPERSCRIPT_DIGITS
                if segments and glyph.text and unicodedata.combining(glyph.text[0]):
                    previous, box, was_lifted = segments.pop()
                    segments.append((previous + glyph.text, bounds((box, glyph.box)), was_lifted))
                else:
                    segments.append((glyph.text, glyph.box, lifted))
            for glyph_text, box, lifted in segments:
                part = unicodedata.normalize("NFKC", glyph_text)
                if part:
                    text.append(part)
                    positions.extend([box if row is line else None] * len(part))
                    raised.extend([lifted] * len(part))

        def hit(start, end):
            visible = [box for box in positions[start:end] if box is not None]
            if visible:
                box = bounds(visible)
                if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                    return box
            return None

        normalized = "".join(text)
        figures = [(span, Citation(label, keys, "figure")) for span, label, keys in figure_mentions(normalized)]
        within_object = line.kind in ("table", "caption")
        if line.kind != "table":
            own_heading = caption_heading(line.text) if line.kind == "caption" else None
            own_label = figure_label(own_heading) if own_heading and own_heading.re is LABEL else None
            for (start, end), citation in figures:
                if own_label not in citation.keys and (box := hit(start, end)):
                    return replace(citation, box=box)
        paragraph_start = first == 0 or not _wrapped(lines[first - 1], lines[first])
        spans = list(citation_spans(normalized.translate(DASHES), paragraph_start))
        if line.kind != "table":
            spans.extend(superscript_spans(normalized.translate(DASHES), raised))
        for (start, end), citation in spans:
            if (box := hit(start, end)) and not any(a < end and start < b for (a, b), _ in figures):
                # A reference with surrounding text is meaningful in a
                # caption or table cell. A naked number in a cell is not.
                if not within_object or re.search(r"[^\W\d_]", normalized[:start] + normalized[end:]):
                    return replace(citation, box=box)
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


def _entry_label(text, bare=False):
    """Return (number, end) for an entry label at the start of text, or (None, 0)."""
    match = NUMBER_ENTRY.match(text)
    if match:
        return int(match[1] or match[2]), match.end()
    if bare and (match := BARE_ENTRY.match(text)):
        return int(match[1]), match.end()
    return None, 0


def _bare_numbering(rows):
    numbers = [int(match[1]) for row in rows if (match := BARE_ENTRY.match(row[2]))]
    return sum(1 for a, b in zip(numbers, numbers[1:]) if b == a + 1) >= 3


def _entry_identity(text, bare=False):
    number, end = _entry_label(text, bare)
    content = text[end:] if number is not None else text
    # A page number such as "1835-1850" looks like a year but is not one.
    year = next((hit for hit in ENTRY_YEAR.finditer(content, 0, 210)
                 if not re.match(r"[-–]\d", content[hit.end():hit.end() + 2])
                 and not re.search(r"\d[-–]$", content[max(0, hit.start() - 2):hit.start()])), None)
    lead, delimiter, remainder = content.partition(":")
    if delimiter and AUTHOR_START.match(lead):
        # Some styles place the year after the venue or DOI. In those styles
        # the author field ends at the colon, before years in the title.
        plain_remainder = re.sub(r"https?://[^\s,]+", "", remainder)
        terminal = END_YEAR.search(plain_remainder)
        if terminal and (terminal_authors := _entry_authors(lead)):
            return number, terminal_authors, terminal[1].casefold(), True
    authors = _entry_authors(content[:year.start()]) if year else ()
    value = year[1].casefold() if year else ""
    if year and (terminal := END_YEAR.search(content.rstrip())) and terminal.start(1) != year.start(1):
        # "Davis, T. N., Alaska earthquake of July 10, 1958; ..., Bull. Seismol. Soc. Am., 50, 221, 1960."
        # Title words before the first year show that it is part of the title, and the entry's year ends it.
        if len(re.findall(r"(?<![^\W\d_])[a-z]{5,}", content[:year.start()])) >= 1:
            value = terminal[1].casefold()
    return number, authors, value, bool(authors and year or BIBLIOGRAPHIC.search(content))


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
        # An empty field is the entry's own "et al.", so any later field means several authors.
        return [entry for authors, entry in first if len(authors) > 1]
    return [entry for authors, entry in first if not any(authors[1:])]


class ReferenceIndex:
    """Collect page rows from the end and publish only complete bibliography regions."""

    def __init__(self, page_count, outline=()):
        self.next_page = page_count - 1
        self.page_count = page_count
        self.head_page = None
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
        self.next_page = index - 1
        if index == 0:
            self._finish()
        elif self._can_stop(index, positions):
            # Everything after the heading has been read, and no earlier page is expected to add a bibliography.
            self.next_page = -1

    def _can_stop(self, index, positions):
        """Stop before page 0 once a headed bibliography is complete.

        Scanning runs from the last page, so a heading found at page H closes the region that follows it.
        Long documents may hold one bibliography per chapter and need every page. A heading alone does not
        prove that a region has entries, so a failed build keeps scanning.
        """
        if any(HEADING.fullmatch(row[2]) and not self._contents_row(row) for row in positions):
            self.head_page = index
        if self.head_page is None or index > self.head_page - EARLY_STOP_MARGIN:
            return False
        if self.page_count > BOOK_PAGES:
            return False
        return self._finish(final=False)

    def _finish(self, final=True):
        rows = [row for index in sorted(self.pages) for row in self.pages[index]]
        heads = [index for index, row in enumerate(rows)
                 if HEADING.fullmatch(row[2]) and not self._contents_row(row)]
        before = len(self.regions)
        for index, head in enumerate(heads):
            stop = heads[index + 1] if index + 1 < len(heads) else len(rows)
            stop = next((end for end in range(head + 1, stop) if END.fullmatch(rows[end][2])), stop)
            section = rows[head + 1:stop]
            if section:
                self._add_region(rows[head], section, self._entries(section))
        if not heads and final:
            self._unheaded(rows)
        if not final and sum(len(region[2]) for region in self.regions[before:]) < EARLY_STOP_ENTRIES:
            # A heading without entries (a contents line, a sentence) is not a bibliography yet.
            del self.regions[before:]
            return False
        self.pages.clear()
        return True

    def _contents_row(self, heading):
        # Books sometimes store each dotted contents row as two separate spans.
        x0, y0, x1, y1 = heading[4]
        # A contents number sits at the right margin; a number fragment in the other column of a two-column page
        # ("793." ending an entry) happens to share the row with a heading but does not reach the margin.
        margin = max(other[4][2] for other in self.pages[heading[0]])
        return any(other is not heading and re.fullmatch(r"[\d .·…]+", other[2])
                   and other[4][0] - x1 > y1 - y0 and abs(other[4][1] - y0) <= (y1 - y0) * .5
                   and other[4][2] >= margin - (y1 - y0) * 3
                   for other in self.pages[heading[0]])

    def _unheaded(self, rows):
        start = int(self.page_count * .5)
        candidates = []
        for row in rows:
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
            section = rows[offsets[first[:2]]:stop]
            entries = self._entries(section)
            evidence = sum(item[4] for item in entries)
            placed = (first[0] >= start and (last[0] >= next_chapter - 2 or bool(next_row and END.fullmatch(next_row[2]))))
            # Short articles print the list right after the text, before methods or extended data. A long numbered
            # run in which most entries look bibliographic is accepted wherever it sits.
            strong = len(entries) >= STRONG_RUN and evidence >= len(entries) * .7
            if evidence >= 2 and (placed or strong):
                self._add_region(first, section, entries)

    def _add_region(self, first, section, entries):
        last = section[-1]
        boundary_rows = [(row[0], row[3]) for row in (first, *section)
                         if row[0] in (first[0], last[0])]
        self.regions.append((first, last, entries, boundary_rows))

    @staticmethod
    def _entries(rows):
        entries = []
        bare = _bare_numbering(rows)
        for i, row in enumerate(rows):
            page, _, label, box, raw, _ = row
            if not label or END.fullmatch(label):
                continue
            previous = rows[i - 1] if i else None
            hanging = previous and previous[0] == page and 3 < raw[0] - previous[4][0] < 30
            explicit_author_year = re.match(rf"^.{{1,120}}\({YEAR}\)\s*[:.]", label)
            if (hanging and _entry_label(label, bare)[0] is None and not re.match(rf"^{SURNAME},", label)
                    and not explicit_author_year):
                continue
            text = label
            last = row
            for subsequent in rows[i + 1:i + 13]:
                if (_entry_label(subsequent[2], bare)[0] is not None or HEADING.fullmatch(subsequent[2])
                        or END.fullmatch(subsequent[2])):
                    break
                indent = subsequent[4][0] - raw[0]
                if abs(indent) < 3 and AUTHOR_START.match(subsequent[2]):
                    break
                if subsequent[0] != last[0]:
                    if not (subsequent[0] == last[0] + 1 and subsequent[1] < 2 and 3 <= indent <= 35):
                        break
                else:
                    current_box = last[4]
                    gap = subsequent[4][1] - current_box[1]
                    height = current_box[3] - current_box[1]
                    if not (0 < gap <= height * 2.35 and (subsequent[5] == last[5] or 3 <= indent <= 35)):
                        break
                text += " " + subsequent[2]
                last = subsequent
            number, authors, year, evidence = _entry_identity(text, bare)
            if (label[0].isdigit() and _entry_label(label, bare)[0] is None
                    and not (bare and number is not None and number < 1000)):
                # The tail of an entry whose first lines are in another column or page ("439:3225-38", "398."),
                # or a year at the end of a line. In a list of bare numbers a lone number ("14") heads an entry.
                continue
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
