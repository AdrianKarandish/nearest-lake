"""Closest lake to a location. Give coordinates or a place name:

    .venv/bin/python closest_lake.py 45.05 -79.35
    .venv/bin/python closest_lake.py "Kelowna, BC"
    .venv/bin/python closest_lake.py "Kelowna, BC" --min-size 1     (only lakes of 1 ha or more)
    .venv/bin/python closest_lake.py "43°40'13.9\"N 79°22'47.7\"W"   (degrees, minutes, seconds)

Writes output/closest/<name>/map.html, lakes.csv, lakes.geojson and summary.txt.
"""
import argparse
import re
import sys
import time
from pathlib import Path

from lakefinder.nearest import find_nearest, geocode, write_all

HERE = Path(__file__).resolve().parent


# Degrees, minutes, seconds as Google Maps copies them: 43°40'13.9"N 79°22'47.7"W
DMS = re.compile(r"""(\d+(?:\.\d+)?)\s*°\s*(?:(\d+(?:\.\d+)?)\s*['′’]\s*)?"""
                 r"""(?:(\d+(?:\.\d+)?)\s*(?:"|″|”|'')\s*)?([NSEWnsew])""")


def parse_coords(words):
    """'45.05 -79.35', '45.05, -79.35' or 43°40'13.9"N 79°22'47.7"W -> (lat, lon); anything else -> None."""
    text = " ".join(words)
    lat = lon = None
    parts = DMS.findall(text)
    if len(parts) == 2:
        for deg, mins, secs, hemi in parts:
            value = float(deg) + float(mins or 0) / 60 + float(secs or 0) / 3600
            if hemi.upper() in "NS":
                lat = -value if hemi.upper() == "S" else value
            else:
                lon = -value if hemi.upper() == "W" else value
    elif re.fullmatch(r"[-\d.,\s]+", text):
        nums = re.findall(r"-?\d+(?:\.\d+)?", text)
        if len(nums) == 2:
            lat, lon = float(nums[0]), float(nums[1])
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def main():
    p = argparse.ArgumentParser(description="Find the closest lake to a location.")
    p.add_argument("location", nargs="+", help='"lat lon" (e.g. 45.05 -79.35) or a place name (e.g. "Kelowna, BC")')
    p.add_argument("--min-size", type=float, default=0, metavar="HECTARES",
                   help="skip lakes smaller than this, e.g. 1 (default: any size)")
    p.add_argument("--name", default="", help="output folder name")
    p.add_argument("--fresh", action="store_true", help="re-download map data instead of using the 7-day cache")
    args = p.parse_args()

    coords = parse_coords(args.location)
    place = ""
    if coords:
        lat, lon = coords
    else:
        query = " ".join(args.location)
        hit = geocode(query)
        if hit is None:
            sys.exit(f"Could not find '{query}' in Canada. Try coordinates instead, e.g. 45.05 -79.35")
        lat, lon, place = hit
        print(f"Found: {place}")
        place = query

    name = args.name or place or f"{lat:.4f}_{lon:.4f}"
    out = HERE / "output" / "closest" / (re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "run")

    t0 = time.time()
    res = find_nearest(lat, lon, HERE / "cache", place=place, min_size_ha=args.min_size, fresh=args.fresh)
    text, files = write_all(res, out)
    print()
    print(text)
    print()
    print(f"Done in {time.time() - t0:.0f} s. Map: {files[0]}")


if __name__ == "__main__":
    main()
