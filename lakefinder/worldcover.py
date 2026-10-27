"""Water seen from space, for lakes OpenStreetMap hasn't mapped (common in the North).

Uses ESA WorldCover, a free global 10 m land-cover map made from Sentinel-1 and Sentinel-2 images
(class 80 = permanent water). The map is a snapshot, so lakes that appeared or dried up since are not
reflected. Water already in OpenStreetMap is removed first, then the sea, rivers and city noise (building
shadows and dark roofs read as water) are filtered out; what is left is lakes nobody has mapped.
"""
import math
import os

import numpy as np
import rasterio
from affine import Affine
from rasterio.features import rasterize, shapes, sieve
from rasterio.warp import Resampling
from rasterio.windows import from_bounds
from shapely.geometry import shape
from shapely.ops import unary_union

from .geo import METRES_PER_DEG_LAT, bbox_around

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff")

TILE_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
            "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
WATER, BUILT_UP = 80, 50
PIXEL_DEG = 1 / 12000  # the map's native grid
MAX_SIDE_PX = 4000  # wider searches are read at lower resolution
NOISE_HA = 0.1  # smaller satellite water patches are too often shadows or noise
KNOWN_BUFFER_M = 30  # grow OpenStreetMap water a little so shoreline pixels don't become "new lakes"
SEA_TOUCH_M = 50
RIVER_BAND_M = 100  # water mostly within this distance of a mapped river line is river, not lake
EDGE_MAX_HA = 50  # bigger water running off the search area that OSM doesn't have is sea or river
URBAN_RING_M = 50  # look this far around a water patch ...
URBAN_MAX = 0.3  # ... and drop it if more than this share is buildings (cities are well mapped in OSM)


def _tiles(s, w, n, e):
    """WorldCover tile names (3 x 3 degrees, named by their south-west corner) covering the box."""
    names = []
    for lat0 in range(math.floor(s / 3) * 3, math.floor(n / 3) * 3 + 1, 3):
        for lon0 in range(math.floor(w / 3) * 3, math.floor(e / 3) * 3 + 1, 3):
            names.append(f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}")
    return names


def read_water(lat, lon, radius_m):
    """-> (water mask, built-up mask, transform) on a grid aligned with the map, covering radius_m
    around the point."""
    s, w, n, e = bbox_around(lat, lon, radius_m)
    scale = max(1, math.ceil(max(n - s, e - w) / PIXEL_DEG / MAX_SIDE_PX))
    res = PIXEL_DEG * scale
    x0, y1 = math.floor(w / res) * res, math.ceil(n / res) * res
    width, height = math.ceil((e - x0) / res), math.ceil((y1 - s) / res)
    transform = Affine(res, 0, x0, 0, -res, y1)
    water = np.zeros((height, width), dtype=bool)
    built = np.zeros((height, width), dtype=bool)
    for tile in _tiles(s, w, n, e):
        try:
            with rasterio.open(TILE_URL.format(tile=tile)) as ds:
                win = from_bounds(x0, y1 - height * res, x0 + width * res, y1, ds.transform)
                a = ds.read(1, window=win, out_shape=(height, width), boundless=True, fill_value=0,
                            resampling=Resampling.nearest)
                water |= a == WATER
                built |= a == BUILT_UP
        except rasterio.errors.RasterioIOError:
            continue  # no tile here: open ocean, or outside the map
    return water, built, transform


def _share(mask, ky, kx):
    """Share of True cells in the (2ky+1) x (2kx+1) box around every cell (integral-image box sum)."""
    ii = np.pad(mask.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    h, w = mask.shape
    r0, r1 = np.clip(np.arange(h) - ky, 0, h), np.clip(np.arange(h) + ky + 1, 0, h)
    c0, c1 = np.clip(np.arange(w) - kx, 0, w), np.clip(np.arange(w) + kx + 1, 0, w)
    total = ii[r1][:, c1] - ii[r0][:, c1] - ii[r1][:, c0] + ii[r0][:, c0]
    return total / ((r1 - r0)[:, None] * (c1 - c0)[None, :])


def unmapped_lakes(grid, lat, lon, radius_m, known_m, coast_m=None, rivers_m=None, min_ha=0.0):
    """Satellite water bodies within radius_m that OpenStreetMap doesn't have.
    known_m: OSM water polygons (metres); coast_m / rivers_m: OSM sea coastline and river lines.
    -> list of (polygon in metres, runs_off_search_area)."""
    water, built, transform = read_water(lat, lon, radius_m)
    if not water.any():
        return []
    px_w = transform.a * METRES_PER_DEG_LAT * math.cos(math.radians(lat))
    px_h = -transform.e * METRES_PER_DEG_LAT
    urban = (_share(built, max(1, round(URBAN_RING_M / px_h)), max(1, round(URBAN_RING_M / px_w)))
             if built.any() else None)
    if known_m:
        known_ll = [grid.to_ll(p.buffer(KNOWN_BUFFER_M)) for p in known_m]
        water &= ~rasterize(known_ll, out_shape=water.shape, transform=transform, fill=0,
                            default_value=1, all_touched=True, dtype=np.uint8).astype(bool)
    px_m2 = px_w * px_h
    min_area_ha = max(NOISE_HA, min_ha)
    water = sieve(water.astype(np.uint8), size=max(2, math.ceil(min_area_ha * 10_000 / px_m2)), connectivity=8)

    river_band = unary_union(rivers_m).buffer(RIVER_BAND_M) if rivers_m else None
    h, w = water.shape
    found = []
    for geom, _ in shapes(water, mask=water.astype(bool), transform=transform, connectivity=8):
        poly = grid.to_m(shape(geom)).buffer(0)
        area_ha = poly.area / 10_000
        if area_ha < min_area_ha:
            continue
        if coast_m is not None and poly.distance(coast_m) < SEA_TOUCH_M:
            continue  # the sea
        if river_band is not None and poly.intersection(river_band).area > 0.5 * poly.area:
            continue  # a river mapped only as a line
        xs = [x for x, _ in geom["coordinates"][0]]
        ys = [y for _, y in geom["coordinates"][0]]
        left, top = transform.c, transform.f
        on_edge = (min(xs) <= left + 1e-9 or max(xs) >= left + w * transform.a - 1e-9 or
                   max(ys) >= top - 1e-9 or min(ys) <= top + h * transform.e + 1e-9)
        if on_edge and area_ha > EDGE_MAX_HA:
            continue  # big water running off the map that OSM doesn't call a lake: sea or river
        if urban is not None:
            cols = np.clip(((np.array(xs) - left) / transform.a).astype(int), 0, w - 1)
            rows = np.clip(((np.array(ys) - top) / transform.e).astype(int), 0, h - 1)
            if urban[rows, cols].mean() > URBAN_MAX:
                continue  # among buildings: shadows or dark roofs, not a lake
        found.append((poly, on_edge))
    return found
