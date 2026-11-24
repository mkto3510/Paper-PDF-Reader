"""Figure/table labels and shared evidence for confirming their captions."""

from collections import Counter
from dataclasses import dataclass, replace
import re
from unicodedata import normalize

from .selection import Box, bounds


NUMBER = (r"(?:(?:[sS]|[a-zA-Z]\.)?\d{1,5}(?:[.-]\d{1,5})*"
          r"(?:\([a-zA-Z]\)|[a-zA-Z](?![a-zA-Z]))?|[IVXLCDM]{1,8}(?![a-zA-Z]))")
ARABIC = r"\d{1,5}(?:[.-]\d{1,5})*(?:\([a-zA-Z]\)|[a-zA-Z](?![a-zA-Z]))?"
LABEL = re.compile(
    rf"(?:(?<!\w)(?P<prefix>(?:extended\s+data|supplementa(?:ry|l))\s+)?(?P<english>fig(?:ure)?s?\.?|tables?)"
    rf"\s*(?P<eng_number>{NUMBER})"
    rf"|第?\s*(?P<japanese>[図表])\s*(?:[-−]\s*)?(?P<jp_number>{NUMBER})"
    rf"|第\s*(?P<ordinal>{NUMBER})\s*(?P<ordinal_kind>[図表])"
    # Other names of figures and tables. They take Arabic numbers only: "plate d" or "scheme i" are not labels.
    rf"|(?<!\w)(?P<word>plates?|schemes?|photos?|photographs?|abb(?:ildung(?:en)?)?\.?|tabellen?)"
    rf"\s*(?P<word_number>{ARABIC})"
    rf"|(?P<jword>写真|図版)\s*(?:[-−]\s*)?(?P<jword_number>{ARABIC}))", re.I,
)
# Names that print their own series of numbers: Plate 1 is not Figure 1.
WORD_TAGS = {"plate": "pl", "scheme": "sc", "photo": "ph", "photograph": "ph", "写真": "ph", "図版": "pl"}
FOLLOWING_NUMBER = re.compile(rf"\s*(?P<sep>,|、|・|–|—|〜|～|~|\s-\s|\band\b|と|および)\s*(?P<number>{NUMBER})", re.I)
CAPTION_END = re.compile(r"^\s*[.:：|]\s*(?=\S)")
BODY_LEAD = re.compile(
    r"^(?:(?:\w+ly|also|further)\s+)*(?:is|are|was|were|has|have|can|could|will|would|should|"
    r"shows?|illustrates?|presents?|gives?|lists?|provides?|summari[sz]es?|reveals?|indicates?|"
    r"reports?|compares?|describes?|depicts?|demonstrates?|contains?|includes?|agrees?|appears?|"
    r"plots?|tracks?|represents?|identifies?|summarizes?|corresponds?|mirrors?)\b"
    r"|^(?:で|に|は|が|を|の|と|から|より)", re.I,
)
CONTINUED = re.compile(r"^\s*(?:(?:[.:|]\s*)?[\[(]?\s*(?:continued\b|cont[.'’]|続き|つづき))", re.I)
LIST_ROW = re.compile(r"[.…·\s]{3,}\s*\d+\s*$")
LIST_HEADING = re.compile(r"^(?:table of contents|list of (?:figures|tables)|目次|図一覧|表一覧|図目次|表目次)$", re.I)
LEGACY_LABEL = re.compile(rf"^(?:algorithm|abbildung|tabelle|tabla|tableau)\.?\s*(?:{NUMBER})", re.I)


@dataclass(frozen=True, slots=True)
class Caption:
    kind: str
    label: str
    text: str
    box: Box
    object_box: Box | None
    continued: bool


def label_key(text):
    return normalize("NFKC", text).casefold().replace("(", "").replace(")", "")


def _word(match):
    """The name of a label that is not Figure or Table: plate, scheme, photo, Abbildung, Tabelle, 写真."""
    word = (match["word"] or match["jword"] or "").casefold().rstrip(".")
    if word.startswith("photograph"):
        return "photo"
    return "abbildung" if word.startswith("abb") else word.rstrip("n") if word.startswith("tabelle") else word.rstrip("s")


def _running_word(match):
    """A name printed in lower case (plate, scheme, photo) is a common noun; labels are capitalized."""
    return bool(match["word"]) and match["word"][0].islower()


def _raw_label(match):
    if match["word"] or match["jword"]:
        word = _word(match)
        return ("table" if word.startswith("tabelle") else "figure",
                label_key(match["word_number"] or match["jword_number"]))
    kind = (match["english"] or match["japanese"] or match["ordinal_kind"]).casefold()
    return ("figure" if kind.startswith("fig") or kind == "図" else "table",
            label_key(match["eng_number"] or match["jp_number"] or match["ordinal"]))


def _tagged(match, number):
    """Keep "Extended Data Fig. 3", "Supplementary Fig. 3" and "Plate 3" apart from the figure 3 of the article."""
    prefix = match["prefix"] if "prefix" in match.re.groupindex else None
    if match["word"] or match["jword"]:
        tag = WORD_TAGS.get(_word(match), "")
    elif prefix:
        tag = "ed" if prefix.casefold().startswith("extended") else "s"
    else:
        return number
    return number if not tag or number.startswith(tag) else tag + number


def figure_label(match):
    kind, label = _raw_label(match)
    return kind, (_tagged(match, label) if match.re is LABEL else label)


def _label_interval(first, last):
    a = re.fullmatch(r"(.*?)(\d+)", first)
    b = re.fullmatch(r"(.*?)(\d+)", last)
    if a and b and a[1] == b[1] and 0 < int(b[2]) - int(a[2]) < 50:
        return [a[1] + str(number) for number in range(int(a[2]), int(b[2]) + 1)]
    return []


def figure_mentions(text):
    for match in LABEL.finditer(text):
        if _running_word(match):
            continue
        start, end = match.span()
        kind, label = _raw_label(match)
        if match["english"] and match["english"].casefold().rstrip(".").endswith("s") and re.fullmatch(r"\d+-\d+", label):
            labels = _label_interval(*label.split("-"))
        else:
            labels = [label]
        keys = [(kind, _tagged(match, value)) for value in labels]
        if not keys:
            continue
        while extra := FOLLOWING_NUMBER.match(text, end):
            following = _tagged(match, label_key(extra["number"]))
            if extra["sep"].strip() in ("–", "—", "〜", "～", "~", "-"):
                previous = keys[-1][1]
                numbers = _label_interval(previous, following)
                if not numbers:
                    end = extra.end()
                    keys.clear()
                    break
                keys.extend((kind, number) for number in numbers[1:])
            else:
                keys.append((kind, following))
            end = extra.end()
            if len(keys) > 50:
                keys.clear()
                break
        if keys:
            yield (start, end), text[start:end], tuple(dict.fromkeys(keys))


def caption_heading(text):
    text = normalize("NFKC", text.strip())
    match = LABEL.match(text)
    if match and _running_word(match):
        match = None  # "tectonic plate 3 moves": the word is part of a sentence
    return match or LEGACY_LABEL.match(text)


def _nearby_object(line, regions, page_lines, kind):
    # Prefer a close object. A figure printed with a wider margin above its caption is accepted when no close one exists.
    return (_objects_within(line, regions, page_lines, kind, 3)
            or _objects_within(line, regions, page_lines, kind, 4))


def _objects_within(line, regions, page_lines, kind, reach):
    left, top, right, bottom = line.box
    candidates = []
    for box in regions:
        shared = min(right, box[2]) - max(left, box[0])
        if shared < min(right - left, box[2] - box[0]) * .5:
            continue
        gap = max(top - box[3], box[1] - bottom)
        if not -line.size * .3 <= gap <= line.size * reach:
            continue
        band_top, band_bottom = (box[3], top) if box[3] <= top else (bottom, box[1])
        # Short rows between the figure and its caption are axis labels, not a paragraph that separates them.
        if any(band_top < other.box[1] < band_bottom
               and min(box[2], other.box[2]) - max(box[0], other.box[0]) > line.size * 4
               and len(other.text.strip()) > 30
               for other in page_lines if other is not line):
            continue
        if box not in candidates:
            candidates.append(box)
    # A panel inside a larger figure object is part of that object.
    candidates = [box for box in candidates if not any(
        other is not box and other[0] - 2 <= box[0] and other[1] - 2 <= box[1]
        and box[2] <= other[2] + 2 and box[3] <= other[3] + 2 for other in candidates)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        ordered = sorted(candidates)
        if all(a[2] <= b[0] <= a[2] + line.size * 3
               and abs(a[1] - b[1]) <= line.size and abs(a[3] - b[3]) <= line.size
               for a, b in zip(ordered, ordered[1:])):
            return bounds(ordered)
        # The conventional side helps when a caption lies between two objects.
        # Require a particularly close object before using that tie-break.
        preferred = [box for box in candidates if
                     (0 <= top - box[3] <= line.size * 1.4 if kind == "figure"
                      else 0 <= box[1] - bottom <= line.size * 1.4)]
        if len(preferred) == 1:
            return preferred[0]
    return None


def _ruled_table(line, rules, lines):
    # Open-sided tables often have horizontal rules but no vertical border.
    left, top, right, bottom = line.box
    fragments = sorted({rule for rule in rules if top - line.size * 14 <= rule[1] <= bottom + line.size * 14},
                       key=lambda rule: (round(rule[1]), rule[0]))
    rails = []
    for fragment in fragments:
        if (rails and abs(fragment[1] - rails[-1][1]) < 1
                and rails[-1][0] - 2 <= fragment[0] <= rails[-1][2] + 2):
            rails[-1] = bounds((rails[-1], fragment))
        else:
            rails.append(fragment)
    nearby = [rule for rule in rails if rule[2] - rule[0] >= line.size * 6
              and min(right, rule[2]) - max(left, rule[0]) >= min(line.size * 3, (right - left) * .8)]
    for first, second in zip(nearby, nearby[1:]):
        if abs(first[0] - second[0]) > line.size or abs(first[2] - second[2]) > line.size:
            continue
        gap = max(top - second[3], first[1] - bottom)
        if not 0 <= gap < line.size * 2:
            continue
        cells = [other.box for other in lines if first[1] <= other.box[1] <= second[1]
                 and first[0] - line.size <= other.box[0] and other.box[2] <= first[2] + line.size]
        columns = {round(x0 / line.size) for x0, _, x1, _ in cells if x1 - x0 <= (first[2] - first[0]) * .45}
        aligned_rails = [rule for rule in nearby if abs(first[0] - rule[0]) <= line.size
                         and abs(first[2] - rule[2]) <= line.size]
        if len(columns) >= 3 or (len(aligned_rails) >= 3 and any(
                abs(a[1] - b[1]) <= line.size * .35 and b[0] - a[2] >= line.size * .5
                for a in cells for b in cells)):
            return bounds(aligned_rails)
    return None


def _continues(above, below, *, split=False):
    size = above.size
    indent = below.box[0] - above.box[0]
    same_block = above.paragraph == below.paragraph
    return ((same_block or (split and size * 1.35 <= indent)) and above.horizontal == below.horizontal
            and abs(above.box[0] - below.box[0]) < size * 3.5
            and abs(above.size - below.size) <= size * .10
            and below.box[1] - above.box[1] > size * .35
            and -size * .75 <= below.box[1] - above.box[3] <= size * .6)


def _row_order(lines):
    """Order lines top to bottom, then left to right, and keep a lone label ahead of its title.

    Text objects on one row can differ by a fraction of a point in height, which would put a title ahead of
    the label at its left. Only the right-hand neighbor of a label-only line is moved, so other lines keep
    the order they would have had."""
    ordered = sorted(lines, key=lambda line: (line.box[1], line.box[0]))
    index = 0
    while index < len(ordered):
        line = ordered[index]
        if _label_only(line):
            for other in range(index):
                neighbor = ordered[other]
                if (abs(neighbor.box[1] - line.box[1]) < line.size * .4
                        and 0 <= neighbor.box[0] - line.box[2] < line.size * 1.8):
                    ordered.insert(index, ordered.pop(other))
                    index += 1  # the label moved one place to the left
                    break
        index += 1
    return ordered


def _inline_mention(line, ordered, has_delimiter):
    """Tell whether a label that stands alone is a cross-reference inside a sentence: text of the same row
    touches it on the left, or the text on its right goes on like a sentence ("), then ...")."""
    neighbors = [other for other in ordered if other is not line and abs(other.box[1] - line.box[1]) < line.size * .4]
    # Within a sentence the neighbor is about a word space away. The gutter between columns is much wider.
    if any(-line.size * .3 <= line.box[0] - other.box[2] < line.size * .6 for other in neighbors):
        return True
    right = next((other for other in neighbors if 0 <= other.box[0] - line.box[2] < line.size * 1.8), None)
    if right is None:
        return False
    first = right.text.lstrip()[:1]
    return first in (")", "]", ",", ";") or (first.islower() and not has_delimiter)


def _label_only(line):
    """Tell whether a line holds just a caption label such as "Figure 1." (the title follows in another object)."""
    text = normalize("NFKC", line.text.strip())
    heading = caption_heading(text)
    return bool(heading) and not text[heading.end():].strip(" .:：|")


def classify_captions(lines, regions, evidence, width, height):
    """Return confirmed caption groups, their lines, and protected body rows."""
    figures, rules, _, tables, _ = evidence
    groups, caption_ids, body_ids = [], set(), set()
    if any(LIST_HEADING.fullmatch(line.text.strip()) for line in lines):
        return groups, caption_ids, body_ids
    # Use page prose as a fallback when geometry split a caption into its own region.
    marked_blocks = {line.paragraph for line in lines if caption_heading(line.text)}
    sizes = Counter()
    prose_rows = [line for line in lines
                  if line.paragraph not in marked_blocks and line.box[2] - line.box[0] > width * .26]
    for line in prose_rows:
        sizes[round(line.size, 1)] += len(line.text)
    body_size = sizes.most_common(1)[0][0] if sizes else None
    for region in regions or (lines,):
        ordered = _row_order(line for line in region if line.horizontal)
        active = []
        split_tail = set()
        metadata = None
        for index, line in enumerate(ordered):
            text = normalize("NFKC", line.text.strip())
            heading = caption_heading(text)
            # A label may be a line of its own ("Figure 1.") with the title in the next text object of the same row,
            # and the following rows may start at the label's left edge rather than at the title's.
            adjacent_label = (len(active) == 1 and (active[0].leading_bold or _label_only(active[0]))
                              and abs(active[0].box[1] - line.box[1]) < line.size * .4
                              and 0 <= line.box[0] - active[0].box[2] < line.size * 1.8)
            from_label = (len(active) == 2 and _label_only(active[0]) and _continues(
                replace(active[1], box=(active[0].box[0], *active[1].box[1:])), line))
            if active and not heading and (_continues(active[-1], line) or adjacent_label or from_label
                                           or id(line) in split_tail):
                active.append(line)
                caption_ids.add(id(line))
                continue
            if active:
                groups.append(Caption(*metadata[:2], " ".join(item.text.strip() for item in active),
                                      bounds(item.box for item in active), *metadata[2:]))
                active = []
                split_tail.clear()
            if not heading or LIST_ROW.search(text) or line.box[1] < 10 or line.box[3] > height - 10:
                continue
            previous = ordered[index - 1] if index else None
            tail = text[heading.end():]
            continued = bool(CONTINUED.match(tail))
            # "(Fig. 2.7), then" wrapped to the start of a line: a closing mark right after the label.
            english_body = (heading.re is LABEL and heading["english"]
                            and (tail.lstrip()[:1].islower() or tail.lstrip()[:1] in (")", "]")))
            following_number = FOLLOWING_NUMBER.match(tail)
            if heading.re is LABEL and ((following_number and re.search(r"\d", following_number["number"]))
                                        or re.match(r"\s*(?:および|及び|と|や|、|・)", tail)):
                english_body = True  # "図3.5および図3.6より": a list of mentions inside a sentence
            if not english_body and _label_only(line):
                english_body = _inline_mention(line, ordered, bool(tail.strip(" .:：|")) or bool(tail.strip()))
            # A real sentence can begin with a label, including within a paragraph.
            if not continued and (english_body or BODY_LEAD.match(tail.lstrip()) or (
                    previous is not None and not caption_heading(previous.text)
                    and _continues(previous, line) and not re.search(r"[.!?。][”’\"]?$", previous.text.rstrip()))):
                body_ids.add(id(line))
                following = line
                for other in ordered[index + 1:]:
                    if caption_heading(other.text) or not _continues(following, other):
                        break
                    body_ids.add(id(other))
                    following = other
                continue
            kind, label = figure_label(heading) if heading.re is LABEL else ("other", "")
            search = line
            if _label_only(line):
                # Measure the caption by its whole text, not by the few characters of its label: the title may
                # follow on the same row or on the rows below.
                last = line
                for other in ordered[index + 1:index + 8]:
                    same_row = (abs(other.box[1] - line.box[1]) < line.size * .4
                                and 0 <= other.box[0] - search.box[2] < line.size * 1.8)
                    next_row = (other.paragraph == line.paragraph and other.box[1] > last.box[1] + line.size * .5
                                and other.box[1] - last.box[3] < line.size * .8)
                    if not (same_row or next_row):
                        break
                    search = replace(search, box=bounds((search.box, other.box)))
                    last = other if next_row else last
            object_box = _nearby_object(search, figures if kind == "figure" else tables, ordered, kind)
            if kind == "table":
                object_box = object_box or _ruled_table(line, rules, lines)
                candidate_lines = [line]
                for following in ordered[index + 1:index + 4]:
                    if (caption_heading(following.text)
                            or not _continues(candidate_lines[-1], following, split=True)
                            or (object_box and line.box[3] <= object_box[1] < following.box[3])):
                        break
                    candidate_lines.append(following)
                if len(candidate_lines) > 1:
                    if object_box is None:
                        # A wrapped title may be closer to the rules at its last line.
                        title = replace(line, box=bounds(part.box for part in candidate_lines))
                        object_box = _ruled_table(title, rules, lines)
                    if object_box:
                        split_tail.update(id(part) for part in candidate_lines[1:])
            isolated = previous is None or line.box[1] - previous.box[3] > line.size * .6
            distinct = (body_size is not None and abs(line.size - body_size) > body_size * .075
                        and not any(abs(row.box[0] - line.box[0]) <= line.size * .6 for row in prose_rows))
            styled_label = line.leading_bold and (isolated or not tail.strip())
            # A line of its own that holds only "Figure 1", with a sentence starting right after it.
            following = ordered[index + 1] if index + 1 < len(ordered) else None
            label_start = (isolated and _label_only(line) and following is not None
                           and following.box[1] - line.box[3] < line.size * 1.2
                           and (following.text.strip()[:1].isupper() or following.text.strip()[:1] == "("))
            # "FIG. 2" and "TABLE I" in capitals are headings; running text writes "Fig. 2" or "Figure 2".
            capitals = (heading.re is LABEL and bool(heading["english"]) and heading["english"].rstrip(".").isupper()
                        and len(heading["english"].rstrip(".")) > 2)
            # Scanned pages carry no figure object: "Fig. 2. The effects..." that is set
            # in bold or in smaller type than the prose (flush with the column, so "distinct" does not apply).
            punctuated = (bool(CAPTION_END.match(tail)) and tail.lstrip(" .:：|")[:1].isupper()
                          and (line.leading_bold or (body_size is not None and line.size < body_size * .95)))
            if object_box is None and not (continued or distinct or styled_label or label_start or capitals
                                           or punctuated or (isolated and CAPTION_END.match(tail))):
                continue
            active = [line]
            metadata = kind, label, object_box, continued
            caption_ids.add(id(line))
        if active:
            groups.append(Caption(*metadata[:2], " ".join(item.text.strip() for item in active),
                                  bounds(item.box for item in active), *metadata[2:]))
    return groups, caption_ids, body_ids
