#!/usr/bin/env python3
"""Step 2: live-music-venue accessibility by ward, Cape Town (OSMnx + scipy).

For a grid of origin points inside every ward, compute door-to-door travel time to
every live music venue by three modes:

  walk     walking only
  transit  walking + buses (bus stops GeoJSON + bus routes GeoJSON)
  drive    car, using OSM maxspeed limits on the road network

Bus ride times are NOT taken from a timetable: each route line is cut into stop-to-stop
segments and every few metres of the segment is matched (OSMnx nearest_edges) to the road
underneath it, using that road's speed limit (OSM `maxspeed`, imputed by road class in
OSMnx where untagged). Where no road is within tolerance (e.g. a busway missing from the
drive network) a fallback bus speed is used.

Inputs   drive.osm / walk.osm (from prepare_osm.py), wards shapefile, stops.geojson,
         routes.geojson, venues (GeoJSON / shapefile / CSV with lon,lat).
Outputs  (in --out) wards_accessibility.gpkg + .geojson + .csv, origins_accessibility.gpkg,
         bus_segments.gpkg (QA of speed-limit ride times), accessibility_map.png

Example
    python venue_access.py --drive-xml osm_prepared/drive.osm --walk-xml osm_prepared/walk.osm \\
        --wards wards.shp --stops stops.geojson --routes routes.geojson \\
        --venues venues.geojson --out results
"""
from __future__ import annotations

import argparse
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import MultiLineString
from shapely.ops import linemerge, substring

import accesscore as core

log = logging.getLogger("venue_access")

# Fallback speed limits (km/h) for roads with no usable `maxspeed` tag. Roughly South
# African defaults (urban 60, freeways 120). OSM `maxspeed` always wins where present.
SA_HWY_SPEEDS = {
    "motorway": 120, "motorway_link": 80,
    "trunk": 100, "trunk_link": 70,
    "primary": 80, "primary_link": 60,
    "secondary": 60, "secondary_link": 50,
    "tertiary": 60, "tertiary_link": 50,
    "unclassified": 50, "residential": 50,
    "living_street": 20, "service": 30, "busway": 60,
}


@dataclass
class Config:
    crs: int = 32734                 # UTM 34S, metric, covers Cape Town
    walk_kph: float = 5.0
    # drive network speeds
    hwy_speeds: dict = field(default_factory=lambda: dict(SA_HWY_SPEEDS))
    fallback_kph: float = 50.0
    # buses
    bus_wait_s: float = 300.0        # expected wait when boarding (~half of a 10 min headway)
    bus_dwell_s: float = 20.0        # dwell at every stop
    bus_max_kph: float = 80.0        # cap on speed limit a bus is assumed to use
    bus_speed_factor: float = 1.0    # <1 slows buses below the speed limit (traffic, signals)
    bus_fallback_kph: float = 30.0   # when no road is matched under the route
    stop_route_tol_m: float = 40.0   # stop belongs to route if within this distance of the line
    min_stop_spacing_m: float = 20.0
    densify_m: float = 25.0          # sampling step along route when reading speed limits
    edge_match_tol_m: float = 35.0   # route sample -> road edge match distance
    stop_snap_warn_m: float = 150.0
    # origins / venues
    origin_spacing_m: float = 500.0
    origin_snap_m: float = 750.0     # origin must be within this of the network
    venue_snap_m: float = 500.0
    max_minutes: float = 120.0
    thresholds: tuple = (15, 30, 45, 60)

    @property
    def walk_mps(self) -> float:
        return self.walk_kph / 3.6


# ----------------------------------------------------------------------------- input helpers
def pick_field(df, candidates, override=None, required=False, what="field"):
    cols = {c.lower(): c for c in df.columns}
    if override:
        if override not in df.columns:
            raise SystemExit(f"{what} '{override}' not in columns: {list(df.columns)}")
        return override
    for c in candidates:
        if c.lower() in cols:
            return cols[c.lower()]
    if required:
        raise SystemExit(f"Could not find a {what}; pass it explicitly. Columns: {list(df.columns)}")
    return None


def fix_crs(g, force_crs=None, swap_xy=False) -> gpd.GeoDataFrame:
    """Drop empty geometries, then apply a CRS override and/or an x<->y swap (lat/lon mix-ups)."""
    g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
    if force_crs:
        g = g.set_crs(force_crs, allow_override=True)
    elif g.crs is None:
        g = g.set_crs(4326)
    if swap_xy:
        swapped = [shapely.transform(geom, lambda c: np.ascontiguousarray(c[:, ::-1])) for geom in g.geometry]
        g = g.set_geometry(gpd.GeoSeries(swapped, index=g.index, crs=g.crs))
    return g


def check_extent(name, g, wards, pad=10_000.0) -> None:
    """Stop early if a layer is nowhere near the wards (wrong CRS / swapped lat-lon)."""
    minx, miny, maxx, maxy = wards.total_bounds
    b = g.geometry.bounds
    ok = ((b.maxx >= minx - pad) & (b.minx <= maxx + pad) &
          (b.maxy >= miny - pad) & (b.miny <= maxy + pad))
    frac = float(ok.mean()) if len(ok) else 0.0
    ll = g.to_crs(4326).total_bounds
    log.info("%s: %.0f%% of features near the wards; lon/lat bounds %.3f,%.3f to %.3f,%.3f",
             name, 100 * frac, ll[0], ll[1], ll[2], ll[3])
    if frac < 0.5:
        raise SystemExit(
            f"\n{name}: only {100 * frac:.0f}% of features are near the wards. Cape Town is about lon 18.3-19.0, "
            f"lat -34.4 to -33.4, but this layer reads as lon/lat bounds {ll.round(3).tolist()}.\n"
            f"Fix: if the file is in another CRS pass --{name}-crs EPSG:<code> (e.g. 2048 for Lo19); "
            f"if lat and lon are swapped pass --swap-xy {name}.")


def read_points(path, crs, lon_col=None, lat_col=None, force_crs=None, swap_xy=False) -> gpd.GeoDataFrame:
    """Points from GeoJSON/shapefile/GPKG, or CSV with lon/lat columns. Polygons -> interior point."""
    p = Path(path)
    if p.suffix.lower() in (".csv", ".txt"):
        df = pd.read_csv(p)
        lon = pick_field(df, ["lon", "lng", "long", "longitude", "x"], lon_col, True, "longitude column")
        lat = pick_field(df, ["lat", "latitude", "y"], lat_col, True, "latitude column")
        g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[lon], df[lat]), crs=4326)
    else:
        g = gpd.read_file(p)
    g = fix_crs(g, force_crs, swap_xy).to_crs(crs).reset_index(drop=True)
    g["geometry"] = [geom if geom.geom_type == "Point" else geom.representative_point()
                     for geom in g.geometry]
    return g


def read_routes(path, crs, route_id_field=None, force_crs=None, swap_xy=False) -> gpd.GeoDataFrame:
    """Route lines; MultiLineStrings are merged, and split only if they stay disconnected."""
    g = fix_crs(gpd.read_file(path), force_crs, swap_xy).to_crs(crs).reset_index(drop=True)
    rid = pick_field(g, ["route_id", "route_short_name", "ref", "route", "name", "id"], route_id_field)
    rows = []
    for i, row in g.iterrows():
        geom = row.geometry
        if isinstance(geom, MultiLineString):
            geom = linemerge(geom)
        parts = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for k, part in enumerate(parts):
            if part.geom_type != "LineString" or part.length == 0:
                continue
            label = str(row[rid]) if rid else f"route{i}"
            rows.append({"route": label, "feature": i, "part": k, "geometry": part})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)


def read_wards(path, crs, ward_id_field, ward_name_field, query, set_crs) -> gpd.GeoDataFrame:
    w = gpd.read_file(path)
    if w.crs is None:
        if not set_crs:
            raise SystemExit("Wards file has no CRS (.prj missing?). Pass --wards-crs, e.g. 4326.")
        w = w.set_crs(set_crs)
    if query:
        w = w.query(query)
    w = w[w.geometry.notna() & ~w.geometry.is_empty].to_crs(crs).reset_index(drop=True)
    idf = pick_field(w, ["WardID", "WARD_ID", "WardNo", "WARDNO", "WARD", "Ward", "ward_id"], ward_id_field)
    namef = pick_field(w, ["WardName", "WARDNAME", "name", "Name"], ward_name_field)
    w["ward_key"] = np.arange(len(w))
    w["ward_id"] = w[idf].astype(str) if idf else w["ward_key"].astype(str)
    w["ward_name"] = w[namef].astype(str) if namef else ""
    return w[["ward_key", "ward_id", "ward_name", "geometry"]]


# ----------------------------------------------------------------------------- OSMnx graphs
def load_drive_graph(path, cfg: Config):
    """Drive network with speed_kph (OSM maxspeed, else SA class defaults) and travel_time (s)."""
    G = ox.graph_from_xml(path, bidirectional=False, simplify=True, retain_all=False)
    G = ox.routing.add_edge_speeds(G, hwy_speeds=cfg.hwy_speeds, fallback=cfg.fallback_kph)
    G = ox.routing.add_edge_travel_times(G)
    return ox.project_graph(G, to_crs=f"EPSG:{cfg.crs}")


def load_walk_graph(path, cfg: Config):
    """Walk network; every way is walkable in both directions."""
    G = ox.graph_from_xml(path, bidirectional=True, simplify=True, retain_all=False)
    return ox.project_graph(G, to_crs=f"EPSG:{cfg.crs}")


def graph_arrays(G, attr):
    """Node coordinates and (src, dst, weight) edge arrays with 0..n-1 node indices."""
    node_ids = np.fromiter(G.nodes, dtype=np.int64, count=G.number_of_nodes())
    xy = np.array([(d["x"], d["y"]) for _, d in G.nodes(data=True)], dtype=float)
    index = pd.Index(node_ids)
    src, dst, w = zip(*G.edges(data=attr))
    w = np.array([np.nan if v is None else v for v in w], dtype=float)
    return xy, index.get_indexer(src), index.get_indexer(dst), w


# ----------------------------------------------------------------------------- buses
def assign_stops_to_routes(routes, stops, cfg: Config):
    """Per route: positional stop indices + chainage (m along line), ordered along the route."""
    sindex = stops.sindex
    assigned = []
    for ridx, row in routes.iterrows():
        line = row.geometry
        cand = sindex.query(line.buffer(cfg.stop_route_tol_m), predicate="intersects")
        if len(cand) < 2:
            assigned.append(None)
            continue
        pts = stops.geometry.iloc[cand]
        ch = np.array([line.project(p) for p in pts])
        dist = np.array([line.distance(p) for p in pts])
        order = np.argsort(ch)
        keep, group = [], [order[0]]
        for k in order[1:]:
            if ch[k] - ch[group[-1]] < cfg.min_stop_spacing_m:
                group.append(k)
            else:
                keep.append(min(group, key=lambda g: dist[g]))
                group = [k]
        keep.append(min(group, key=lambda g: dist[g]))
        assigned.append((np.asarray(cand)[keep], ch[keep]) if len(keep) >= 2 else None)
    return assigned


def bus_segment_times(routes, assigned, G_drive, cfg: Config):
    """Ride seconds between consecutive stops, from the speed limit under the route.

    Returns {route index: ride_s array} and a GeoDataFrame of segments for QA.
    """
    seg_meta, xs, ys, steps, seg_ids = [], [], [], [], []
    for ridx, a in enumerate(assigned):
        if a is None:
            continue
        line = routes.geometry.iloc[ridx]
        _, ch = a
        for i in range(len(ch) - 1):
            d0, d1 = ch[i], ch[i + 1]
            n = max(1, math.ceil((d1 - d0) / cfg.densify_m))
            edges = np.linspace(d0, d1, n + 1)
            mids = (edges[:-1] + edges[1:]) / 2.0
            pts = shapely.line_interpolate_point(line, mids)
            sid = len(seg_meta)
            seg_meta.append((ridx, i, d0, d1))
            xs.append(shapely.get_x(pts))
            ys.append(shapely.get_y(pts))
            steps.append(np.diff(edges))
            seg_ids.append(np.full(n, sid))
    if not seg_meta:
        raise SystemExit("No route has >= 2 stops within tolerance. Check stops/routes CRS and --stop-route-tol.")

    X, Y = np.concatenate(xs), np.concatenate(ys)
    step, sid = np.concatenate(steps), np.concatenate(seg_ids)

    log.info("matching %d route samples to road edges", len(X))
    ne, dist = ox.nearest_edges(G_drive, X, Y, return_dist=True)
    # OSMnx returns (u, v, key) tuples (an object array in v2); accept a plain (n, 3) array too
    ne = np.array([tuple(t) for t in ne], dtype=np.int64).reshape(-1, 3)
    edge_speed = ox.convert.graph_to_gdfs(G_drive, nodes=False)["speed_kph"]
    idx = pd.MultiIndex.from_arrays([ne[:, 0], ne[:, 1], ne[:, 2]])
    limit = edge_speed.reindex(idx).to_numpy(dtype=float)

    matched = (np.asarray(dist) <= cfg.edge_match_tol_m) & np.isfinite(limit)
    kph = np.where(matched, np.minimum(limit, cfg.bus_max_kph) * cfg.bus_speed_factor, cfg.bus_fallback_kph)
    seconds = step / (kph / 3.6)

    nseg = len(seg_meta)
    ride_s = np.bincount(sid, weights=seconds, minlength=nseg)
    seg_len = np.bincount(sid, weights=step, minlength=nseg)
    matched_len = np.bincount(sid, weights=step * matched, minlength=nseg)

    rows = []
    per_route: dict[int, list] = {}
    for s, (ridx, i, d0, d1) in enumerate(seg_meta):
        per_route.setdefault(ridx, []).append(ride_s[s])
        rows.append({"route": routes["route"].iloc[ridx], "feature": int(routes["feature"].iloc[ridx]),
                     "seg_no": i, "length_m": seg_len[s], "ride_s": ride_s[s],
                     "mean_kph": seg_len[s] / ride_s[s] * 3.6 if ride_s[s] else np.nan,
                     "share_on_road": matched_len[s] / seg_len[s] if seg_len[s] else np.nan,
                     "geometry": substring(routes.geometry.iloc[ridx], d0, d1)})
    segs = gpd.GeoDataFrame(rows, geometry="geometry", crs=routes.crs)
    return {k: np.array(v) for k, v in per_route.items()}, segs


# ----------------------------------------------------------------------------- origins
def make_origins(wards: gpd.GeoDataFrame, spacing: float) -> pd.DataFrame:
    """Regular grid of origin points inside each ward (interior point if the grid misses)."""
    frames = []
    for key, geom in zip(wards["ward_key"], wards.geometry):
        minx, miny, maxx, maxy = geom.bounds
        gx, gy = np.meshgrid(np.arange(minx + spacing / 2, maxx, spacing),
                             np.arange(miny + spacing / 2, maxy, spacing))
        gx, gy = gx.ravel(), gy.ravel()
        inside = shapely.contains_xy(geom, gx, gy)
        pts = np.c_[gx[inside], gy[inside]]
        if len(pts) == 0:
            rp = geom.representative_point()
            pts = np.array([[rp.x, rp.y]])
        frames.append(pd.DataFrame({"ward_key": key, "x": pts[:, 0], "y": pts[:, 1]}))
    return pd.concat(frames, ignore_index=True)


def snap(tree: cKDTree, xy):
    d, i = tree.query(xy)
    return i.astype(np.int64), d


def analyse_mode(name, csr, tree, o_xy, v_xy, cfg: Config):
    o_node, o_d = snap(tree, o_xy)
    v_node, v_d = snap(tree, v_xy)
    o_acc = np.where(o_d <= cfg.origin_snap_m, o_d / cfg.walk_mps, np.inf)
    v_acc = np.where(v_d <= cfg.venue_snap_m, v_d / cfg.walk_mps, np.inf)
    if np.isinf(v_acc).any():
        log.warning("%s: %d venue(s) farther than %.0f m from the network are excluded",
                    name, int(np.isinf(v_acc).sum()), cfg.venue_snap_m)
    t0 = time.time()
    minutes = core.od_minutes(csr, o_node, o_acc, v_node, v_acc, cfg.max_minutes)
    log.info("%s: %d origins x %d venues in %.1fs", name, *minutes.shape, time.time() - t0)
    metrics = core.origin_metrics(minutes, np.isfinite(o_acc), cfg.thresholds)
    metrics.columns = [f"{name}_{c}" for c in metrics.columns]
    return metrics


def export_roads(G, path) -> None:
    """Drive network with the speed used for each edge, for inspection in QGIS / the web map."""
    e = ox.convert.graph_to_gdfs(G, nodes=False).reset_index(drop=True)
    highway = e["highway"].map(lambda v: v[0] if isinstance(v, list) else v).astype(str)
    tagged = e["maxspeed"].notna() if "maxspeed" in e.columns else pd.Series(False, index=e.index)
    roads = gpd.GeoDataFrame({"highway": highway, "speed_kph": e["speed_kph"].round(0),
                              "tagged": tagged.astype(bool), "length_m": e["length"].round(0)},
                             geometry=e.geometry, crs=e.crs)
    roads.to_file(path, layer="drive_roads", driver="GPKG")
    log.info("wrote %d road edges to %s", len(roads), path)


# ----------------------------------------------------------------------------- plotting
# Single-hue sequential ramp (light = short trip, dark = long trip); validated default palette.
BLUE_RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
             "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
SURFACE, INK, INK2, VENUE = "#fcfcfb", "#0b0b0b", "#52514e", "#eb6834"


def plot_maps(wards_out, venues, stops, routes, path, cfg: Config):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.lines import Line2D

    cmap = LinearSegmentedColormap.from_list("seq_blue", BLUE_RAMP)
    panels = [("walk", "Walking"), ("transit", "Walk + bus"), ("drive", "Driving (speed limits)")]
    cols = [f"{m}_nearest_median" for m, _ in panels]
    vmax = float(np.nanpercentile(wards_out[cols].to_numpy(dtype=float), 98))
    vmax = vmax if np.isfinite(vmax) and vmax > 1.0 else 60.0

    fig, axes = plt.subplots(1, 3, figsize=(18, 7), facecolor=SURFACE)
    for ax, (mode, title), col in zip(axes, panels, cols):
        ax.set_facecolor(SURFACE)
        wards_out.plot(ax=ax, column=col, cmap=cmap, vmin=0, vmax=vmax, edgecolor=SURFACE,
                       linewidth=0.4, missing_kwds={"color": "#d4d3ce"})
        if mode == "transit":
            routes.plot(ax=ax, color=INK, linewidth=0.5, alpha=0.6)
            stops.plot(ax=ax, color=INK, markersize=1.5)
        venues.plot(ax=ax, color=VENUE, edgecolor=SURFACE, linewidth=0.8, markersize=34, zorder=5)
        ax.set_title(title, color=INK, fontsize=14, loc="left")
        ax.set_axis_off()

    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, vmax))
    cbar = fig.colorbar(sm, ax=axes, orientation="horizontal", fraction=0.04, pad=0.03, aspect=50)
    cbar.set_label("Median minutes from a ward to its nearest live music venue "
                   f"(scale capped at the 98th percentile, {vmax:.0f} min)", color=INK2)
    cbar.outline.set_visible(False)
    cbar.ax.tick_params(colors=INK2)
    handles = [Line2D([], [], marker="o", ls="", color=VENUE, markeredgecolor=SURFACE, markersize=8,
                      label="Live music venue"),
               Line2D([], [], color=INK, lw=1, alpha=0.6, label="Bus route (transit panel)"),
               Line2D([], [], marker="s", ls="", color="#d4d3ce", markersize=9, label="No venue reachable")]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.93), frameon=False,
               labelcolor=INK2, ncol=3)
    fig.savefig(path, dpi=200, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------- main
def run(a) -> None:
    cfg = Config(
        crs=a.crs, walk_kph=a.walk_kph, fallback_kph=a.fallback_kph, bus_wait_s=a.bus_wait_s,
        bus_dwell_s=a.bus_dwell_s, bus_max_kph=a.bus_max_kph, bus_speed_factor=a.bus_speed_factor,
        bus_fallback_kph=a.bus_fallback_kph, stop_route_tol_m=a.stop_route_tol,
        origin_spacing_m=a.origin_spacing, origin_snap_m=a.origin_snap, max_minutes=a.max_minutes,
        thresholds=tuple(a.thresholds))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    log.info("reading vector inputs")
    wards = read_wards(a.wards, cfg.crs, a.ward_id_field, a.ward_name_field, a.ward_query, a.wards_crs)
    swap = set(a.swap_xy or [])
    venues = read_points(a.venues, cfg.crs, a.venue_lon, a.venue_lat, a.venues_crs, "venues" in swap)
    stops = read_points(a.stops, cfg.crs, force_crs=a.stops_crs, swap_xy="stops" in swap)
    routes = read_routes(a.routes, cfg.crs, a.route_id_field, a.routes_crs, "routes" in swap)
    for name, layer in (("venues", venues), ("stops", stops), ("routes", routes)):
        check_extent(name, layer, wards)
    vname = pick_field(venues, ["name", "venue", "venue_name", "title"], a.venue_name_field)
    venues["venue_name"] = venues[vname].astype(str) if vname else [f"venue_{i}" for i in range(len(venues))]
    log.info("%d wards, %d venues, %d stops, %d route lines", len(wards), len(venues), len(stops), len(routes))

    log.info("loading OSM graphs (this is the slow part for a whole metro)")
    G_drive = load_drive_graph(a.drive_xml, cfg)
    G_walk = load_walk_graph(a.walk_xml, cfg)
    w_xy, w_src, w_dst, w_len = graph_arrays(G_walk, "length")
    d_xy, d_src, d_dst, d_tt = graph_arrays(G_drive, "travel_time")
    log.info("walk graph: %d nodes; drive graph: %d nodes", len(w_xy), len(d_xy))
    w_time = w_len / cfg.walk_mps
    n_walk = len(w_xy)

    # --- buses: stops -> routes, speed-limit ride times, stops -> walk nodes
    assigned = assign_stops_to_routes(routes, stops, cfg)
    ride_by_route, segments = bus_segment_times(routes, assigned, G_drive, cfg)
    walk_tree = cKDTree(w_xy)
    bus_routes = []
    stops_xy = np.c_[stops.geometry.x, stops.geometry.y]
    far_stops = 0
    for ridx, a_ in enumerate(assigned):
        if a_ is None or ridx not in ride_by_route:
            continue
        stop_idx, _ = a_
        node, dist = snap(walk_tree, stops_xy[stop_idx])
        far_stops += int((dist > cfg.stop_snap_warn_m).sum())
        bus_routes.append({"nodes": node, "access_s": dist / cfg.walk_mps, "ride_s": ride_by_route[ridx]})
    if far_stops:
        log.warning("%d stop-route pairs are > %.0f m from the walk network", far_stops, cfg.stop_snap_warn_m)
    log.info("%d of %d route lines have >= 2 stops and are in the transit network",
             len(bus_routes), len(routes))

    walk_csr = core.edges_to_csr(w_src, w_dst, w_time, n_walk)
    transit_csr = core.build_transit_csr(w_src, w_dst, w_time, n_walk, bus_routes,
                                         cfg.bus_wait_s, cfg.bus_dwell_s, two_way=a.two_way_routes)
    drive_csr = core.edges_to_csr(d_src, d_dst, d_tt, len(d_xy))
    drive_tree = cKDTree(d_xy)

    # --- origins and per-mode analysis
    origins = make_origins(wards, cfg.origin_spacing_m)
    log.info("%d origin points", len(origins))
    o_xy = origins[["x", "y"]].to_numpy()
    v_xy = np.c_[venues.geometry.x, venues.geometry.y]
    parts = [analyse_mode("walk", walk_csr, walk_tree, o_xy, v_xy, cfg),
             analyse_mode("transit", transit_csr, walk_tree, o_xy, v_xy, cfg),
             analyse_mode("drive", drive_csr, drive_tree, o_xy, v_xy, cfg)]
    origins = pd.concat([origins] + parts, axis=1)
    origins["transit_saving_min"] = origins["walk_nearest_min"] - origins["transit_nearest_min"]
    for m in ("walk", "transit", "drive"):
        i = origins[f"{m}_nearest_idx"].to_numpy()
        origins[f"{m}_nearest_venue"] = np.where(i >= 0, venues["venue_name"].to_numpy()[np.clip(i, 0, None)], "")
        origins = origins.drop(columns=[f"{m}_nearest_idx"])

    # --- ward summary
    modes = ("walk", "transit", "drive")
    wm = core.ward_metrics(origins, "ward_key", modes, cfg.thresholds)
    wm["transit_saving_median_min"] = origins.groupby("ward_key")["transit_saving_min"].median()
    wm["pct_origins_bus_helps"] = origins.groupby("ward_key")["transit_saving_min"].apply(
        lambda s: np.nan if s.notna().sum() == 0 else 100.0 * (s.dropna() > 1.0).mean())
    wards_out = wards.merge(wm, left_on="ward_key", right_index=True, how="left")
    in_ward = gpd.sjoin(venues[["geometry"]], wards[["ward_key", "geometry"]], predicate="within")
    wards_out["venues_in_ward"] = wards_out["ward_key"].map(in_ward["ward_key"].value_counts()).fillna(0).astype(int)

    # --- write
    wards_out.to_file(out / "wards_accessibility.gpkg", layer="wards", driver="GPKG")
    wards_out.to_crs(4326).to_file(out / "wards_accessibility.geojson", driver="GeoJSON")
    pd.DataFrame(wards_out.drop(columns="geometry")).to_csv(out / "wards_accessibility.csv", index=False)
    gpd.GeoDataFrame(origins, geometry=gpd.points_from_xy(origins.x, origins.y), crs=cfg.crs) \
        .to_file(out / "origins_accessibility.gpkg", layer="origins", driver="GPKG")
    segments.to_file(out / "bus_segments.gpkg", layer="bus_segments", driver="GPKG")
    # cleaned (reprojected, swap-fixed) inputs, used by make_web_map.py
    clean = out / "inputs_clean.gpkg"
    clean.unlink(missing_ok=True)
    venues.to_file(clean, layer="venues", driver="GPKG")
    stops.to_file(clean, layer="stops", driver="GPKG")
    routes.to_file(clean, layer="routes", driver="GPKG")
    if a.export_roads:
        export_roads(G_drive, out / "drive_roads.gpkg")
    plot_maps(wards_out, venues, stops, routes, out / "accessibility_map.png", cfg)
    log.info("done -> %s", out.resolve())


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("inputs")
    g.add_argument("--drive-xml", required=True, help="drive.osm from prepare_osm.py")
    g.add_argument("--walk-xml", required=True, help="walk.osm from prepare_osm.py")
    g.add_argument("--wards", required=True, help="wards shapefile (basemap + analysis units)")
    g.add_argument("--stops", required=True, help="bus stops GeoJSON (points)")
    g.add_argument("--routes", required=True, help="bus routes GeoJSON (lines)")
    g.add_argument("--venues", required=True, help="live music venues: GeoJSON/shp/gpkg or CSV with lon,lat")
    g.add_argument("--out", default="results")
    g.add_argument("--export-roads", action="store_true",
                   help="also write drive_roads.gpkg (speed used on every road) for the interactive map")
    f = ap.add_argument_group("field names / filters (auto-detected when omitted)")
    f.add_argument("--ward-id-field")
    f.add_argument("--ward-name-field")
    f.add_argument("--ward-query", help='pandas query to keep Cape Town wards, e.g. \'MunicName == "City of Cape Town"\'')
    f.add_argument("--wards-crs", help="EPSG code to assume if the wards file has no .prj")
    f.add_argument("--route-id-field")
    f.add_argument("--venue-name-field")
    f.add_argument("--venue-lon")
    f.add_argument("--venue-lat")
    f.add_argument("--venues-crs", help="force this CRS on the venues file, ignoring what it declares (e.g. 2048)")
    f.add_argument("--stops-crs", help="force this CRS on the stops file")
    f.add_argument("--routes-crs", help="force this CRS on the routes file")
    f.add_argument("--swap-xy", nargs="+", choices=["venues", "stops", "routes"],
                   help="swap x and y (lat/lon mix-up) on these layers")
    m = ap.add_argument_group("model parameters")
    m.add_argument("--crs", type=int, default=32734, help="projected CRS for all distances (default UTM 34S)")
    m.add_argument("--walk-kph", type=float, default=5.0)
    m.add_argument("--fallback-kph", type=float, default=50.0, help="drive speed if road class unknown")
    m.add_argument("--bus-wait-s", type=float, default=300.0, help="expected wait per boarding (s)")
    m.add_argument("--bus-dwell-s", type=float, default=20.0, help="dwell per stop (s)")
    m.add_argument("--bus-max-kph", type=float, default=80.0)
    m.add_argument("--bus-speed-factor", type=float, default=1.0, help="e.g. 0.7 = buses average 70%% of the limit")
    m.add_argument("--bus-fallback-kph", type=float, default=30.0)
    m.add_argument("--stop-route-tol", type=float, default=40.0, help="max stop-to-route-line distance (m)")
    m.add_argument("--two-way-routes", action="store_true",
                   help="allow riding each route line in both directions (default: line direction only)")
    m.add_argument("--origin-spacing", type=float, default=500.0, help="origin grid spacing inside wards (m)")
    m.add_argument("--origin-snap", type=float, default=750.0)
    m.add_argument("--max-minutes", type=float, default=120.0)
    m.add_argument("--thresholds", type=int, nargs="+", default=[15, 30, 45, 60])
    return ap.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run(parse_args())
