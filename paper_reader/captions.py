"""Figure/table labels and shared evidence for confirming their captions."""

from collections import Counter
from dataclasses import dataclass, replace
import re
from unicodedata import normalize

from .selection import Box, bounds


NUMBER = (r"(?:(?:[sS]|[a-zA-Z]\.)?\d{1,5}(?:[.-]\d{1,5})*"
          r"(?:\([a-zA-Z]\)|[a-zA-Z](?![a-zA-Z]))?|[IVXLCDM]{1,8}(?![a-zA-Z]))")
LABEL = re.compile(
    rf"(?:(?<!\w)(?P<english>fig(?:ure)?s?\.?|tables?)\s*(?P<eng_number>{NUMBER})"
    rf"|第?\s*(?P<japanese>[図表])\s*(?:[-−]\s*)?(?P<jp_number>{NUMBER})"
    rf"|第\s*(?P<ordinal>{NUMBER})\s*(?P<ordinal_kind>[図表]))", re.I,
)
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


def figure_label(match):
    kind = (match["english"] or match["japanese"] or match["ordinal_kind"]).casefold()
    return ("figure" if kind.startswith("fig") or kind == "図" else "table",
            label_key(match["eng_number"] or match["jp_number"] or match["ordinal"]))


def _label_interval(first, last):
    a = re.fullmatch(r"(.*?)(\d+)", first)
    b = re.fullmatch(r"(.*?)(\d+)", last)
    if a and b and a[1] == b[1] and 0 < int(b[2]) - int(a[2]) < 50:
        return [a[1] + str(number) for number in range(int(a[2]), int(b[2]) + 1)]
    return []


def figure_mentions(text):
    for match in LABEL.finditer(text):
        start, end = match.span()
        kind, label = figure_label(match)
        if match["english"] and match["english"].casefold().rstrip(".").endswith("s") and re.fullmatch(r"\d+-\d+", label):
            labels = _label_interval(*label.split("-"))
        else:
            labels = [label]
        keys = [(kind, value) for value in labels]
        if not keys:
            continue
        while extra := FOLLOWING_NUMBER.match(text, end):
            following = label_key(extra["number"])
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
    return LABEL.match(text) or LEGACY_LABEL.match(text)


def _nearby_object(line, regions, page_lines, kind):
    left, top, right, bottom = line.box
    candidates = []
    for box in regions:
        shared = min(right, box[2]) - max(left, box[0])
        if shared < min(right - left, box[2] - box[0]) * .5:
            continue
        gap = max(top - box[3], box[1] - bottom)
        if not -line.size * .3 <= gap <= line.size * 3:
            continue
        band_top, band_bottom = (box[3], top) if box[3] <= top else (bottom, box[1])
        if any(band_top < other.box[1] < band_bottom
               and min(box[2], other.box[2]) - max(box[0], other.box[0]) > line.size * 4
               for other in page_lines if other is not line):
            continue
        if box not in candidates:
            candidates.append(box)
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
    fragments = sorted({rule for rule in rules if bottom - line.size * 10 <= rule[1] <= top + line.size * 10},
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
        cells = [other for other in lines if first[1] <= other.box[1] <= second[1]
                 and other.box[2] - other.box[0] <= (first[2] - first[0]) * .45
                 and first[0] - line.size <= other.box[0] < first[2]]
        columns = {round(cell.box[0] / line.size) for cell in cells}
        aligned_rails = [rule for rule in nearby if abs(first[0] - rule[0]) <= line.size
                         and abs(first[2] - rule[2]) <= line.size]
        if len(columns) >= 3 or (len(columns) == 2 and len(aligned_rails) >= 3):
            return first[0], first[1], first[2], second[3]
    return None


def _continues(above, below):
    size = above.size
    return (above.paragraph == below.paragraph and above.horizontal == below.horizontal
            and abs(above.box[0] - below.box[0]) < size * 3.5
            and abs(above.size - below.size) <= size * .10
            and below.box[1] - above.box[1] > size * .35
            and -size * .75 <= below.box[1] - above.box[3] <= size * .6)


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
        ordered = sorted((line for line in region if line.horizontal),
                         key=lambda line: (line.box[1], line.box[0]))
        active = []
        metadata = None
        for index, line in enumerate(ordered):
            text = normalize("NFKC", line.text.strip())
            heading = caption_heading(text)
            adjacent_label = (len(active) == 1 and active[0].leading_bold
                              and abs(active[0].box[1] - line.box[1]) < line.size * .4
                              and 0 <= line.box[0] - active[0].box[2] < line.size * 1.8)
            if active and not heading and (_continues(active[-1], line) or adjacent_label):
                active.append(line)
                caption_ids.add(id(line))
                continue
            if active:
                groups.append(Caption(*metadata[:2], " ".join(item.text.strip() for item in active),
                                      bounds(item.box for item in active), *metadata[2:]))
                active = []
            if not heading or LIST_ROW.search(text) or line.box[1] < 10 or line.box[3] > height - 10:
                continue
            previous = ordered[index - 1] if index else None
            tail = text[heading.end():]
            continued = bool(CONTINUED.match(tail))
            english_body = (heading.re is LABEL and heading["english"]
                            and tail.lstrip()[:1].islower())
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
            object_box = _nearby_object(line, figures if kind == "figure" else tables, ordered, kind)
            if object_box is None and kind == "table":
                # Test from the edge of a wrapped title, as its first line can
                # be farther from the first table rule than a standalone label.
                tail_line = line
                for following in ordered[index + 1:index + 4]:
                    if caption_heading(following.text) or not _continues(tail_line, following):
                        break
                    tail_line = following
                title = replace(line, box=bounds((line.box, tail_line.box)))
                object_box = _ruled_table(title, rules, lines)
            isolated = previous is None or line.box[1] - previous.box[3] > line.size * .6
            distinct = (body_size is not None and abs(line.size - body_size) > body_size * .075
                        and not any(abs(row.box[0] - line.box[0]) <= line.size * .6 for row in prose_rows))
            styled_label = line.leading_bold and (isolated or not tail.strip())
            if object_box is None and not (continued or distinct or styled_label or (isolated and CAPTION_END.match(tail))):
                continue
            active = [line]
            metadata = kind, label, object_box, continued
            caption_ids.add(id(line))
        if active:
            groups.append(Caption(*metadata[:2], " ".join(item.text.strip() for item in active),
                                  bounds(item.box for item in active), *metadata[2:]))
    return groups, caption_ids, body_ids
