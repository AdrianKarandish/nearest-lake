"""OpenStreetMap data through the Overpass API: lakes, ponds, rivers and the sea coastline.

Large lakes (Lake Huron, Okanagan Lake...) are far too big to download whole, so for any lake that
reaches past the search box we download only its nearby shoreline, cut the box into pieces along that
shoreline, and ask OpenStreetMap whether one piece is inside the lake. Every shoreline crossed from
there flips water <-> land, which tells us which other pieces are water.
"""
import json
import re
import time
from functools import reduce
from pathlib import Path

import requests
from shapely.geometry import LineString, Polygon, box
from shapely.ops import linemerge, polygonize, unary_union

from .geo import bbox_around

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HEADERS = {"User-Agent": "nearest-lake (+https://github.com/AdrianKarandish/nearest-lake)"}
BOX_MARGIN_M = 1000  # the download box reaches this far past the search radius
CACHE_DAYS = 7
AREA_ID_OFFSET = 3_600_000_000  # Overpass area id = relation id + this


def overpass(query, log=print):
    """Run an Overpass query. The main server is often busy, so retry with growing waits and
    try the backup servers (with shorter timeouts) in between."""
    last = None
    for attempt in range(4):
        for i, url in enumerate(OVERPASS_URLS):
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=180 if i == 0 else 45)
            except requests.RequestException as e:
                last = f"{url}: {e.__class__.__name__}"
                continue
            if r.status_code == 400:
                errors = re.findall(r"Error</strong>:(.*?)</p>", r.text, re.S)
                raise ValueError("OpenStreetMap rejected the query: " + " ".join(re.sub(r"<[^>]+>|\s+", " ", x)
                                                                             for x in errors)[:300])
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    last = f"{url}: bad response"
                    continue
                if "runtime error" not in data.get("remark", ""):
                    return data
                last = f"{url}: {data['remark'][:120]}"
            else:
                last = f"{url}: HTTP {r.status_code}"
        wait = 15 * 2 ** attempt
        log(f"    OpenStreetMap servers busy ({last}); retrying in {wait} s ...")
        time.sleep(wait)
    raise RuntimeError(f"OpenStreetMap servers did not answer ({last})")


def fetch_area(lat, lon, radius_m, cache_dir, fresh=False, log=print):
    """Download water bodies within radius_m, plus river lines and the sea coastline a bit wider."""
    cache = Path(cache_dir) / "osm" / f"{lat:.4f}_{lon:.4f}_{int(radius_m)}.json"
    if not fresh and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_DAYS * 86400:
        return json.loads(cache.read_text())

    s, w, n, e = bbox_around(lat, lon, radius_m + BOX_MARGIN_M)
    around = f"(around:{int(radius_m)},{lat},{lon})"
    around_wide = f"(around:{int(radius_m + 500)},{lat},{lon})"
    query = f"""[out:json][timeout:180];
(way["natural"="water"]{around}; way["landuse"="reservoir"]{around};);
out tags geom;
make section name="relations"; out;
(relation["natural"="water"]{around}; relation["landuse"="reservoir"]{around};);
foreach->.r(
  .r out tags bb;
  way(r.r)({s},{w},{n},{e});
  out ids geom;
);
make section name="lines"; out;
(way["waterway"~"^(river|canal)$"]{around_wide}; way["natural"="coastline"]{around_wide};);
out tags geom;
"""
    data = {"ways": [], "relations": [], "coast": [], "waterways": []}
    section, rel = "ways", None
    for el in overpass(query, log)["elements"]:
        if el["type"] == "section":
            section = el["tags"]["name"]
        elif section == "ways":
            data["ways"].append(el)
        elif section == "relations":
            if el["type"] == "relation":
                rel = {"id": el["id"], "tags": el.get("tags", {}), "bounds": el.get("bounds"), "ways": []}
                data["relations"].append(rel)
            elif rel is not None:
                rel["ways"].append(el)
        elif el.get("tags", {}).get("natural") == "coastline":
            data["coast"].append(el)
        else:
            data["waterways"].append(el)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def coords(el):
    return [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if p]


def lines_of(elements):
    return [LineString(c) for c in map(coords, elements) if len(c) >= 2]


# --- shape building -------------------------------------------------------------------------------

def rings_to_polygon(lines):
    """Join a complete lake's shoreline pieces into rings. Even-odd rule: islands become holes,
    a pond on an island becomes water again."""
    merged = linemerge(lines)
    parts = getattr(merged, "geoms", [merged])
    polys = [Polygon(p.coords).buffer(0) for p in parts if p.is_closed and len(p.coords) >= 4]
    if not polys:
        return None
    return reduce(lambda a, b: a.symmetric_difference(b), polys)


def split_into_faces(lines, clip):
    """Cut the clip box into pieces along the (partial) shoreline."""
    clipped = [g for g in (ln.intersection(clip) for ln in lines) if not g.is_empty]
    if not clipped:
        return [], None
    shore = unary_union(clipped)
    faces = list(polygonize(unary_union(clipped + [clip.exterior])))
    return faces, shore


def crossings(seg, shore):
    hit = seg.intersection(shore)
    if hit.is_empty:
        return 0
    return len(getattr(hit, "geoms", [hit]))


def water_faces(faces, shore, seed_idx, seed_is_water):
    """Pieces that are water, given whether faces[seed_idx] is water."""
    p0 = faces[seed_idx].representative_point()
    out = []
    for i, f in enumerate(faces):
        if i == seed_idx:
            is_water = seed_is_water
        else:
            seg = LineString([p0, f.representative_point()])
            is_water = seed_is_water ^ (crossings(seg, shore) % 2 == 1)
        if is_water:
            out.append(f)
    return out


def seed_lookup(seeds, cache_dir=None, log=print):
    """seeds: [(relation_id, lat, lon)] -> [(point_in_lake, area_exists)] using Overpass is_in.
    Answers never change, so they are cached for good."""
    cache = Path(cache_dir) / "osm" / "lake_seeds.json" if cache_dir else None
    known = json.loads(cache.read_text()) if cache and cache.exists() else {}
    keys = [f"{rid}:{lat:.7f},{lon:.7f}" for rid, lat, lon in seeds]
    todo = [(k, sd) for k, sd in zip(keys, seeds) if k not in known]
    if todo:
        q = ["[out:json][timeout:120];"]
        for i, (_, (rid, lat, lon)) in enumerate(todo):
            q.append(f'is_in({lat},{lon});out ids;make seed n="{i}";out;'
                     f'area({rid + AREA_ID_OFFSET});out ids;make exists n="{i}";out;')
        answers, cur, found_inside = [], set(), set()
        for el in overpass("\n".join(q), log)["elements"]:
            if el["type"] == "seed":
                found_inside, cur = cur, set()
            elif el["type"] == "exists":
                aid = todo[len(answers)][1][0] + AREA_ID_OFFSET
                answers.append([aid in found_inside, aid in cur])
                cur = set()
            elif el["type"] == "area":
                cur.add(el["id"])
        known.update({k: ans for (k, _), ans in zip(todo, answers)})
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(known))
    return [tuple(known[k]) for k in keys]


def build_lakes(data, lat, lon, radius_m, cache_dir=None, log=print):
    """-> list of dicts {osm, tags, poly_ll, partial}. Polygons are in lon/lat."""
    s, w, n, e = bbox_around(lat, lon, radius_m + BOX_MARGIN_M)
    clip = box(w, s, e, n)
    lakes, partial, member_ids = [], [], set()

    for rel in data["relations"]:
        member_ids.update(wy["id"] for wy in rel["ways"])
        lines = lines_of(rel["ways"])
        b = rel["bounds"]
        if not lines or not b:
            continue
        complete = b["minlat"] >= s and b["maxlat"] <= n and b["minlon"] >= w and b["maxlon"] <= e
        if complete:
            poly = rings_to_polygon(lines)
            if poly is not None and not poly.is_empty:
                lakes.append({"osm": f"relation/{rel['id']}", "tags": rel["tags"], "poly_ll": poly, "partial": False})
        else:
            faces, shore = split_into_faces(lines, clip)
            if faces:
                partial.append((rel, faces, shore))

    if partial:
        seeds = []
        for rel, faces, _ in partial:
            idx = max(range(len(faces)), key=lambda i: faces[i].area)
            p = faces[idx].representative_point()
            seeds.append((rel["id"], p.y, p.x, idx))
        try:
            answers = seed_lookup([(rid, la, lo) for rid, la, lo, _ in seeds], cache_dir, log)
        except RuntimeError as err:
            log(f"  ! could not rebuild {len(partial)} large lake(s): {err}")
            answers = [(False, False)] * len(partial)
        for (rel, faces, shore), (_, _, _, idx), (inside, exists) in zip(partial, seeds, answers):
            name = rel["tags"].get("name", f"relation {rel['id']}")
            if not exists:
                log(f"  ! skipped large water body '{name}': OpenStreetMap has no lookup area for it")
                continue
            water = water_faces(faces, shore, idx, inside)
            if water:
                lakes.append({"osm": f"relation/{rel['id']}", "tags": rel["tags"],
                              "poly_ll": unary_union(water), "partial": True})

    for wy in data["ways"]:
        if wy["id"] in member_ids:
            continue
        c = coords(wy)
        if len(c) >= 4 and c[0] == c[-1]:
            lakes.append({"osm": f"way/{wy['id']}", "tags": wy.get("tags", {}),
                          "poly_ll": Polygon(c).buffer(0), "partial": False})
    return lakes
