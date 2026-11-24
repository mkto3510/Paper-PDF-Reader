"""A page as the index reads it: its lines and the results derived from them, each worked out once."""

from __future__ import annotations

from dataclasses import dataclass

from .captions import classify_captions
from .selection import reading_regions


@dataclass(slots=True, eq=False)
class PageLayout:
    """Lines of one page (one object per printed line), its size, and the keys of its top and bottom bands.

    The reference scan reads pages from the end and the figure scan from the start, and a figure mention may read
    the pages around it. All of them need the same reading regions and the same caption decisions, so they are kept
    here instead of being worked out again by each caller.
    """

    lines: list
    width: float
    height: float
    edges: frozenset
    _regions: list | None = None
    _captions: tuple | None = None

    @property
    def regions(self):
        if self._regions is None:
            self._regions = reading_regions(self.lines, self.width)
        return self._regions

    @property
    def reading_order(self):
        return tuple(line for region in self.regions for line in region)

    def captions(self, evidence):
        """Return (groups, caption ids, body ids) as classify_captions decides them."""
        if self._captions is None:
            self._captions = classify_captions(self.lines, self.regions, evidence, self.width, self.height)
        return self._captions
