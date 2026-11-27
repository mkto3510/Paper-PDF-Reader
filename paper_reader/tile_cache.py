"""Size-limited cache shared by the views of open documents."""

from collections import OrderedDict


CACHE_BYTES = 64 * 1024 * 1024


class TileCache:
    def __init__(self):
        self.tiles = OrderedDict()
        self.bytes = 0

    def get(self, key):
        tile = self.tiles.get(key)
        if tile is not None:
            self.tiles.move_to_end(key)
        return tile

    def put(self, key, tile):
        previous = self.tiles.pop(key, None)
        if previous:
            self.bytes -= previous[2].sizeInBytes()
        self.tiles[key] = tile
        self.bytes += tile[2].sizeInBytes()
        while self.bytes > CACHE_BYTES:
            self.bytes -= self.tiles.popitem(last=False)[1][2].sizeInBytes()

    def discard(self, document):
        for key in [key for key in self.tiles if key[0] == document]:
            self.bytes -= self.tiles.pop(key)[2].sizeInBytes()
