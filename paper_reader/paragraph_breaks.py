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
HEADING_LEAD = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.|[A-Z]\.|[0-9０-９]+[.．])\s+[A-ZÀ-Ý぀-鿿]|"
    r"第\s*[0-9０-９一二三四五六七八九十]+\s*[章節項]|"
    r"(?:abstract|introduction|references|acknowledg\w*|conclusions?|discussion|methods?|results|summary|"
    r"appendix|概要|要旨|はじめに|まとめ|参考文献|謝辞|結論|考察)\b)", re.I)


def bold_tells(lines) -> bool:
    """Tell whether a bold lead says anything on this page. OCR text often marks every line as bold."""
    body = [line for line in lines if line.horizontal and not line.kind]
    return len(body) < 5 or sum(line.leading_bold for line in body) < len(body) * .8


def is_heading(text: str, line: Line, bold: bool = True) -> bool:
    # "Acknowledgments:" is a heading although a colon ends it; a finished sentence is not.
    if len(text) > 80 or SENTENCE_END.search(text):
        return False
    return bool((bold and line.leading_bold) or HEADING_LEAD.match(text))


def flows(previous: Line, line: Line, previous_text: str, text: str) -> bool:
    """Tell whether `line` continues the paragraph that `previous` ends, when `previous` is the line before it in
    reading order but not directly above it (the next column, the next page, or past a displayed equation)."""
    if abs(line.size - previous.size) > previous.size * .1:
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


def starts_paragraph(a, b, text_a, text_b, usual=0, same_page=True, bold=True):
    """Tell whether body line `b` starts a new paragraph, `a` being the body line before it."""
    size = max(a.size, b.size)
    below = b.box[1] - a.box[3]
    if same_page and abs((a.box[1] + a.box[3]) - (b.box[1] + b.box[3])) <= size * .5 and -size * .3 <= b.box[0] - a.box[2]:
        return False  # the next text object on the same printed line (OCR and old typesetting split lines into words)
    if NUMBERED_ITEM.match(text_b) and SENTENCE_END.search(text_a):
        return True
    head_a, head_b = is_heading(text_a, a, bold), is_heading(text_b, b, bold)
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
        return 3 < indent < size * 3 or below > max(size * .65, usual + size * .4)
    # Inside one block the paragraph ends where a line is indented after a finished sentence, or at a clear gap.
    return below > max(size * .9, usual + size * .6) or (3 < indent < size * 3 and bool(TERMINAL.search(text_a)))
