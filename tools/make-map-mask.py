#!/usr/bin/env python3
"""Prints MAP_MASK for raincheck: sea, the two land colours and the borders of Buienradar's radar map.

  tools/make-map-mask.py background.png

background.png is Buienradar's WebMercator radar map with its background, at its
own size of 1058 x 915, fetched at a dry moment so no rain hides the land:

  https://image.buienradar.nl/2.0/image/single/RadarMapRainWebmercatorNL?width=1058&height=915&renderBackground=True&renderBranding=False&renderText=False&extension=png

The yellow city dots are dropped; the map is scaled down to MAP_MASK_SIZE with a
border wherever one crosses a pixel, and packed four pixels to a byte.
"""
import base64
import importlib.machinery
import importlib.util
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("raincheck", os.path.join(HERE, "..", "raincheck"))
raincheck = importlib.util.module_from_spec(importlib.util.spec_from_loader("raincheck", _loader))
_loader.exec_module(raincheck)

DOT_SIZE = 9


def classify(red, green, blue):
    if blue > 100 and green < 80:
        return raincheck.SEA
    if red > 150 and green > 150 and blue < 100:
        return raincheck.BORDER
    return raincheck.LAND_DARK if green < 150 else raincheck.LAND_LIGHT


def classes_of(picture):
    rows = []
    for row in picture.rows:
        step = picture.channels
        rows.append([classify(*row[x:x + 3]) for x in range(0, len(row), step)])
    return rows


def drop_dots(grid):
    """Turns small blobs of border colour, the city dots, into the land around them."""
    height, width = len(grid), len(grid[0])
    seen = set()
    for y in range(height):
        for x in range(width):
            if grid[y][x] != raincheck.BORDER or (x, y) in seen:
                continue
            blob = flood(grid, x, y, seen)
            xs, ys = [px for px, _ in blob], [py for _, py in blob]
            if max(xs) - min(xs) < DOT_SIZE and max(ys) - min(ys) < DOT_SIZE:
                land = surrounding_land(grid, blob)
                for px, py in blob:
                    grid[py][px] = land


def flood(grid, x, y, seen):
    blob, todo = [], [(x, y)]
    seen.add((x, y))
    while todo:
        px, py = todo.pop()
        blob.append((px, py))
        for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
            if 0 <= ny < len(grid) and 0 <= nx < len(grid[0]) and (nx, ny) not in seen and grid[ny][nx] == raincheck.BORDER:
                seen.add((nx, ny))
                todo.append((nx, ny))
    return blob


def surrounding_land(grid, blob):
    counts = {}
    for px, py in blob:
        for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
            if 0 <= ny < len(grid) and 0 <= nx < len(grid[0]) and grid[ny][nx] != raincheck.BORDER:
                counts[grid[ny][nx]] = counts.get(grid[ny][nx], 0) + 1
    return max(counts, key=counts.get) if counts else raincheck.LAND_LIGHT


def main(path):
    with open(path, "rb") as file:
        grid = classes_of(raincheck.decode_png(file.read()))
    drop_dots(grid)
    width, height = raincheck.MAP_MASK_SIZE
    small = raincheck.shrink_mask(grid, width, height)
    packed = bytearray()
    flat = [value for row in small for value in row]
    for start in range(0, len(flat), 4):
        quad = flat[start:start + 4] + [0] * (4 - len(flat[start:start + 4]))
        packed.append(quad[0] << 6 | quad[1] << 4 | quad[2] << 2 | quad[3])
    text = base64.b64encode(zlib.compress(bytes(packed), 9)).decode()
    print("MAP_MASK = (")
    for start in range(0, len(text), 100):
        print(f'    "{text[start:start + 100]}"')
    print(")")


if __name__ == "__main__":
    main(sys.argv[1])
