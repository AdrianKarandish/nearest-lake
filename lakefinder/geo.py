"""Geometry helpers: a local metre grid centred on the search point, and bounding boxes."""
import math

import shapely
from pyproj import Transformer

METRES_PER_DEG_LAT = 111_320


class LocalGrid:
    """Flat x/y grid in metres around (lat, lon). Distances near the centre are true ground distances."""

    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon
        self.crs = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
        self._to_m = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
        self._to_ll = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)

    def to_m(self, geom):
        return shapely.transform(geom, self._to_m.transform, interleaved=False)

    def to_ll(self, geom):
        return shapely.transform(geom, self._to_ll.transform, interleaved=False)

    def to_crs(self, geom, crs):
        t = Transformer.from_crs(self.crs, crs, always_xy=True)
        return shapely.transform(geom, t.transform, interleaved=False)

    def point_ll(self, pt):
        """Metre point -> (lat, lon)."""
        lon, lat = self._to_ll.transform(pt.x, pt.y)
        return lat, lon


def bbox_around(lat, lon, radius_m):
    """(south, west, north, east) in degrees covering a circle of radius_m."""
    dlat = radius_m / METRES_PER_DEG_LAT
    dlon = radius_m / (METRES_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon
