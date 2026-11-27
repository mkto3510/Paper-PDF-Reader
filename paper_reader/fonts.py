"""Font areas and the arithmetic behind them (no Qt, so preferences can import it)."""

from collections import namedtuple
from math import floor


MINIMUM_PERCENT = 50
MAXIMUM_PERCENT = 300
FLOOR_PT = 7.0
FALLBACK_BASE_PT = 9.0
ICON_EDGES = (16, 20, 24, 28, 32, 40, 48)

Area = namedtuple("Area", "id key default floored")

AREAS = (
    Area("menu", "fontMenu", 100, True),
    Area("toolbar", "fontToolbar", 100, False),
    Area("tab", "fontTab", 100, False),
    Area("outline", "fontOutline", 130, False),
    Area("notes", "fontNotes", 130, False),
    Area("translation", "fontTranslation", 130, False),
    Area("dialog", "fontDialog", 100, True),
    Area("notice", "fontNotice", 100, False),
)
AREA_BY_ID = {area.id: area for area in AREAS}


def effective_pt(base_pt, percent, floored):
    """The point size of an area: the base size times the percentage, in 0.5 pt steps."""
    if percent == 100 and (not floored or base_pt >= FLOOR_PT):
        return base_pt
    size = base_pt * percent / 100
    if floored:
        size = max(size, FLOOR_PT)
    return floor(size * 2 + 0.5) / 2


def icon_edge(percent):
    """The toolbar icon edge in logical pixels: 16 px times the percentage, snapped to a step."""
    wanted = 16 * percent / 100
    return min(ICON_EDGES, key=lambda edge: (abs(edge - wanted), edge))
