#!/usr/bin/env python3
"""Build valley-canal-roads.html: an offline map of Phoenix-metro canals,
canal-bank roads, canal trails, street openings and light rail.

Usage:
    python build_canal_map.py              # download extract, build, delete .pbf
    python build_canal_map.py --keep-pbf   # keep the .pbf for faster refreshes
    python build_canal_map.py --pbf my.osm.pbf
    python build_canal_map.py --html-only  # re-render after editing map_template.html

Outputs valley-canal-roads.html (a single file that works from disk) and docs/,
an installable phone app to upload to any https host.

Requires: pip install osmium shapely pillow
"""
import argparse
import heapq
import json
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date

import numpy as np
import osmium
import shapely
from shapely import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
PBF_URL = "https://download.geofabrik.de/north-america/us/arizona-latest.osm.pbf"
DEFAULT_PBF = os.path.join(HERE, "arizona-latest.osm.pbf")
TEMPLATE = os.path.join(HERE, "map_template.html")
OUTPUT = os.path.join(HERE, "valley-canal-roads.html")

# Clip box: S, W, N, E
S, W, N, E = 33.15, -112.62, 33.86, -111.55
PAD = 0.01  # degrees of slack when deciding which ways to read

ALONG_DIST_M = 35.0      # sample must be this close to an open canal
ALONG_FRACTION = 0.60    # share of samples that must be close
MIN_LENGTH_M = 40.0      # canal road/trail minimum length
BARRIER_WALK_M = 60.0    # how far to look for a barrier from a junction
CONNECTOR_MAX_M = 60.0   # short spur linking a street to a canal road
SIMPLIFY_M = 5.0

ROAD_HW = {"track", "service", "unclassified"}
TRAIL_HW = {"path", "cycleway"}
EXCLUDED_SERVICE = {"parking_aisle", "driveway", "drive-through"}
STREET_HW = {
    "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link",
    "residential", "unclassified", "living_street",
}
BARRIERS = {
    "gate", "lift_gate", "swing_gate", "sliding_gate", "bollard", "block",
    "chain", "jersey_barrier", "fence", "cycle_barrier", "wall", "log",
    "rope", "height_restrictor", "hampshire_gate", "debris",
}
ROAD_TAGS = ("name", "highway", "service", "surface", "access", "motor_vehicle")

# Light rail crossings
RAIL_CLUSTER_M = 35.0     # merge crossing nodes this close into one intersection
RAIL_PARALLEL_M = 25.0    # a road this close to the tracks, away from the crossing, means street-running
RAIL_SIGNAL_M = 40.0      # traffic signals mapped this close count for the crossing
RAIL_XING_TAGS = {"level_crossing", "tram_level_crossing", "crossing"}
# Roads a car can use to reach the tracks (service driveways and parking aisles included here)
CAR_HW = STREET_HW | {"service", "track", "road", "busway", "trunk", "motorway_link"}
PARALLEL_HW = {"trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
               "living_street", "busway"}
HW_RANK = ["motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
           "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "busway", "road",
           "service", "track"]
GATED = {"yes", "full", "half", "double_half", "gate", "chain"}

# Local equirectangular projection in metres (accurate to well under 1% here)
LAT0 = (S + N) / 2
KX = 111320.0 * math.cos(math.radians(LAT0))
KY = 110574.0


def proj(coords):
    a = np.asarray(coords, dtype=float)
    return np.column_stack((a[:, 0] * KX, a[:, 1] * KY))


def unproj(xy):
    a = np.asarray(xy, dtype=float)
    return np.column_stack((a[:, 0] / KX, a[:, 1] / KY))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def download(path):
    log(f"Downloading {PBF_URL}")
    tmp = path + ".part"
    with urllib.request.urlopen(PBF_URL) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and done % (50 << 20) < (1 << 20):
                log(f"  {done >> 20} / {total >> 20} MB")
    os.replace(tmp, path)


# ---------------------------------------------------------------- reading

def pbf_timestamp(path):
    """Date of the extract from the PBF header, falling back to the file's mtime."""
    try:
        r = osmium.io.Reader(path, osmium.osm.osm_entity_bits.NOTHING)
        ts = r.header().get("osmosis_replication_timestamp")
        r.close()
        if ts:
            return ts[:10]
    except Exception:
        pass
    return time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))

def in_box(lon, lat, pad=0.0):
    return (S - pad) <= lat <= (N + pad) and (W - pad) <= lon <= (E + pad)


def read_pbf(path):
    """Return (ways, barriers, rail_xing, signals). ways: id -> dict(kind, tags, refs, coords)."""
    ways = {}
    barriers = {}
    rail_xing = {}   # node id -> crossing tags
    signals = set()  # node ids tagged highway=traffic_signals
    fp = (osmium.FileProcessor(path)
          .with_locations()
          .with_filter(osmium.filter.KeyFilter("highway", "waterway", "railway", "barrier")))
    n_seen = 0
    for obj in fp:
        if obj.is_node():
            loc = obj.location
            if not (loc.valid() and in_box(loc.lon, loc.lat, PAD)):
                continue
            t = obj.tags
            if t.get("barrier") in BARRIERS:
                barriers[obj.id] = t.get("barrier")
            if t.get("railway") in RAIL_XING_TAGS:
                rail_xing[obj.id] = {k: v for k, v in ((tg.k, tg.v) for tg in t)
                                     if k.startswith("crossing") or k == "railway"}
            if t.get("highway") == "traffic_signals":
                signals.add(obj.id)
            continue
        if not obj.is_way():
            continue
        n_seen += 1
        if n_seen % 500000 == 0:
            log(f"  {n_seen:,} tagged ways scanned")
        t = obj.tags
        hw, ww, rw = t.get("highway"), t.get("waterway"), t.get("railway")
        kind = None
        if ww == "canal":
            kind = "canal"
        elif rw in ("light_rail", "tram") and "service" not in t:
            if "sky train" in (t.get("name") or "").lower():
                continue  # PHX Sky Train (airport people mover) is tagged tram
            kind = "rail"
        elif hw in ROAD_HW or hw in TRAIL_HW or hw in CAR_HW:
            kind = "hw"
        if kind is None:
            continue
        refs, coords = [], []
        inside = False
        for nd in obj.nodes:
            loc = nd.location
            if not loc.valid():
                continue
            refs.append(nd.ref)
            coords.append((loc.lon, loc.lat))
            if not inside and in_box(loc.lon, loc.lat, PAD):
                inside = True
        if not inside or len(coords) < 2:
            continue
        keep = ("name", "highway", "service", "surface", "access", "motor_vehicle",
                "operator", "tunnel", "covered", "railway", "ref", "layer", "embedded")
        ways[obj.id] = {
            "kind": kind,
            "tags": {k: t.get(k) for k in keep if k in t},
            "refs": refs,
            "coords": coords,
        }
    return ways, barriers, rail_xing, signals


# ---------------------------------------------------------------- analysis

def is_underground(tags):
    tun = tags.get("tunnel")
    return (tun is not None and tun != "no") or tags.get("covered") == "yes"


def line_length(xy):
    d = np.diff(xy, axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum())


def analyse(ways, barriers):
    canals = {i: w for i, w in ways.items() if w["kind"] == "canal"}
    rails = {i: w for i, w in ways.items() if w["kind"] == "rail"}
    hws = {i: w for i, w in ways.items() if w["kind"] == "hw"}
    log(f"Read {len(canals)} canal ways, {len(rails)} rail ways, {len(hws)} highway ways, "
        f"{len(barriers)} barrier nodes")

    # Spatial index of open canal segments
    segs, seg_canal = [], []
    for cid, c in canals.items():
        if is_underground(c["tags"]):
            continue
        xy = proj(c["coords"])
        for k in range(len(xy) - 1):
            segs.append((xy[k], xy[k + 1]))
            seg_canal.append(cid)
    seg_geoms = shapely.linestrings(np.array(segs))
    tree = STRtree(seg_geoms)
    log(f"Indexed {len(segs):,} open canal segments")

    # "Runs alongside" test for road/trail candidates
    cand = {i: w for i, w in hws.items()
            if w["tags"].get("highway") in ROAD_HW | TRAIL_HW
            and w["tags"].get("service") not in EXCLUDED_SERVICE}
    pts, owner = [], []
    lengths = {}
    for wid, w in cand.items():
        xy = proj(w["coords"])
        w["xy"] = xy
        lengths[wid] = line_length(xy)
        samples = np.vstack((xy, (xy[:-1] + xy[1:]) / 2))
        pts.append(samples)
        owner.append(np.full(len(samples), wid, dtype=np.int64))
    pts = np.vstack(pts)
    owner = np.concatenate(owner)
    near = np.zeros(len(pts), dtype=bool)
    hit_idx, _ = tree.query(shapely.points(pts), predicate="dwithin", distance=ALONG_DIST_M)
    near[np.unique(hit_idx)] = True
    order = np.argsort(owner, kind="stable")
    ow, nr = owner[order], near[order]
    bounds = np.flatnonzero(np.diff(ow)) + 1
    canal_ways = {}
    for grp_w, grp_n in zip(np.split(ow, bounds), np.split(nr, bounds)):
        wid = int(grp_w[0])
        frac = grp_n.mean()
        if frac >= ALONG_FRACTION and lengths[wid] >= MIN_LENGTH_M:
            canal_ways[wid] = cand[wid]
    for wid, w in canal_ways.items():
        hw = w["tags"]["highway"]
        w["class"] = "trail" if hw in TRAIL_HW else "road"
    log(f"{sum(1 for w in canal_ways.values() if w['class'] == 'road')} canal-bank roads, "
        f"{sum(1 for w in canal_ways.values() if w['class'] == 'trail')} canal trails")

    # Public streets (a canal road is never its own street)
    streets = {i: w for i, w in hws.items()
               if w["tags"].get("highway") in STREET_HW and i not in canal_ways}
    street_at = defaultdict(list)
    for sid, s in streets.items():
        for r in s["refs"]:
            street_at[r].append(sid)
    canal_at = defaultdict(list)
    for wid, w in canal_ways.items():
        for r in w["refs"]:
            canal_at[r].append(wid)

    # Short connector spurs: street -> spur -> canal road/trail
    connectors = {}
    for wid, w in cand.items():
        if wid in canal_ways or wid in streets or lengths[wid] > CONNECTOR_MAX_M:
            continue
        refs = w["refs"]
        if any(r in canal_at for r in refs) and any(r in street_at for r in refs):
            connectors[wid] = w
    conn_at = defaultdict(list)
    for wid, w in connectors.items():
        for r in w["refs"]:
            conn_at[r].append(wid)
    log(f"{len(connectors)} short connector spurs")

    # Walk graph over canal roads, trails and connectors
    node_xy = {}
    adj = defaultdict(list)
    for w in list(canal_ways.values()) + list(connectors.values()):
        xy = w["xy"]
        refs = w["refs"]
        for k, r in enumerate(refs):
            node_xy[r] = xy[k]
        for k in range(len(refs) - 1):
            d = float(np.hypot(*(xy[k + 1] - xy[k])))
            adj[refs[k]].append((refs[k + 1], d))
            adj[refs[k + 1]].append((refs[k], d))

    def nearest_barrier(start):
        best = None
        dist = {start: 0.0}
        heap = [(0.0, start)]
        while heap:
            d, n = heapq.heappop(heap)
            if d > dist.get(n, math.inf):
                continue
            if n in barriers:
                return barriers[n], d, n
            for m, step in adj[n]:
                nd = d + step
                if nd <= BARRIER_WALK_M and nd < dist.get(m, math.inf):
                    dist[m] = nd
                    heapq.heappush(heap, (nd, m))
        return best

    # Canal name lookup near a point
    canal_names = {cid: c["tags"].get("name") for cid, c in canals.items()}

    def nearby_canal(xy):
        idx = tree.query_nearest(shapely.points(xy), max_distance=120)
        for i in np.atleast_1d(idx):
            name = canal_names.get(seg_canal[int(i)])
            if name:
                return name
        return None

    # Openings
    openings = []
    junctions = {}  # node -> (canal way ids, via connector ids)
    for r, wids in canal_at.items():
        if r in street_at:
            junctions.setdefault(r, (set(), set()))[0].update(wids)
    for r, cids in conn_at.items():
        if r in street_at and r not in canal_at:
            for cid in cids:
                linked = {x for rr in connectors[cid]["refs"] for x in canal_at.get(rr, [])}
                if linked:
                    j = junctions.setdefault(r, (set(), set()))
                    j[0].update(linked)
                    j[1].add(cid)

    for r, (cwids, via) in junctions.items():
        xy = node_xy[r]
        lon, lat = unproj([xy])[0]
        if not in_box(lon, lat):
            continue
        cw = [canal_ways[i] for i in sorted(cwids)]
        st = [streets[i] for i in street_at[r]]
        street_named = next((s for s in st if s["tags"].get("name")), st[0])
        classes = sorted({w["class"] for w in cw})
        b = nearest_barrier(r)
        tagsrc = [connectors[i]["tags"] for i in sorted(via)] + [w["tags"] for w in cw]
        openings.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": {
                "kind": "canal",
                "node": r,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "street": street_named["tags"].get("name") or street_named["tags"].get("ref"),
                "street_class": street_named["tags"].get("highway"),
                "conn": "+".join(classes),
                "via_connector": bool(via),
                "canal_hw": ",".join(sorted({w["tags"]["highway"] for w in cw})),
                "canal_road": next((w["tags"]["name"] for w in cw if w["tags"].get("name")), None),
                "canal": nearby_canal(xy),
                "access": next((t["access"] for t in tagsrc if t.get("access")), None),
                "motor_vehicle": next((t["motor_vehicle"] for t in tagsrc if t.get("motor_vehicle")), None),
                "barrier": b is not None,
                "barrier_type": b[0] if b else None,
                "barrier_m": round(b[1]) if b else None,
                "barrier_node": b[2] if b else None,
                "ways": sorted(cwids | via),
            },
        })
    openings.sort(key=lambda f: f["properties"]["node"])
    nb = sum(1 for o in openings if o["properties"]["barrier"])
    log(f"{len(openings)} street openings ({len(openings) - nb} no barrier, {nb} barrier)")
    return canals, rails, canal_ways, connectors, openings


# ---------------------------------------------------------------- light rail crossings

def walk_along(xy, k, dist):
    """Point `dist` metres along polyline xy from vertex k (negative = backwards), or None past the end."""
    step = 1 if dist > 0 else -1
    left = abs(dist)
    i = k
    while 0 <= i + step < len(xy):
        a, b = xy[i], xy[i + step]
        seg = float(np.hypot(*(b - a)))
        if seg >= left:
            return a + (b - a) * (left / seg)
        left -= seg
        i += step
    return None


def hw_rank(h):
    return HW_RANK.index(h) if h in HW_RANK else 99


def analyse_rail(rails, hws, rail_xing, signals):
    """Places where a car-accessible road meets the mainline light rail / streetcar tracks."""
    rail_at = defaultdict(list)
    for rid, r in rails.items():
        r["xy"] = proj(r["coords"])
        for k, n in enumerate(r["refs"]):
            rail_at[n].append((rid, k))
    car = {i: w for i, w in hws.items() if w["tags"].get("highway") in CAR_HW}
    road_at = defaultdict(list)
    for wid, w in car.items():
        for n in w["refs"]:
            if n in rail_at:
                road_at[n].append(wid)
    xnodes = sorted(road_at)
    if not xnodes:
        return []
    node_xy = {}
    for n in xnodes:
        rid, k = rail_at[n][0]
        node_xy[n] = rails[rid]["xy"][k]

    # Merge nodes of the same intersection (two tracks x two carriageways)
    parent = {n: n for n in xnodes}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    pts = np.array([node_xy[n] for n in xnodes])
    ptree = STRtree(shapely.points(pts))
    a, b = ptree.query(shapely.points(pts), predicate="dwithin", distance=RAIL_CLUSTER_M)
    for i, j in zip(a, b):
        ri, rj = find(xnodes[i]), find(xnodes[j])
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)
    clusters = defaultdict(list)
    for n in xnodes:
        clusters[find(n)].append(n)

    # Roads running alongside the tracks (for the street-median test)
    segs, seg_way = [], []
    for wid, w in car.items():
        if w["tags"].get("highway") not in PARALLEL_HW:
            continue
        xy = proj(w["coords"])
        for k in range(len(xy) - 1):
            segs.append((xy[k], xy[k + 1]))
            seg_way.append(wid)
    seg_way = np.array(seg_way)
    rtree = STRtree(shapely.linestrings(np.array(segs)))

    # Traffic signals mapped on any car road
    sig_pts = []
    seen = set()
    for w in car.values():
        for k, n in enumerate(w["refs"]):
            if n in signals and n not in seen:
                seen.add(n)
                sig_pts.append(w["coords"][k])
    stree = STRtree(shapely.points(proj(sig_pts))) if sig_pts else None

    feats = []
    for nodes in clusters.values():
        cxy = np.mean([node_xy[n] for n in nodes], axis=0)
        lon, lat = unproj([cxy])[0]
        if not in_box(lon, lat):
            continue
        road_ids = sorted({w for n in nodes for w in road_at[n]})
        roads = sorted((car[w] for w in road_ids), key=lambda w: hw_rank(w["tags"]["highway"]))
        rail_ids = sorted({rid for n in nodes for rid, _ in rail_at[n]})
        rtags = [rails[r]["tags"] for r in rail_ids]

        # Street-running: away from the crossing, is there still a road right beside the tracks?
        samples = []
        for n in nodes:
            for rid, k in rail_at[n]:
                for d in (45, 90, -45, -90):
                    p = walk_along(rails[rid]["xy"], k, d)
                    if p is not None:
                        samples.append(p)
        beside = 0.0
        if samples:
            si, ti = rtree.query(shapely.points(np.array(samples)), predicate="dwithin", distance=RAIL_PARALLEL_M)
            crossing_roads = set(road_ids)
            hit = {int(i) for i, t in zip(si, ti) if int(seg_way[t]) not in crossing_roads}
            beside = len(hit) / len(samples)
        embedded = any(t.get("embedded") == "yes" for t in rtags)
        street_running = beside >= 0.5 or embedded

        classes = [w["tags"]["highway"] for w in roads]
        driveway = all(h in ("service", "track") for h in classes)
        names = []
        for w in roads:
            nm = w["tags"].get("name")
            if nm and nm not in names:
                names.append(nm)

        xt = [rail_xing.get(n, {}) for n in nodes]
        gate = next((t["crossing:barrier"] for t in xt if t.get("crossing:barrier") in GATED), None)
        lights = any(t.get("crossing:light") == "yes" or t.get("crossing") == "traffic_signals" for t in xt)
        sig = lights
        if not sig and stree is not None:
            sig = len(stree.query(shapely.points(cxy), predicate="dwithin", distance=RAIL_SIGNAL_M)) > 0
        streetcar = any(t.get("railway") == "tram" or "streetcar" in (t.get("name") or "").lower() for t in rtags)

        setting = "gated" if gate else "shared" if embedded else "median" if street_running else "row"
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": {
                "kind": "rail",
                "node": min(nodes),
                "lat": round(lat, 6), "lon": round(lon, 6),
                "street": " & ".join(names[:2]) or None,
                "street_class": classes[0] if classes else None,
                "driveway": driveway,
                "line": "Tempe Streetcar" if streetcar else "Valley Metro Rail",
                "setting": setting,
                "shared_lane": embedded,
                "gate": gate,
                "signals": bool(sig),
                "access": next((w["tags"]["access"] for w in roads if w["tags"].get("access")), None),
                "motor_vehicle": next((w["tags"]["motor_vehicle"] for w in roads if w["tags"].get("motor_vehicle")), None),
                "nodes": sorted(nodes),
            },
        })
    feats.sort(key=lambda f: f["properties"]["node"])
    c = defaultdict(int)
    for f in feats:
        c[f["properties"]["setting"]] += 1
    log(f"{len(feats)} rail crossings ({c['median']} in a street median, {c['shared']} in shared streetcar lanes, "
        f"{c['row']} on own right-of-way, {c['gated']} gated) from {len(xnodes)} crossing nodes")
    return feats


# ---------------------------------------------------------------- output

CLIP = shapely.box(W * KX, S * KY, E * KX, N * KY)


def to_lines(coords, clip=True):
    """Simplify (in metres), optionally clip to the box, return list of coord lists."""
    g = shapely.linestrings(proj(coords))
    g = shapely.simplify(g, SIMPLIFY_M, preserve_topology=False)
    if clip:
        g = shapely.intersection(g, CLIP)
    parts = []
    for p in shapely.get_parts(shapely.line_merge(g) if g.geom_type.startswith("Multi") else g):
        if p.geom_type != "LineString" or p.is_empty or len(p.coords) < 2:
            continue
        ll = np.round(unproj(np.asarray(p.coords)), 6)
        parts.append(ll.tolist())
    return parts


def feature(parts, props):
    if not parts:
        return None
    geom = ({"type": "LineString", "coordinates": parts[0]} if len(parts) == 1
            else {"type": "MultiLineString", "coordinates": parts})
    return {"type": "Feature", "geometry": geom, "properties": props}


def build_geojson(canals, rails, canal_ways, connectors, openings, rail_xings):
    canal_f, road_f, trail_f, rail_f = [], [], [], []
    for cid, c in canals.items():
        t = c["tags"]
        f = feature(to_lines(c["coords"]), {
            "id": cid, "name": t.get("name"), "operator": t.get("operator"),
            "underground": is_underground(t), "tunnel": t.get("tunnel"), "covered": t.get("covered"),
        })
        if f:
            canal_f.append(f)
    for wid, w in list(canal_ways.items()) + list(connectors.items()):
        props = {"id": wid, **{k: w["tags"].get(k) for k in ROAD_TAGS}}
        if wid in connectors:
            props["connector"] = True
        f = feature(to_lines(w["coords"], clip=False), props)
        if not f:
            continue
        cls = w.get("class") or ("trail" if w["tags"]["highway"] in TRAIL_HW else "road")
        (trail_f if cls == "trail" else road_f).append(f)
    for rid, r in rails.items():
        t = r["tags"]
        streetcar = t.get("railway") == "tram" or "streetcar" in (t.get("name") or "").lower()
        f = feature(to_lines(r["coords"]), {
            "id": rid, "name": t.get("name"),
            "line": "Tempe Streetcar" if streetcar else "Valley Metro Rail",
        })
        if f:
            rail_f.append(f)

    def fc(feats):
        return {"type": "FeatureCollection", "features": feats}

    return {
        "canals": fc(canal_f), "roads": fc(road_f), "trails": fc(trail_f),
        "openings": fc(openings), "rail": fc(rail_f), "rail_crossings": fc(rail_xings),
    }


WEB_DIR = os.path.join(HERE, "docs")  # served by GitHub Pages
PWA_HEAD = """<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/png" sizes="192x192" href="icon-192.png">
<link rel="apple-touch-icon" href="icon-180.png">
<script>window.VCR_PWA = true;</script>"""
LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/"


def data_from_html(path):
    """Pull the embedded data back out of a previously built page."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("const DATA = ") and not line.startswith("const DATA = /*"):
                return json.loads(line[len("const DATA = "):].rstrip().rstrip(";").replace("<\/", "</"))
    sys.exit(f"No embedded data found in {path}; run a full build instead.")


def render(data, pwa=False):
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\/")
    html = html.replace("/*__DATA__*/null", blob)
    if pwa:
        html = html.replace("<!--__PWA_HEAD__-->", PWA_HEAD)
    return html


def make_icon(size):
    """App icon: a canal with its bank road and a red street opening."""
    from PIL import Image, ImageDraw
    k = 4
    S_ = size * k
    img = Image.new("RGB", (S_, S_), "#0f3d63")
    d = ImageDraw.Draw(img)
    u = S_ / 100

    def curve(off):
        return [(x * u, (58 + off - 0.0028 * (x - 50) ** 2 * 6 + (x - 50) * 0.35) * u) for x in range(-5, 106, 2)]

    d.line(curve(0), fill="#3b9cf0", width=int(13 * u), joint="curve")
    d.line(curve(-12), fill="#f59e0b", width=int(3.2 * u), joint="curve")
    d.line([(68 * u, -5 * u), (60 * u, 105 * u)], fill="#d7dde3", width=int(5 * u))
    cx, cy = 64.2 * u, 47.6 * u
    r = 8.5 * u
    d.ellipse([cx - r - 2.2 * u, cy - r - 2.2 * u, cx + r + 2.2 * u, cy + r + 2.2 * u], fill="#ffffff")
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#e02424")
    return img.resize((size, size), Image.LANCZOS)


def write_web(data):
    os.makedirs(WEB_DIR, exist_ok=True)
    open(os.path.join(WEB_DIR, ".nojekyll"), "w").close()  # serve files as-is on GitHub Pages
    with open(os.path.join(WEB_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(data, pwa=True))
    for n in (180, 192, 512):
        make_icon(n).save(os.path.join(WEB_DIR, f"icon-{n}.png"), optimize=True)
    manifest = {
        "name": "Valley Canal Roads",
        "short_name": "Canal Roads",
        "description": "Phoenix-metro canals, canal-bank roads, canal trails and street openings.",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": "#16191d",
        "theme_color": "#16191d",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    with open(os.path.join(WEB_DIR, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    version = time.strftime("%Y%m%d%H%M%S")
    sw = SW_TEMPLATE.replace("__VERSION__", version).replace("__LEAFLET__", LEAFLET)
    with open(os.path.join(WEB_DIR, "sw.js"), "w", encoding="utf-8") as f:
        f.write(sw)
    log(f"Wrote web app to {WEB_DIR}")


# Caches the page, its icons and Leaflet so the app opens offline.
# Basemap tiles are left to the browser's normal HTTP cache.
SW_TEMPLATE = """const CACHE = "valley-canal-roads-__VERSION__";
const SHELL = ["./", "manifest.webmanifest", "icon-192.png", "icon-512.png", "icon-180.png",
  "__LEAFLET__leaflet.min.js", "__LEAFLET__leaflet.min.css"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.origin === location.origin) {
    // Network first so a rebuilt map shows up, cached copy when offline.
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }).then(r => r || caches.match("./"))));
  } else if (url.href.startsWith("__LEAFLET__")) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", default=DEFAULT_PBF, help="path to Arizona .osm.pbf (downloaded if missing)")
    ap.add_argument("--keep-pbf", action="store_true", help="don't delete the .pbf afterwards")
    ap.add_argument("--html-only", action="store_true",
                    help="re-render the page from the data already in the output file (after editing the template)")
    ap.add_argument("--out", default=OUTPUT)
    args = ap.parse_args()

    if args.html_only:
        data = data_from_html(args.out)
    else:
        if not os.path.exists(args.pbf):
            download(args.pbf)
        extract_date = pbf_timestamp(args.pbf)

        log(f"Reading {args.pbf}")
        ways, barriers, rail_xing, signals = read_pbf(args.pbf)
        canals, rails, canal_ways, connectors, openings = analyse(ways, barriers)
        hws = {i: w for i, w in ways.items() if w["kind"] == "hw"}
        rail_xings = analyse_rail(rails, hws, rail_xing, signals)
        data = build_geojson(canals, rails, canal_ways, connectors, openings, rail_xings)
        data["meta"] = {
            "built": date.today().isoformat(),
            "extract": extract_date,
            "source": PBF_URL,
            "bbox": [S, W, N, E],
            "params": {"along_m": ALONG_DIST_M, "along_frac": ALONG_FRACTION,
                       "min_len_m": MIN_LENGTH_M, "barrier_walk_m": BARRIER_WALK_M,
                       "connector_max_m": CONNECTOR_MAX_M, "rail_cluster_m": RAIL_CLUSTER_M,
                       "rail_parallel_m": RAIL_PARALLEL_M},
        }

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render(data))
    log(f"Wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")
    write_web(data)

    if not args.html_only and not args.keep_pbf and args.pbf == DEFAULT_PBF:
        os.remove(args.pbf)
        log("Deleted the .pbf (use --keep-pbf to keep it)")


if __name__ == "__main__":
    sys.exit(main())
