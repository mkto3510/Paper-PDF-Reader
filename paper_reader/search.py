"""Search normalized page text while retaining the geometry of each match."""

from __future__ import annotations

import re
import unicodedata

from .selection import bounds


def normalize(text, match_case, match_marks):
    text = unicodedata.normalize("NFKD", text)
    if not match_marks:
        text = "".join(char for char in text if unicodedata.category(char) not in ("Mn", "Me"))
    if not match_case:
        text = text.casefold()
    return text.replace("\u00ad", "").replace("’", "'").replace("‐", "-")


def character_type(char):
    code = ord(char)
    if 0x3400 <= code <= 0x9fff or 0x20000 <= code <= 0x323af:
        return "han"
    if 0x3040 <= code <= 0x309f:
        return "hiragana"
    if 0x30a0 <= code <= 0x30ff:
        return "katakana"
    return "word" if char.isalnum() or char == "_" else None


def same_word(text, left, right):
    while left >= 0 and unicodedata.category(text[left]).startswith("M"):
        left -= 1
    if left < 0 or right >= len(text):
        return False
    kind = character_type(text[left])
    return kind is not None and kind == character_type(text[right])


def find_page(lines, query, match_case=False, whole=False, match_marks=False):
    needle = " ".join(normalize(query, match_case, match_marks).split())
    if not needle:
        return []
    text, positions = [], []
    for index, line in enumerate(lines):
        if not line.glyphs:
            continue
        for glyph in line.glyphs:
            for char in normalize(glyph.text, match_case, match_marks):
                if char.isspace():
                    char = " "
                    if not text or text[-1] == " ":
                        continue
                text.append(char)
                positions.append((index, glyph.box))
        if index + 1 >= len(lines) or not text:
            continue
        next_line = lines[index + 1]
        while text and text[-1] == " ":
            text.pop()
            positions.pop()
        if not text:
            continue
        last, following = text[-1], next_line.text.lstrip()[:1]
        if last == "-" and len(text) > 1 and text[-2].isalpha() and following.isalpha():
            if "-" not in needle:
                text.pop()
                positions.pop()
        elif not (following and character_type(last) == character_type(following) == "han"):
            text.append(" ")
            positions.append((index, None))
    result = []
    for match in re.finditer(re.escape(needle), "".join(text)):
        start, end = match.span()
        if match_marks and end < len(text) and unicodedata.category(text[end]).startswith("M"):
            continue
        if whole and (same_word(text, start - 1, start) or same_word(text, end - 1, end)):
            continue
        by_line = {}
        for line, box in positions[match.start():match.end()]:
            if box is not None:
                by_line.setdefault(line, []).append(box)
        if by_line:
            result.append([bounds(boxes) for boxes in by_line.values()])
    return result
