"""Where a paragraph starts: the rules shared by a selection and by the list of paragraphs.

A selection sends the text of the lines it covers to translation, and the blank lines in it say where one paragraph
ends and the next begins. The list of paragraphs (paragraphs.py) is checked against paragraphs labeled by hand and
uses the same rules, so they are written once, here. The rules look at the lines only: their boxes, sizes, bold
leads and text. The usual gap between two lines of a paragraph is measured on the page itself, because the space
between lines depends on the typesetting (Japanese text leaves about a line height between lines).
"""

from __future__ import annotations

import re


# A sentence or a clause that ends a line: the next line may then start a new paragraph.
TERMINAL = re.compile(r"[.!?。！？:：…][\"”’'»)\]）」』]*$")
# A sentence that really ends: after one of these a new paragraph may start. A colon or a comma does not end it.
SENTENCE_END = re.compile(r"[.!?。！？…][\"”’'»)\]）」』]*$")
# A section number or a letter (1. 2.3 A. IV.) and then a word that starts with a capital: "2.3 Methods", but not
# "2.3 MPa", "2.3 Å resolution", "0.91. When ..." or a year ("2009. Kerinci had ...").
HEADING_NUMBER = re.compile(
    r"^(?:(?!0\.\d)\d{1,2}(?:\.\d{1,2})*\.?|[IVXLC]+\.|[A-Z]\.|[0-9０-９]{1,2}[.．])\s+"
    r"(?:[A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ]|[A-ZÀ-ÖØ-Ý]{2,}\b|[AI]\s|[A-Z]\.|[぀-鿿])")
HEADING_WORD = re.compile(
    r"^(?:第\s*[0-9０-９一二三四五六七八九十]+\s*[章節項]|"
    r"(?:abstract|introduction|references|acknowledg\w*|conclusions?|discussion|methods?|results|summary|"
    r"appendix|概要|要旨|はじめに|まとめ|参考文献|謝辞|結論|考察)\b)", re.I)
# "Acknowledgments: For our research, we ..." : a heading that runs into its paragraph. It starts a paragraph, but the
# next line goes on with it.
RUN_IN = re.compile(r"^[^:.：]{1,30}[:.：]\s+" + r"\S")


INITIAL = re.compile(r"^[A-Z]\.\s")


def is_fragment(previous: Line, line: Line, same_page: bool = True) -> bool:
    """Tell whether `line` is a smaller piece (a subscript or a superscript such as the 2 of H2O) that sits on the
    printed line of `previous`, inside or after it. It belongs to that line and does not start a paragraph, and the
    line after it continues `previous` rather than the fragment."""
    size = max(previous.size, line.size)
    return (same_page and line.size <= previous.size * .9 and len(line.text.strip()) <= 6
            and previous.box[0] - size * .3 <= line.box[0]
            and abs((previous.box[1] + previous.box[3]) - (line.box[1] + line.box[3])) <= size * .9
            and line.box[0] <= previous.box[2] + size)


def bold_tells(lines) -> bool:
    """Tell whether a bold lead says anything on this page. OCR text often marks every line as bold."""
    body = [line for line in lines if line.horizontal and not line.kind]
    return len(body) < 5 or sum(line.leading_bold for line in body) < len(body) * .8


def is_heading(text: str, line: Line, bold: bool = True) -> bool:
    # "Acknowledgments:" is a heading although a colon ends it; a finished sentence is not.
    if len(text) > 80 or SENTENCE_END.search(text):
        return False
    if bold and line.leading_bold:
        return True
    # A word such as "results," at the start of a line that continues a sentence is not a heading, and neither is a
    # line of names that begins with an initial ("R. Cravey, B. Rosich Tell, N. Walker, and ...").
    if text[:1].islower() or (INITIAL.match(text) and text.count(",") >= 2):
        return False
    return bool(HEADING_NUMBER.match(text) or HEADING_WORD.match(text))


# "5 N.H. Sleep, Thermal effects of ..." : the numbered entry of a list of references starts a paragraph, and the
# lines under it go on with it.
ENTRY = re.compile(r"^\d{1,3}\s+[A-Z]\.")


def runs_in(text: str) -> bool:
    """Tell whether a line opens with something that starts a paragraph but runs on into it (a heading with its
    first sentence after a colon, a numbered entry): the next line goes on with that paragraph."""
    return bool((HEADING_WORD.match(text) and RUN_IN.match(text)) or ENTRY.match(text))


def flows(previous: Line, line: Line, previous_text: str, text: str) -> bool:
    """Tell whether `line` continues the paragraph that `previous` ends, when `previous` is the line before it in
    reading order but not directly above it (the next column, the next page, or past a displayed equation)."""
    if abs(line.size - previous.size) > previous.size * .15:
        return False
    if text[:1].islower() and not SENTENCE_END.search(previous_text):
        return True  # also past a displayed equation that follows a colon
    # A line that stops without ending a sentence goes on in the next column or page, in any script.
    return not TERMINAL.search(previous_text)


# "17. M. D. Zoback, ..." : an item of a numbered list printed in the running text.
NUMBERED_ITEM = re.compile(r"^\[?\d{1,3}[.\]]\s+[A-Z぀-鿿]")


def line_gap(a, b):
    """The space between two consecutive lines of one paragraph, or None when `b` is not directly below `a`."""
    below = b.box[1] - a.box[3]
    size = max(a.size, b.size)
    if 0 <= below <= size * 2 and abs(b.box[0] - a.box[0]) < size * 3.2:
        return below
    return None


def usual_gap(spaces):
    """The usual space between two lines of a paragraph on a page: the median, or 0 when there are too few."""
    return sorted(spaces)[len(spaces) // 2] if len(spaces) >= 5 else 0


class _Row:
    """The box and the type size of a printed line that was split into several text objects."""
    __slots__ = ("box", "size")

    def __init__(self, box, size):
        self.box, self.size = box, size


def page_gap(lines):
    """The usual space between two printed lines of a paragraph on a page. Lines that are split into words or
    pieces (OCR, Japanese typesetting) are first put together, or too few gaps could be measured."""
    rows = []
    for line in lines:
        if not line.horizontal or line.kind:
            continue
        if rows and on_same_line(rows[-1], line):
            x0, y0, x1, y1 = rows[-1].box
            rows[-1] = _Row((min(x0, line.box[0]), min(y0, line.box[1]), max(x1, line.box[2]), max(y1, line.box[3])),
                            rows[-1].size)
        else:
            rows.append(_Row(line.box, line.size))
    return usual_gap([gap for a, b in zip(rows, rows[1:]) if (gap := line_gap(a, b)) is not None])


def hanging(a, b, after):
    """Tell whether `b`, indented against `a`, only begins the inner lines of an entry whose first line sticks out to
    the left (a list of references): the line `after` it lines up with `b`, where under the first line of an
    indented paragraph it would be back at the margin of `a`."""
    # The line before `b` is the full first line of the entry. A short last line of a paragraph, followed by an
    # indented heading or first line, is not.
    return (after is not None and abs(after.box[0] - b.box[0]) <= 3 < abs(after.box[0] - a.box[0])
            and 0 <= after.box[1] - b.box[3] <= max(a.size, b.size) * 1.5
            and a.box[2] - a.box[0] >= (b.box[2] - b.box[0]) * .85)


def on_same_line(a, b, same_page=True):
    """Tell whether `b` is the next text object on the printed line of `a`."""
    size = max(a.size, b.size)
    return same_page and abs((a.box[1] + a.box[3]) - (b.box[1] + b.box[3])) <= size * .5 and -size * .3 <= b.box[0] - a.box[2]


def starts_paragraph(a, b, text_a, text_b, usual=0, same_page=True, bold=True, after=None):
    """Tell whether body line `b` starts a new paragraph, `a` being the body line before it and `after` the one
    below `b`, when there is one."""
    size = max(a.size, b.size)
    below = b.box[1] - a.box[3]
    if on_same_line(a, b, same_page):
        return False  # the next text object on the same printed line (OCR and old typesetting split lines into words)
    if same_page and len(text_a) <= 2 and a.size > b.size * 1.4 and b.box[0] >= a.box[2] - b.size * .5             and a.box[1] - b.size <= b.box[1] and b.box[3] <= a.box[3] + b.size:
        return False  # a drop cap: the first lines of the paragraph wrap around its large first letter
    if NUMBERED_ITEM.match(text_b) and SENTENCE_END.search(text_a):
        return True
    head_a, head_b = is_heading(text_a, a, bold) and not runs_in(text_a), is_heading(text_b, b, bold)
    if head_a or head_b:
        # A heading that wraps onto a second line stays one paragraph, and so does a whole paragraph set in bold
        # (an abstract): its lines look like short headings, but each continues the line above.
        wrapped = (same_page and abs(a.size - b.size) <= size * .05 and -size * .4 <= below <= size * .9
                   and not SENTENCE_END.search(text_a)
                   and ((head_a and head_b) or (bold and a.leading_bold and b.leading_bold)))
        return not wrapped
    directly_below = (same_page and -size * .35 <= below <= size * 2.5 and abs(b.box[0] - a.box[0]) < size * 3.2)
    if not directly_below:
        # Another column or page, or text that resumes after a displayed equation.
        return not flows(a, b, text_a, text_b)
    indent = b.box[0] - a.box[0]
    if text_b[:1].islower() and not SENTENCE_END.search(text_a):
        return False  # the sentence goes on, whatever the spacing
    if a.paragraph != b.paragraph:
        # A PDF often starts new blocks at columns; an indent or a clear gap marks a new paragraph.
        return ((3 < indent < size * 3 and bool(TERMINAL.search(text_a)) and not hanging(a, b, after))
                or below > max(size * .65, usual + size * .4))
    # Inside one block the paragraph ends where a line is indented after a finished sentence, or at a clear gap.
    return below > max(size * .9, usual + size * .6) or (3 < indent < size * 3 and bool(TERMINAL.search(text_a))
                                                         and not hanging(a, b, after))
