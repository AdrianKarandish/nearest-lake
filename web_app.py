"""Local web page for the closest-lake search.

    .venv/bin/python web_app.py            then open http://localhost:8787

Type a place name or coordinates; the page shows the closest lake and its map. Only reachable from
this computer.
"""
import argparse
import json
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from closest_lake import parse_coords
from lakefinder.nearest import _dist, _size, find_nearest, geocode, write_all

HERE = Path(__file__).resolve().parent
MAPS = HERE / "output" / "closest"
LOCK = threading.Lock()  # one search at a time: the free map servers don't like parallel queries

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Closest Lake</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#15181d; --muted:#5f6670; --line:#dde1e6; --accent:#0a7cc2; --bad:#b3261e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111418; --card:#1a1e24; --ink:#e8ebef; --muted:#9aa3ad; --line:#2c323a; --accent:#4fb3f0; --bad:#f2b8b5; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system, system-ui, sans-serif; }
  main { max-width: 980px; margin: 0 auto; padding: 24px 16px 40px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 18px; }
  form { display:flex; gap:8px; flex-wrap:wrap; }
  input { font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--ink); }
  #q { flex: 1 1 320px; }
  #min { width: 150px; }
  button { font:inherit; padding:10px 18px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
  button:disabled { opacity:.6; cursor:wait; }
  .examples { color:var(--muted); font-size:13px; margin:8px 0 0; }
  .examples a { color:var(--accent); cursor:pointer; margin-right:10px; }
  #status { margin:16px 0 0; color:var(--muted); min-height:1.5em; }
  #status.err { color:var(--bad); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-top:16px; }
  .big { font-size:20px; font-weight:600; }
  .meta { color:var(--muted); font-size:14px; }
  ol, ul { margin:6px 0 0; padding-left:20px; }
  iframe { width:100%; height:560px; border:1px solid var(--line); border-radius:10px; margin-top:16px; background:var(--card); }
  a { color: var(--accent); }
</style>
</head>
<body>
<main>
  <h1>Closest lake</h1>
  <p class="sub">Type a place in Canada or coordinates (latitude, longitude).</p>
  <form id="f">
    <input id="q" placeholder="e.g. Kelowna, BC  or  45.05, -79.35" autofocus required>
    <input id="min" type="number" min="0" step="0.1" placeholder="Min size (ha)">
    <button id="go">Find</button>
  </form>
  <p class="examples">Try:
    <a data-q="Kelowna, BC">Kelowna, BC</a>
    <a data-q="Halifax, NS">Halifax, NS</a>
    <a data-q="45.05, -79.35">45.05, -79.35</a>
  </p>
  <div id="status"></div>
  <div id="result"></div>
</main>
<script>
const f = document.getElementById('f'), q = document.getElementById('q'), min = document.getElementById('min');
const go = document.getElementById('go'), status = document.getElementById('status'), result = document.getElementById('result');
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.querySelectorAll('.examples a').forEach(a => a.onclick = () => { q.value = a.dataset.q; f.requestSubmit(); });

f.onsubmit = async e => {
  e.preventDefault();
  go.disabled = true; status.className = ''; result.innerHTML = '';
  const t0 = Date.now();
  const tick = setInterval(() => {
    status.textContent = `Searching... ${Math.round((Date.now() - t0) / 1000)} s (a new area can take a minute or two)`;
  }, 500);
  try {
    const r = await fetch(`/api/closest?q=${encodeURIComponent(q.value)}&min=${encodeURIComponent(min.value || 0)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Something went wrong');
    status.textContent = `Done in ${d.seconds} s.`;
    let h = `<div class="card"><div class="meta">${esc(d.location.label)}</div>`;
    if (!d.closest) {
      h += `<div class="big">No lake found within ${d.searched_km} km.</div>`;
    } else {
      const c = d.closest;
      h += `<div class="big">${esc(c.name)} — ${esc(c.distance)}</div>
        <div class="meta">Nearest shore ${c.shore_lat}, ${c.shore_lon} · ${esc(c.size)} · type: ${esc(c.type)} · ` +
        (c.osm ? `<a href="${c.osm}" target="_blank" rel="noopener">OpenStreetMap</a>`
               : `found on the satellite water map, not in OpenStreetMap`) + `</div>`;
      if (d.next.length) h += `<p style="margin:12px 0 0">Next closest:</p><ol start="2">` +
        d.next.map(n => `<li>${esc(n.name)} — ${esc(n.distance)} (${esc(n.size)})</li>`).join('') + `</ol>`;
      if (d.skipped.length) h += `<p style="margin:12px 0 0">Closer water that was skipped:</p><ul>` +
        d.skipped.map(n => `<li>${esc(n.name)} — ${esc(n.distance)}: ${esc(n.reason)}</li>`).join('') + `</ul>`;
    }
    h += `</div><iframe src="${d.map}" title="Map"></iframe>
      <p class="meta"><a href="${d.map}" target="_blank" rel="noopener">Open map in its own tab</a> · files saved in ${esc(d.folder)}</p>`;
    result.innerHTML = h;
  } catch (err) {
    status.className = 'err'; status.textContent = err.message;
  } finally {
    clearInterval(tick); go.disabled = false;
  }
};
</script>
</body>
</html>
"""


def search(text, min_ha):
    coords = parse_coords([text])
    place = ""
    if coords:
        lat, lon = coords
        label = f"{lat:.5f}, {lon:.5f}"
    else:
        hit = geocode(text)
        if hit is None:
            raise ValueError(f"Could not find '{text}' in Canada. Try coordinates, e.g. 45.05, -79.35")
        lat, lon, label = hit
        place = text
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", place or f"{lat:.4f}_{lon:.4f}").strip("_") or "run"
    if min_ha > 0:
        slug += f"_min{min_ha:g}"
    t0 = time.time()
    res = find_nearest(lat, lon, HERE / "cache", place=place, min_size_ha=min_ha,
                       log=lambda msg: print(f"  [{slug}] {msg.strip()}", flush=True))
    write_all(res, MAPS / slug)
    g = res.grid

    def item(w):
        return {"name": w.name, "distance": "you are on it" if w.distance_m < 1 else _dist(w.distance_m),
                "size": _size(w)}

    closest = None
    if res.lakes:
        best = res.lakes[0]
        s_lat, s_lon = g.point_ll(best.shore_m)
        closest = dict(item(best), shore_lat=round(s_lat, 5), shore_lon=round(s_lon, 5),
                       type=best.kind if best.kind != "unspecified" else "not tagged",
                       osm=f"https://www.openstreetmap.org/{best.osm}" if best.osm else "")
    return {
        "location": {"lat": lat, "lon": lon, "label": label},
        "closest": closest,
        "next": [item(w) for w in res.lakes[1:5]],
        "skipped": [dict(item(w), reason=w.not_a_lake) for w in res.skipped[:5]],
        "searched_km": res.radius_m / 1000,
        "map": f"/maps/{slug}/map.html?t={int(time.time())}",
        "folder": str((MAPS / slug).relative_to(HERE)),
        "seconds": round(time.time() - t0, 1),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/api/closest":
            qs = parse_qs(url.query)
            text = (qs.get("q") or [""])[0].strip()
            try:
                min_ha = max(0.0, float((qs.get("min") or ["0"])[0] or 0))
            except ValueError:
                min_ha = 0.0
            if not text:
                return self._send(400, json.dumps({"error": "Type a place or coordinates."}), "application/json")
            print(f"Search: {text!r} (min size {min_ha:g} ha)", flush=True)
            try:
                with LOCK:
                    body = search(text, min_ha)
                return self._send(200, json.dumps(body), "application/json")
            except ValueError as err:
                return self._send(400, json.dumps({"error": str(err)}), "application/json")
            except Exception as err:
                traceback.print_exc()
                return self._send(500, json.dumps({"error": f"Search failed: {err}"}), "application/json")
        if url.path.startswith("/maps/"):
            target = (MAPS / url.path[len("/maps/"):]).resolve()
            if MAPS.resolve() in target.parents and target.is_file():
                return self._send(200, target.read_bytes(), "text/html; charset=utf-8")
        return self._send(404, "Not found", "text/plain")

    def log_message(self, fmt, *args):  # keep the console to search progress only
        pass


def main():
    p = argparse.ArgumentParser(description="Local web page for the closest-lake search.")
    p.add_argument("--port", type=int, default=8787)
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Closest-lake page running at http://localhost:{args.port}  (Ctrl+C to stop)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
