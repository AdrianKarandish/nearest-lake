"""Offline checks (no internet needed): .venv/bin/python -m pytest -q"""
from shapely.geometry import LineString, Point, Polygon, box

from lakefinder import osm
from lakefinder.nearest import not_a_lake


def test_sea_salt_seasonal_and_sewage_water_are_not_lakes():
    pond = box(100, 0, 300, 200)
    assert not_a_lake({"natural": "water"}, "unspecified", pond, None) == ""
    assert not_a_lake({"water": "lake"}, "lake", pond, None) == ""
    assert not_a_lake({"salt": "yes"}, "lake", pond, None)
    assert not_a_lake({"intermittent": "yes"}, "pond", pond, None)
    assert not_a_lake({"water": "wastewater"}, "wastewater", pond, None)
    assert not_a_lake({"water": "river"}, "river", pond, None)
    assert not_a_lake({}, "lake", pond, LineString([(310, -100), (310, 300)]))  # touches the sea coast


def test_complete_lake_with_island_becomes_polygon_with_hole():
    outer = [LineString([(0, 0), (10, 0), (10, 10)]), LineString([(10, 10), (0, 10), (0, 0)])]
    island = [LineString([(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)])]
    poly = osm.rings_to_polygon(outer + island)
    assert abs(poly.area - 96) < 1e-9


def test_partial_big_lake_rebuilt_from_nearby_shoreline():
    clip = box(0, 0, 10, 10)
    shore = [LineString([(-5, 6), (5, 6), (15, 4)])]  # lake to the north, continues off the map
    island = [LineString([(2, 8), (3, 8), (3, 9), (2, 9), (2, 8)])]
    faces, shore_u = osm.split_into_faces(shore + island, clip)
    seed = next(i for i, f in enumerate(faces) if f.contains(Point(8, 9.5)))  # we know this point is water
    water = osm.water_faces(faces, shore_u, seed, True)
    total = sum(f.area for f in water)
    expected = Polygon([(0, 6), (5, 6), (10, 5), (10, 10), (0, 10)]).area - 1  # minus the island
    assert abs(total - expected) < 1e-6


def test_parse_coords_accepts_decimal_and_degrees_minutes_seconds():
    from closest_lake import parse_coords
    assert parse_coords(["45.05", "-79.35"]) == (45.05, -79.35)
    assert parse_coords(["45.05, -79.35"]) == (45.05, -79.35)
    lat, lon = parse_coords(['43°40\'13.9"N 79°22\'47.7"W'])
    assert abs(lat - 43.670528) < 1e-6 and abs(lon + 79.379917) < 1e-6
    lat, lon = parse_coords(['60°34\'17.9"N', '111°07\'26.2"W'])
    assert abs(lat - 60.571639) < 1e-6 and abs(lon + 111.123944) < 1e-6
    assert parse_coords(["Kelowna,", "BC"]) is None
    assert parse_coords(["95.0", "-79.0"]) is None  # latitude out of range


def test_satellite_water_keeps_only_unmapped_lakes(monkeypatch):
    """Fake satellite map: an unmapped lake, a lake OSM already has, the sea, a river, a speck and a
    shadow in a city block. Only the unmapped lake should survive."""
    import numpy as np
    from affine import Affine
    from lakefinder import worldcover
    from lakefinder.geo import LocalGrid

    lat, lon = 50.0, -100.0
    grid = LocalGrid(lat, lon)
    res = worldcover.PIXEL_DEG
    transform = Affine(res, 0, lon - 300 * res, 0, -res, lat + 300 * res)
    water = np.zeros((600, 600), dtype=bool)
    water[100:140, 100:140] = True    # unmapped lake
    water[100:140, 400:440] = True    # lake OpenStreetMap already has
    water[560:600, :] = True          # the sea along the bottom edge, with a coastline
    water[200:204, 250:550] = True    # river: a long thin strip along a mapped river line
    water[300, 300] = True            # one-pixel speck
    built = np.zeros_like(water)
    water[450:460, 450:460] = True    # "pond" in the middle of a city block: building shadow
    built[430:480, 430:480] = True
    built[450:460, 450:460] = False
    monkeypatch.setattr(worldcover, "read_water", lambda *a: (water, built, transform))

    def ll_box(r0, r1, c0, c1):
        x0, y0 = transform @ (c0, r1)
        x1, y1 = transform @ (c1, r0)
        return grid.to_m(box(x0, y0, x1, y1))

    def ll_line(r, c0, c1):
        return grid.to_m(LineString([transform @ (c0, r), transform @ (c1, r)]))

    known = [ll_box(100, 140, 400, 440)]
    coast = ll_line(560, 0, 600)
    river = ll_line(202, 250, 550)
    found = worldcover.unmapped_lakes(grid, lat, lon, 3000, known, coast, [river])
    assert len(found) == 1
    poly, runs_off = found[0]
    assert poly.distance(ll_box(100, 140, 100, 140).centroid) < 1 and not runs_off
