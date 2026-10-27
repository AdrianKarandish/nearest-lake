"""Closest lake to a location: the nearest fresh-water lake, pond or reservoir.

Lakes come from OpenStreetMap, plus lakes seen on the ESA WorldCover satellite map that OpenStreetMap
hasn't mapped. Searches 3 km around the point first and widens (10, 30, 80 km) only if nothing is found.
"""
import csv
import html
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import folium
import requests
from shapely.geometry import Point, mapping
from shapely.ops import nearest_points, unary_union

from . import osm, worldcover
from .geo import LocalGrid

LAKE_TYPES = {"lake", "reservoir", "pond", "oxbow", "unspecified"}
SEARCH_STEPS_M = (3_000, 10_000, 30_000, 80_000)
SPECK_HA = 0.01  # under 100 m2 is mapping noise (fountains, pools), never a lake
COAST_TOUCH_M = 30
RIVER_KINDS = {"river", "canal", "stream", "riverbank"}
PROBLEM_TYPES = {
    "wastewater": "a sewage / wastewater pond",
    "lagoon": "a lagoon (often salty)",
    "basin": "a stormwater or industrial basin",
    "reflecting_pool": "a decorative pool",
    "salt_pool": "a salt pond",
    "fishpond": "a fish farm pond",
}
NOMINATIM = "https://nominatim.openstreetmap.org/search"


@dataclass
class Water:
    name: str
    kind: str
    osm: str
    partial: bool
    area_ha: float
    distance_m: float
    shore_m: object
    poly_m: object
    not_a_lake: str = ""  # why it was skipped, if it was
    source: str = "OpenStreetMap"  # or "satellite" for lakes only seen on the satellite map


@dataclass
class Nearest:
    lat: float
    lon: float
    place: str
    grid: object
    radius_m: float
    lakes: list = field(default_factory=list)    # closest first
    skipped: list = field(default_factory=list)  # closer water that isn't a usable lake


def _kind(tags):
    if tags.get("water"):
        return tags["water"]
    if tags.get("landuse") == "reservoir":
        return "reservoir"
    return "unspecified"


def _label(tags, kind):
    name = tags.get("name") or tags.get("name:en") or tags.get("name:fr")
    return name or f"Unnamed {kind if kind != 'unspecified' else 'lake'}"


def geocode(place):
    """Place name -> (lat, lon, full name) using OpenStreetMap's Nominatim, limited to Canada."""
    r = requests.get(NOMINATIM, params={"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "ca"},
                     headers=osm.HEADERS, timeout=30)
    r.raise_for_status()
    hits = r.json()
    if not hits:
        return None
    return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0]["display_name"]


def not_a_lake(tags, kind, poly_m, coast):
    if kind in PROBLEM_TYPES:
        return f"mapped as {PROBLEM_TYPES[kind]}"
    if kind not in LAKE_TYPES:
        return f"mapped as '{kind}'"
    if tags.get("salt") == "yes" or tags.get("tidal") == "yes":
        return "salt or tidal water"
    if tags.get("intermittent") == "yes" or tags.get("seasonal") == "yes":
        return "seasonal pond, often dry"
    if coast is not None and poly_m.distance(coast) < COAST_TOUCH_M:
        return "touches the sea, may be salty"
    return ""


def find_nearest(lat, lon, cache_dir, place="", min_size_ha=SPECK_HA, fresh=False, keep=20, log=print):
    grid = LocalGrid(lat, lon)
    origin = Point(0, 0)
    for radius in SEARCH_STEPS_M:
        log(f"Looking for lakes within {radius / 1000:g} km ...")
        data = osm.fetch_area(lat, lon, radius, cache_dir, fresh, log)
        coast_lines = [grid.to_m(g) for g in osm.lines_of(data["coast"])]
        coast = unary_union(coast_lines) if coast_lines else None
        found = []
        for r in osm.build_lakes(data, lat, lon, radius, cache_dir, log=log):
            poly = grid.to_m(r["poly_ll"]).buffer(0)
            if poly.is_empty:
                continue
            kind = _kind(r["tags"])
            area_ha = poly.area / 10_000
            if area_ha < SPECK_HA:
                continue
            reason = not_a_lake(r["tags"], kind, poly, coast)
            if not reason and not r["partial"] and area_ha < min_size_ha:
                reason = f"smaller than {min_size_ha:g} ha"
            # Standing on the lake: distance 0, and the "shore" is the nearest edge of the water.
            shore = nearest_points(poly.boundary, origin)[0]
            found.append(Water(name=_label(r["tags"], kind), kind=kind, osm=r["osm"], partial=r["partial"],
                               area_ha=area_ha, distance_m=poly.distance(origin), shore_m=shore,
                               poly_m=poly, not_a_lake=reason))
        # River lines, plus the banks of river and canal areas (big ones can't always be rebuilt as shapes).
        bank_ways = [wy for rel in data["relations"] if _kind(rel["tags"]) in RIVER_KINDS for wy in rel["ways"]]
        rivers = [grid.to_m(g) for g in osm.lines_of(data.get("waterways", []) + bank_ways)]
        try:
            unmapped = worldcover.unmapped_lakes(grid, lat, lon, radius, [w.poly_m for w in found],
                                                 coast, rivers, min_size_ha)
        except Exception as err:  # the satellite map is a bonus; never fail the search because of it
            log(f"    satellite water map unavailable ({err.__class__.__name__}), using OpenStreetMap only")
            unmapped = []
        for poly, runs_off in unmapped:
            found.append(Water(name="Unmapped lake", kind="seen by satellite", osm="", partial=runs_off,
                               area_ha=poly.area / 10_000, distance_m=poly.distance(origin),
                               shore_m=nearest_points(poly.boundary, origin)[0], poly_m=poly,
                               source="satellite"))
        found.sort(key=lambda w: w.distance_m)
        lakes = [w for w in found if not w.not_a_lake]
        # Anything closer than `radius` is guaranteed to be in this download, so the first lake is the closest.
        if lakes and lakes[0].distance_m <= radius:
            skipped = [w for w in found if w.not_a_lake and w.distance_m < lakes[0].distance_m]
            return Nearest(lat, lon, place, grid, radius, lakes[:keep], skipped)
    return Nearest(lat, lon, place, grid, radius, [], [w for w in found if w.not_a_lake])


# --- outputs --------------------------------------------------------------------------------------

def _dist(m):
    return f"{m:,.0f} m" if m < 1000 else f"{m / 1000:,.2f} km"


def _size(w):
    area = f"{w.area_ha:,.{2 if w.area_ha < 10 else 1}f} ha"
    if not w.partial:
        return area
    return "large, continues past the search area" if w.source == "OpenStreetMap" else f"{area}+, runs past the search area"


def _source_line(w):
    if w.source == "satellite":
        return "  Source: ESA WorldCover satellite map (not in OpenStreetMap)"
    return f"  OpenStreetMap: https://www.openstreetmap.org/{w.osm}"


def summary(res):
    where = f"{res.place} " if res.place else ""
    out = [f"Location: {where}({res.lat:.5f}, {res.lon:.5f})", ""]
    if not res.lakes:
        out.append(f"No lake found within {res.radius_m / 1000:g} km.")
        return "\n".join(out)
    best = res.lakes[0]
    s_lat, s_lon = res.grid.point_ll(best.shore_m)
    shore_away = best.shore_m.distance(Point(0, 0))
    out += [f"CLOSEST LAKE: {best.name} — " + ("you are on it" if best.distance_m < 1 else _dist(best.distance_m)),
            f"  Nearest shore: {s_lat:.5f}, {s_lon:.5f}" + (f" ({_dist(shore_away)} away)" if best.distance_m < 1 else ""),
            f"  Size: {_size(best)}   Type: {best.kind if best.kind != 'unspecified' else 'not tagged'}",
            _source_line(best)]
    if len(res.lakes) > 1:
        out += ["", "Next closest:"]
        out += [f"  {i}. {w.name} — {_dist(w.distance_m)} ({_size(w)})" for i, w in enumerate(res.lakes[1:5], 2)]
    if res.skipped:
        out += ["", "Closer water that was skipped:"]
        out += [f"  - {w.name} — {_dist(w.distance_m)}: {w.not_a_lake}" for w in res.skipped[:5]]
    return "\n".join(out)


def _row(rank, w, grid):
    s_lat, s_lon = grid.point_ll(w.shore_m)
    return {"rank": rank, "name": w.name, "distance_km": round(w.distance_m / 1000, 3),
            "shore_lat": round(s_lat, 6), "shore_lon": round(s_lon, 6), "type": w.kind,
            "area_ha": None if w.partial and w.source == "OpenStreetMap" else round(w.area_ha, 2),
            "runs_past_search_area": w.partial, "source": w.source,
            "osm": f"https://www.openstreetmap.org/{w.osm}" if w.osm else ""}


def write_map(res, path):
    g = res.grid
    m = folium.Map(location=[res.lat, res.lon], zoom_start=14, tiles=None, control_scale=True)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Imagery © Esri, Maxar, Earthstar Geographics", name="Satellite").add_to(m)
    folium.TileLayer("OpenStreetMap", name="Street map", show=False).add_to(m)

    skipped = folium.FeatureGroup(name="Skipped water (grey)").add_to(m)
    for w in reversed(res.skipped[:10]):
        folium.GeoJson(mapping(g.to_ll(w.poly_m.simplify(2))), tooltip=f"{w.name}: {w.not_a_lake}",
                       style_function=lambda _: {"color": "#95a5a6", "weight": 1, "fillOpacity": 0.15}
                       ).add_to(skipped)
    lakes = folium.FeatureGroup(name="Lakes (closest in cyan, dashed = satellite only)").add_to(m)
    for i, w in reversed(list(enumerate(res.lakes))):
        best = i == 0
        colour = "#00e5ff" if best else "#ffd60a"
        sat = w.source == "satellite"
        tip = (f"{'CLOSEST: ' if best else f'{i + 1}. '}{w.name} — {_dist(w.distance_m)}, {_size(w)}"
               + (" (seen by satellite, not in OpenStreetMap)" if sat else ""))
        folium.GeoJson(mapping(g.to_ll(w.poly_m.simplify(2))), tooltip=tip,
                       style_function=lambda _, c=colour, b=best, d=sat: {
                           "color": c, "weight": 4 if b else 1.5, "fillColor": c,
                           "fillOpacity": 0.3 if b else 0.1, "dashArray": "6 4" if d else None}).add_to(lakes)

    points = [(res.lat, res.lon)]
    if res.lakes:
        best = res.lakes[0]
        s_lat, s_lon = g.point_ll(best.shore_m)
        points.append((s_lat, s_lon))
        folium.PolyLine([(res.lat, res.lon), (s_lat, s_lon)], color="#00e5ff", weight=3,
                        dash_array="6 6").add_to(m)
        folium.Marker([s_lat, s_lon], icon=folium.Icon(color="blue", icon="tint", prefix="fa"),
                      tooltip=f"Nearest shore of {best.name}: {_dist(best.distance_m)}").add_to(m)
    label = html.escape(res.place or f"{res.lat:.5f}, {res.lon:.5f}")
    folium.Marker([res.lat, res.lon], icon=folium.Icon(color="red", icon="map-marker", prefix="fa"),
                  tooltip=label).add_to(m)

    # Frame the point and the nearest shore, at least ~1 km across.
    pad = max(0.005, max(abs(points[-1][0] - res.lat), abs(points[-1][1] - res.lon)) * 0.3)
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    m.fit_bounds([[min(lats) - pad, min(lons) - pad / math.cos(math.radians(res.lat))],
                  [max(lats) + pad, max(lons) + pad / math.cos(math.radians(res.lat))]])
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(str(path))


def write_all(res, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_map(res, out / "map.html")
    rows = [_row(i + 1, w, res.grid) for i, w in enumerate(res.lakes)]
    with open(out / "lakes.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["rank"])
        wr.writeheader()
        wr.writerows(rows)
    feats = [{"type": "Feature", "properties": row, "geometry": mapping(res.grid.to_ll(w.poly_m.simplify(1)))}
             for w, row in zip(res.lakes, rows)]
    (out / "lakes.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    text = summary(res)
    (out / "summary.txt").write_text(text + "\n")
    return text, [out / "map.html", out / "lakes.csv", out / "lakes.geojson", out / "summary.txt"]
