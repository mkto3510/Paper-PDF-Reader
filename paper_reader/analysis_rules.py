"""Layout evidence for deciding which PDF text to omit from translation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from difflib import SequenceMatcher
import re
from statistics import median
import unicodedata

from .captions import classify_captions
from .selection import bounds, edge_band, furniture_key


RULES = ("furniture", "caption", "small", "figure", "table", "footnote",
         "margin", "equation", "code", "ruby", "line_number", "artifact", "front")
DEFAULT_RULES = {rule: True for rule in RULES} | {"edge_band": 12, "small_text": 6}
LABELS = {
    "body": ("本文", "Body text"),
    "furniture": ("ヘッダー・フッター・ページ番号", "Headers, footers and page numbers"),
    "caption": ("図表の見出し", "Captions"),
    "small": ("小さい文字・回転した文字", "Small or rotated text"),
    "figure": ("図の中の文字", "Figure labels"), "table": ("表", "Tables"),
    "footnote": ("脚注", "Footnotes"), "margin": ("余白注", "Margin notes"),
    "equation": ("別行立ての数式", "Displayed equations"),
    "code": ("コード・アルゴリズム", "Code and algorithms"), "ruby": ("ルビ", "Ruby"),
    "line_number": ("査読用行番号", "Line numbers"), "artifact": ("装飾タグ", "PDF artifacts"),
    "front": ("著者・所属・受理日・著作権", "Authors, affiliations, dates and copyright"),
}
NUMBER = re.compile(r"[-–—―\s]*([0-9０-９]+)[-–—―\s]*$")
MATH = re.compile(r"[=∑∫√≤≥±∂∇]|(?<=\s)\(\d+[a-z]?\)\s*$")
ENGLISH_WORD = re.compile(r"[A-Za-z]{3,}")
CJK_WORD = re.compile(r"[぀-ヿ㐀-鿿]")
EQUATION_NUMBER = re.compile(r"[(（]\d+(?:[.\-]\d+)*[a-z]?[)）][,.]?")
CODE = re.compile(r"^\s*(?:\d+[:.)]\s*)?(?:if|else|for|while|return|end|function|def|procedure)\b|[{};]|←|:=")
MARGINAL = re.compile(r"^(?:section|exercise|example|remark|note|節|演習|例)\s*[\d.]+", re.I)


# Words that can stand in a formula: a line of symbols with only these is a piece of an equation.
MATH_FUNCTIONS = {"log", "exp", "sin", "cos", "tan", "cot", "sec", "csc", "ln", "det", "max", "min", "lim", "mod",
                  "erf", "arctan", "arcsin", "arccos", "sinh", "cosh", "tanh", "sup", "inf", "div", "tr", "diag"}
LABEL_WORD = re.compile(r"[A-Za-z]{2,}|[぀-ヿ㐀-鿿]")
REAL_WORD = re.compile(r"[A-Za-z]{3,}|[぀-ヿ㐀-鿿]{2,}")


def symbols_only(text):
    """Tell whether a line is a piece of an equation: control or private-use characters (the glyphs of a math font)
    and no real word. A line of prose with a symbol in it has words and is left alone."""
    odd = sum(1 for char in text if not char.isspace() and unicodedata.category(char) in ("Cc", "Co"))
    if odd < 2:
        return False
    words = [word for word in REAL_WORD.findall(text) if word.casefold() not in MATH_FUNCTIONS]
    # One short Latin word is a variable name (bjk, djm); a word of any other script, or a longer one, is text.
    return not words or (len(words) == 1 and words[0].isascii() and len(words[0]) <= 3 and odd >= len(text) * .15)


def overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def inside(box, region):
    return overlap(box, region) >= area(box) * 0.82 and area(box) > 0


def page_evidence(page):
    w, h = page.rect.width, page.rect.height
    if page.rotation in (90, 270):
        w, h = h, w
    all_images = [tuple(image["bbox"]) for image in page.get_image_info()]
    images = [box for box in all_images if 0.004 * w * h <= area(box) <= 0.70 * w * h]
    scanned = any(area(box) > .77 * w * h for box in all_images)
    rules, dividers, boxes, vectors = [], [], [], []
    for drawing in page.get_cdrawings():
        rect = tuple(drawing["rect"])
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        stroke = "s" in drawing.get("type", "")
        if stroke and (width > 2 or height > 2):
            vectors.append(rect)
        if drawing.get("closePath") and 0 < area(rect) < w * h * .66 and width > 38 and height > 14:
            boxes.append(rect)
        for item in drawing["items"]:
            if item[0] == "re":
                x0, y0, x1, y1 = item[1]
                rx, ry = x1 - x0, y1 - y0
                if rx >= 18 and ry <= 2:
                    rules.append((x0, y0, x1, y1))
                elif rx > 38 and ry > 14 and rx * ry < w * h * .66:
                    boxes.append((x0, y0, x1, y1))
            elif stroke and item[0] == "l":
                (ax, ay), (bx, by) = item[1:3]
                segment = min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)
                if abs(ax - bx) >= 18 and abs(ay - by) < 2:
                    rules.append(segment)
                elif abs(ax - bx) < 2 and abs(ay - by) >= 12:
                    dividers.append(segment)
    # Merge intersecting or nearby strokes into candidate vector figures.
    clusters = []
    for rect in sorted(vectors, key=lambda box: box[1]):
        match = next((entry for entry in reversed(clusters[-28:])
                      if all(rect[axis] <= entry[0][axis + 2] + 7 and entry[0][axis] <= rect[axis + 2] + 7
                             for axis in (0, 1))), None)
        if match is None:
            clusters.append([rect, 1])
        else:
            match[0] = bounds((match[0], rect))
            match[1] += 1
    images.extend(rect for rect, count in clusters if count >= 5 and w * h * .008 < area(rect) < w * h * .55)
    grids = []
    for region in boxes:
        x0, y0, x1, y1 = region
        rails = {round(y) for l, y, r, _ in rules
                 if y0 + 2 < y < y1 - 2 and l <= x0 + 5 and r >= x1 - 5}
        separators = [x for x, t, _, b in dividers if x0 + 3 < x < x1 - 3 and t <= y0 + 3 and b >= y1 - 3]
        if len(rails) >= 2 and separators:
            grids.append(region)
    return images, rules, boxes, grids, scanned


def _rows(lines, tolerance=.35):
    rows = []
    for line in sorted(lines, key=lambda l: ((l.box[1] + l.box[3]) / 2, l.box[0])):
        center = (line.box[1] + line.box[3]) / 2
        if not rows or center - rows[-1][0] > max(2, line.size * tolerance):
            rows.append((center, []))
        rows[-1][1].append(line)
    return rows


def _similar_edge(line, side, key, neighbours, height):
    for old_side, old_key, old_y in neighbours:
        if old_side != side or abs(old_y - line.box[1] / height) > .025:
            continue
        if key == old_key or (min(len(key), len(old_key)) >= 12 and
                              SequenceMatcher(None, key, old_key).ratio() >= .87):
            return True
    return False


def _mark_figure_text(lines, result, width, body_ids):
    """Mark the numbers inside a figure that has no image or drawing to show where it is (a scan, an old print).

    Near a caption, five or more short lines of numbers and symbols that follow each other and do not share a left
    edge are the tick labels of a figure. A line with a word is never marked: a review of every line that a first
    version marked by shape alone found that 80% of the lines with words were article text (running text in narrow
    columns, captions, Japanese text), while the lines of numbers were figure labels or table and equation pieces."""
    captions = [line for line in lines if result[id(line)].kind == "caption"]
    if not captions:
        return
    short = sorted((line for line in lines
                    if not result[id(line)].kind and line.horizontal and id(line) not in body_ids
                    and line.box[2] - line.box[0] < width * .30 and 0 < len(line.text.strip()) <= 28
                    and not LABEL_WORD.search(line.text)),
                   key=lambda line: line.box[1])
    for caption in captions:
        top = caption.box[1]
        cluster = []
        for line in [*(line for line in short if top - 300 <= line.box[1] <= top + 15), None]:
            if line is not None and (not cluster or line.box[1] - cluster[-1].box[3] <= 30):
                cluster.append(line)
                continue
            edges = {round(item.box[0] / 8) for item in cluster}
            if len(cluster) >= 5 and len(edges) >= 3:
                for item in cluster:
                    if not result[id(item)].kind:
                        result[id(item)] = replace(item, kind="figure")
            cluster = [line] if line is not None else []


# Where the text of an article begins, on its first page: what comes above is the title block.
BODY_START = re.compile(r"^(?:abstract|article\s*info|summary|key\s*words?|index\s+terms|introduction|1\.?\s+introduction|"
                        r"要\s*旨|概\s*要|要\s*約|あらまし|アブストラクト|キーワード|はじめに|1\.?\s*はじめに)\b", re.I)
DATE_LINE = re.compile(r"^(?:manuscript\s+)?(?:received|accepted|revised|published|available\s+online|submitted|"
                       r"受付|受理|原稿受付|原稿受理|投稿|改訂)\b|^©|^copyright\b|^\(?c\)\s*\d{4}", re.I)
AFFILIATION = re.compile(r"\b(?:universit(?:y|ät|é|à)|universidad|institut(?:e|ion|o)?|department|dept\.|laborator(?:y|ies)|"
                         r"faculty|school\s+of|college|centre|center|observatory|academy|hospital|ministry|agency|"
                         r"division)\b|大学|研究所|研究科|学部|学科|センター|機構|研究院|気象庁|研究部|事業所|観測所|教室", re.I)
CONTACT = re.compile(r"@|\b(?:corresponding\s+author|correspondence|e-?mail)\b|連絡先|責任著者", re.I)
LATIN_NAME = re.compile(r"(?:[A-Z][\w'’.-]*\.?)(?:\s+(?:[A-Z][\w'’.-]*\.?|van|von|de|der|la|le|di|da|dos)){1,3}")
JAPANESE_NAME = re.compile(r"[㐀-鿿々ァ-ヶー]{2,5}")


def _unspaced(text):
    """A heading set with spaces between its letters ("A B S T R A C T") without them."""
    text = text.strip()
    return text.replace(" ", "") if re.fullmatch(r"(?:\S ){2,}\S", text) else text


def _author_line(text):
    """Tell whether a line is a list of author names: several names split by commas, "and" or a middle dot,
    each possibly followed by a note mark (a digit, an asterisk, a dagger)."""
    if re.search(r"[()（）［］\[\]「」『』]", text):
        return False  # a name list has no brackets; a wrapped line of a sentence often has one
    plain = re.sub(r"[\d*†‡§¶²³¹⁰-⁹＊]+", "", text)
    plain = re.sub(r"\s[a-z](?!\w)", "", plain)  # a letter that marks an affiliation: "Petaev b"
    parts = [part.strip() for part in re.split(r",|;|\band\b|&|・|、|，", plain) if part.strip()]
    if len(parts) < 2 or len(text) > 200:
        return False
    names = sum(bool(LATIN_NAME.fullmatch(part) or (JAPANESE_NAME.fullmatch(part) and len(parts) >= 3))
                for part in parts)
    return names >= len(parts) * .8


def _mark_front_matter(lines, result, size, body_ids):
    """Mark the author line, the affiliations, the contact line and the dates of a first page.

    Only lines above the first line that opens the text (an abstract, keywords, an introduction) are looked at, and
    when no such line is found nothing is marked: the title and the abstract are text to read, not front matter."""
    starts = [line for line in lines if line.horizontal and BODY_START.match(_unspaced(line.text))]
    if not starts:
        return
    limit = min(line.box[1] for line in starts)
    for line in lines:
        text = line.text.strip()
        if (result[id(line)].kind or not line.horizontal or line.box[3] > limit + 1 or id(line) in body_ids
                or (line.size > size * 1.15 and not (line.size < size * 1.5 and _author_line(text)))
                or not text or len(text) > 220):
            continue
        if (text[0].islower() and not re.match(r"[a-z]\s+[A-Z]", text)) or line.text[:1] in ("　", "	"):
            continue  # a line that goes on from the one above, or opens an indented paragraph: running text
        sentence = len(text.split()) >= 12 and text.endswith(".")
        if (DATE_LINE.match(text) or CONTACT.search(text) or (AFFILIATION.search(text) and not sentence)
                or _author_line(text)):
            result[id(line)] = replace(line, kind="front")


# The notes at the foot of a first page: the dates, the authors and their affiliations, the corresponding author,
# the contact address and the copyright line. A line of the running text that happens to start with "Accepted" is not
# one, so a date line needs a month or a year.
NOTE_DATE = re.compile(r"^(?:manuscript\s+)?(?:received|accepted|revised|published|available\s+online|submitted)\b.{0,70}?"
                       r"(?:\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b(?:19|20)\d\d\b)", re.I)
NOTE_LEAD = re.compile(r"^(?:the\s+authors?\s+(?:is|are|was|were)\s+with|\*+\s*corresponding\s+authors?|"
                       r"corresponding\s+authors?\b|\*?\s*e-?mail\s*:|digital\s+object\s+identifier|copyright\b|\(c\)\s*\d{4}|©)",
                       re.I)


def _mark_front_notes(lines, result, width, height, size):
    """Mark the notes at the foot of a first page, and the lines that go on from them below."""
    below = sorted((line for line in lines if line.horizontal and line.box[1] > height * .5), key=lambda l: l.box[1])
    chain = None  # the first line of the note being followed, the bottom and the right edge of what has been seen
    for line in below:
        text = line.text.strip()
        opens = bool(text) and (text[0].isupper() or text[0] in "*©(") and (NOTE_DATE.match(text) or NOTE_LEAD.match(text))
        if opens and line.size <= size * 1.05:
            chain = line, line.box[3], line.box[2]
        elif chain is not None:
            start, bottom, right = chain
            same_column = start.box[0] - size * 2 <= line.box[0] < right
            if not same_column:
                continue  # a line of the other column at the same height
            if line.box[1] > bottom + size * .8 or abs(line.size - start.size) > start.size * .1 or not text:
                chain = None
                continue
            chain = start, max(bottom, line.box[3]), max(right, line.box[2])
        else:
            continue
        if result[id(line)].kind in ("", "small"):  # small print at the foot of a first page is such a note too
            result[id(line)] = replace(line, kind="front")


def classify(lines, width, height, neighbours, evidence, options, regions=(), first_page=False):
    if not lines:
        return ()
    edge_fraction = options["edge_band"] / 100
    vertical = sum(len(line.glyphs) for line in lines if not line.horizontal)
    vertical_body = vertical > sum(len(line.glyphs) for line in lines) / 2
    main = [line for line in lines if line.horizontal != vertical_body and not edge_band(line, height, edge_fraction)]
    sizes = Counter()
    for line in main:
        sizes[round(line.size, 1)] += len(line.glyphs)
    size = sizes.most_common(1)[0][0] if sizes else median(line.size for line in lines)
    # A journal can use larger type for the abstract than for the entire main text.
    # Estimate the type size in each geometric region from nearby prose, not math or labels.
    _, caption_ids, body_ids = classify_captions(lines, regions, evidence, width, height)
    line_sizes, prose = {}, set(body_ids)
    for region in regions:
        regular = [line for line in region if line.horizontal != vertical_body
                   and line.box[2] - line.box[0] >= width * .23
                   and not edge_band(line, height, edge_fraction) and id(line) not in caption_ids]
        if not regular:
            continue
        for line in region:
            nearby = sorted(regular, key=lambda other: abs(line.box[1] - other.box[1]))[:7]
            if len(nearby) >= 2:
                line_sizes[id(line)] = median(other.size for other in nearby)
        paragraphs = {line.paragraph for line in regular
                      if len(ENGLISH_WORD.findall(line.text)) >= 4 or len(CJK_WORD.findall(line.text)) >= 8}
        prose.update(id(line) for line in region if line.paragraph in paragraphs)
    body = [line for line in main if line.box[2] - line.box[0] >= width * .24
            and line_sizes.get(id(line), size) * .9 <= line.size <= line_sizes.get(id(line), size) * 1.12]
    left = min((line.box[0] for line in body), default=0)
    right = max((line.box[2] for line in body), default=width)
    images, rules, boxes, grids, scanned = evidence
    figures = [region for region in images if sum(inside(line.box, region) for line in body) < 2]
    footnotes = [rule for rule in rules if rule[1] > height * .55 and 18 < rule[2] - rule[0] < width * .34
                 and any(abs(line.box[0] - rule[0]) < 8 and 0 <= line.box[1] - rule[3] < 25
                         and line.size < size * .95 for line in lines)]
    table_cells = set()
    equations = set()
    rows = _rows(lines)
    for _, row in _rows(lines, tolerance=.65):
        runs = []
        for part in sorted((line for line in row if line.horizontal), key=lambda line: line.box[0]):
            if not runs or part.box[0] - runs[-1][-1].box[2] > size * 1.45:
                runs.append([])
            runs[-1].append(part)
        for run in runs:
            # An extracted equation can be split into several objects on the same
            # printed line as a clause. A separate displayed equation lacks that clause.
            if (len(run) > 1 and run[-1].box[2] - run[0].box[0] >= width * .30
                    and any(len(ENGLISH_WORD.findall(line.text)) >= 4 for line in run)
                    and any(MATH.search(line.text) for line in run)
                    and not any(inside(line.box, region) for line in run for region in grids)):
                prose.update(id(line) for line in run)
    if scanned:
        # OCR commonly assigns shrunken type to individual words in an otherwise
        # continuous line. Substantial fragments on the same baseline are prose evidence.
        for _, row in _rows(lines, tolerance=.65):
            runs = []
            for part in sorted((line for line in row if line.horizontal), key=lambda line: line.box[0]):
                if not runs or part.box[0] - runs[-1][-1].box[2] > max(part.size * 2, width * .022):
                    runs.append([])
                runs[-1].append(part)
            for run in runs:
                if len(run) > 1 and run[-1].box[2] - run[0].box[0] >= width * .22:
                    prose.update(id(part) for part in run)
        paragraphs = defaultdict(list)
        for line in lines:
            if line.horizontal and id(line) in prose:
                paragraphs[line.paragraph].append(line)
        for line in lines:
            if not line.horizontal or id(line) in prose:
                continue
            for other in paragraphs[line.paragraph]:
                horizontal_gap = max(other.box[0] - line.box[2], line.box[0] - other.box[2], 0)
                shared_height = min(other.box[3], line.box[3]) - max(other.box[1], line.box[1])
                if horizontal_gap < max(width * .14, other.size * 3) and shared_height >= (line.box[3] - line.box[1]) * .42:
                    prose.add(id(line))
                    break
    for y, row in rows:
        formulas = [line for line in row if (id(line) not in prose and MATH.search(line.text)
                    and len(ENGLISH_WORD.findall(line.text)) <= 3
                    and len(CJK_WORD.findall(line.text)) <= 5 and line.box[2] - line.box[0] < width * .65)]
        if not formulas and not any(line.box[0] > width * .74 and EQUATION_NUMBER.fullmatch(line.text.strip()) for line in row):
            continue
        for line in row:
            if (id(line) not in prose and len(ENGLISH_WORD.findall(line.text)) <= 3 and len(CJK_WORD.findall(line.text)) <= 5
                    and line.box[0] >= left + size * 1.6):
                equations.add(id(line))
        equations.update(id(line) for line in formulas)
    for y, row in rows:
        short = [line for line in row if line.box[2] - line.box[0] < width * .25]
        if len(short) >= 3 and (sum(any(c.isdigit() for c in line.text) for line in short) >= 2
                                or (any(rule[1] < y and y - rule[1] < 58 for rule in rules)
                                    and any(rule[1] > y and rule[1] - y < 58 for rule in rules))):
            table_cells.update(id(line) for line in short)
    numeric = defaultdict(list)
    for line in lines:
        if number := NUMBER.fullmatch(line.text.strip()):
            if not edge_band(line, height, edge_fraction) and (line.box[2] <= left + 6 or line.box[0] >= right - 6
                     or all(overlap(line.box, b.box) == 0 for b in body)):
                numeric[round(line.box[0] / 3)].append((line, int(number[1])))
    numbers = set()
    for column in numeric.values():
        column.sort(key=lambda item: item[0].box[1])
        if len(column) >= 4:
            step = column[1][1] - column[0][1]
            if step in (1, 5) and all(b[1] - a[1] == step for a, b in zip(column, column[1:])):
                numbers.update(id(line) for line, _ in column)
    code = set()
    for region in boxes:
        enclosed = [line for line in lines if inside(line.box, region)]
        if len(enclosed) >= 2 and sum(bool(CODE.search(line.text)) for line in enclosed) >= 2:
            code.update(id(line) for line in enclosed)
    ordered = sorted(lines, key=lambda line: (line.box[1], line.box[0]))
    equation = None
    result = {}
    body_lines = []
    for line in ordered:
        text = line.text.strip()
        side = edge_band(line, height, edge_fraction)
        kind = ""
        local_size = line_sizes.get(id(line), size)
        if side and (_similar_edge(line, side, furniture_key(text), neighbours, height)
                     or NUMBER.fullmatch(text) or re.match(r"^(?:https?://doi\.org/|doi\s*:)", text, re.I)):
            kind = "furniture"
        elif id(line) in numbers:
            kind = "line_number"
        elif id(line) in body_ids:
            kind = ""
        elif id(line) in caption_ids:
            kind = "caption"
        elif id(line) in code:
            kind = "code"
        elif id(line) in equations and not any(inside(line.box, region) for region in figures):
            kind, equation = "equation", line
        elif (id(line) in table_cells and id(line) not in prose) or any(inside(line.box, grid) for grid in grids):
            kind = "table"
        elif any(inside(line.box, region) for region in figures):
            kind = "figure"
        elif line.size < size * .72 and len(text) <= 32 and re.fullmatch(r"[\u3040-\u30ff]+", text) \
                and any(0 <= other.box[1] - line.box[3] <= size * .85 and
                        min(other.box[2], line.box[2]) > max(other.box[0], line.box[0]) for other in main):
            kind = "ruby"
        elif line.size < size * .96 and any(
                0 <= line.box[1] - rule[3] < 70 and rule[0] - 8 <= line.box[0] <= rule[2] + size
                for rule in footnotes):
            kind = "footnote"
        elif body and (line.box[2] < left - size * .65 or line.box[0] > right + size * .65) \
                and (MARGINAL.match(text) or line.size < size * .94):
            kind = "margin"
        elif (equation is not None and line.paragraph == equation.paragraph and
              0 <= line.box[1] - equation.box[3] < size * .75 and abs(line.box[0] - equation.box[0]) < size * 2
              and not ENGLISH_WORD.search(text) and not CJK_WORD.search(text)):
            kind, equation = "equation", line
        elif line.horizontal == vertical_body or line.size < local_size * (1 - options["small_text"] / 100):
            # A wrapped final word can have a smaller mean size due to a note marker.
            continuation = line.horizontal != vertical_body and line.size >= local_size * .79 and any(
                abs(line.box[0] - above.box[0]) < local_size * 1.5
                and -local_size * .35 <= line.box[1] - above.box[3] <= local_size * .6
                and (line.paragraph == above.paragraph
                     or (not re.search(r"[.!?][\"'”’)\]]?$", above.text.rstrip())
                         and above.box[2] - above.box[0] >= width * .23))
                for above in reversed(body_lines))
            if not continuation and (line.horizontal == vertical_body or id(line) not in prose):
                kind = "small"
        if not kind and line.horizontal and id(line) not in body_ids and symbols_only(text):
            kind, equation = "equation", line
        result[id(line)] = replace(line, kind=kind)
        if not kind:
            body_lines.append(line)
    # A formula is often cut into single symbols ("W", "T", "5") that carry no symbol glyph pair by themselves.
    # A short line without a word, next to a line that is a piece of an equation, belongs to that equation.
    pieces = [line for line in lines if result[id(line)].kind == "equation" and symbols_only(line.text.strip())]
    for line in lines:
        text = line.text.strip()
        if (result[id(line)].kind or not line.horizontal or len(text) > 4 or REAL_WORD.search(text)
                or id(line) in body_ids):
            continue
        reach = line.size * 1.5
        if any(abs(line.box[1] - piece.box[1]) <= reach and line.box[0] <= piece.box[2] + reach
               and piece.box[0] <= line.box[2] + reach for piece in pieces):
            result[id(line)] = replace(line, kind="equation")
    _mark_figure_text(lines, result, width, body_ids)
    if first_page:
        _mark_front_matter(lines, result, size, body_ids)
        _mark_front_notes(lines, result, width, height, size)
    return tuple(result[id(line)] for line in lines)
