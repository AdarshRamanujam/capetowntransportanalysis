#!/usr/bin/env python3
"""Step 3: interactive map of the results (single HTML file, Leaflet).

Reads the folder written by venue_access.py and writes <results>/interactive_map.html.

    python make_web_map.py results              # write the map
    python make_web_map.py results --serve      # write it, then open it at http://localhost:8000

What you can do in the map
  * switch mode (walk / walk + bus / drive) and ward metric (median, mean, 90th percentile,
    venues reachable, % of locations within T minutes, minutes saved by bus)
  * hover/click a ward for every metric; click a sample point for its times and straight lines
    to the nearest venue by each mode
  * show bus segments coloured by the speed that came from the road speed limits (hover for
    length, ride time, share of the segment matched to a road)
  * show the drive network coloured by speed limit (run venue_access.py with --export-roads)

Opening the HTML by double-click works, but some tile servers refuse requests from file:// pages;
--serve avoids that.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# --------------------------------------------------------------------------- HTML template
HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live music venue accessibility - Cape Town</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  html, body { height: 100%; margin: 0; }
  body { font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color: #0b0b0b; }
  #map { position: absolute; inset: 0; background: #fcfcfb; }
  #panel { position: absolute; top: 10px; right: 10px; z-index: 1000; width: 300px;
           max-height: calc(100% - 20px); overflow: auto; box-sizing: border-box;
           background: rgba(252,252,251,0.96); border-radius: 8px; padding: 12px 14px;
           box-shadow: 0 1px 8px rgba(0,0,0,0.25); }
  #panel h1 { font-size: 15px; margin: 0 0 2px; }
  .dim { color: #52514e; }
  fieldset { border: 0; margin: 10px 0 0; padding: 0; }
  legend { font-weight: 600; padding: 0; margin-bottom: 3px; }
  label { display: block; cursor: pointer; padding: 1px 0; }
  select { width: 100%; margin-top: 3px; padding: 3px; }
  .legend { margin-top: 10px; }
  #legend-bar { height: 10px; border-radius: 2px; margin: 4px 0 2px; }
  .scale { display: flex; justify-content: space-between; color: #52514e; font-size: 12px; }
  .swatch { display: inline-block; width: 12px; height: 10px; margin-right: 5px; border-radius: 2px; vertical-align: middle; }
  details { margin-top: 10px; }
  summary { cursor: pointer; font-weight: 600; }
  ul { margin: 4px 0 0; padding-left: 18px; }
  .pop table { border-collapse: collapse; margin-top: 4px; }
  .pop th, .pop td { padding: 2px 8px 2px 0; text-align: right; white-space: nowrap; }
  .pop th:first-child, .pop td:first-child { text-align: left; }
  .pop th { font-weight: 600; border-bottom: 1px solid #ddd; }
  .keyline { display: inline-block; width: 22px; border-top: 3px solid; vertical-align: middle; margin-right: 5px; }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <h1>Live music venue accessibility</h1>
  <div class="dim" id="counts"></div>

  <fieldset><legend>Travel mode</legend><div id="modes"></div></fieldset>

  <fieldset><legend>Ward colour shows</legend><select id="metric"></select></fieldset>
  <div class="legend">
    <div id="legend-title"></div>
    <div id="legend-bar"></div>
    <div class="scale"><span id="legend-min"></span><span id="legend-max"></span></div>
    <div class="dim"><span class="swatch" style="background:#d4d3ce"></span>no data / no venue reachable</div>
  </div>

  <fieldset><legend>Layers</legend><div id="layers"></div></fieldset>

  <div class="legend" id="origin-key" hidden>
    <div>Sample points: minutes to nearest venue (selected mode)</div>
    <div id="origin-bar" style="height:10px;border-radius:2px;margin:4px 0 2px"></div>
    <div class="scale"><span>0</span><span id="origin-max"></span></div>
  </div>
  <div class="legend" id="speed-key" hidden>
    <div>Speed from road speed limit (km/h)</div>
    <div id="speed-swatches"></div>
  </div>

  <details open>
    <summary>How to read this map</summary>
    <ul>
      <li>Wards are coloured by the metric above; darker = longer trip (or more venues / larger share, for those metrics).</li>
      <li>Times are door to door: walk to the network, travel, walk from the network to the venue.</li>
      <li>Bus legs add an expected wait when boarding and a short dwell at each stop.</li>
      <li>Click a sample point to draw lines to its nearest venue by walking (dotted), walk + bus (blue) and driving (dashed).</li>
      <li>Bus segments and roads use OSM speed limits; roads without a tagged limit use a default for their road class.</li>
    </ul>
  </details>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>const DATA = __DATA__;</script>
<script>
(function () {
"use strict";

const RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
              "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];
const MODES = [["walk", "Walking"], ["transit", "Walk + bus"], ["drive", "Driving (speed limits)"]];
const LINKS = { walk: { color: "#52514e", dash: "2 6" },
                transit: { color: "#2a78d6", dash: null },
                drive: { color: "#0b0b0b", dash: "8 6" } };
const NA = "#d4d3ce", INK = "#0b0b0b", VENUE = "#eb6834", SURFACE = "#fcfcfb";
const TH = (DATA.thresholds && DATA.thresholds.length) ? DATA.thresholds : [15, 30, 45, 60];
const state = { mode: "transit", metric: "nearest_median" };

const isNum = v => typeof v === "number" && isFinite(v);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ESC[c]);
const fmt = (v, d) => isNum(v) ? v.toFixed(d == null ? 1 : d) : "–";
const modeLabel = () => MODES.find(m => m[0] === state.mode)[1];

function quantile(vals, q) {
  const a = vals.filter(isNum).sort((x, y) => x - y);
  if (!a.length) return NaN;
  const pos = (a.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos);
  return a[lo] + (a[hi] - a[lo]) * (pos - lo);
}
function hex2rgb(h) { return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)); }
function rampColor(t) {
  if (!isNum(t)) return NA;
  t = Math.max(0, Math.min(1, t));
  const x = t * (RAMP.length - 1), i = Math.min(Math.floor(x), RAMP.length - 2), f = x - i;
  const a = hex2rgb(RAMP[i]), b = hex2rgb(RAMP[i + 1]);
  return "rgb(" + a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",") + ")";
}
const GRADIENT = "linear-gradient(90deg," + RAMP.join(",") + ")";

// ---- speed classes (km/h), shared by bus segments and roads
const SPEED_BREAKS = [20, 30, 40, 50, 60, 80];
const SPEED_COLORS = [RAMP[3], RAMP[5], RAMP[7], RAMP[8], RAMP[10], RAMP[11], RAMP[12]];
function speedColor(v) {
  if (!isNum(v)) return NA;
  const i = SPEED_BREAKS.findIndex(b => v < b);
  return SPEED_COLORS[i === -1 ? SPEED_BREAKS.length : i];
}
function speedLabels() {
  const out = ["< " + SPEED_BREAKS[0]];
  for (let i = 0; i < SPEED_BREAKS.length - 1; i++) out.push(SPEED_BREAKS[i] + "–" + SPEED_BREAKS[i + 1]);
  out.push(SPEED_BREAKS[SPEED_BREAKS.length - 1] + "+");
  return out;
}

// ---- ward metrics
const METRICS = [
  { id: "nearest_median", label: "Median minutes to nearest venue", unit: "min" },
  { id: "nearest_mean", label: "Mean minutes to nearest venue", unit: "min" },
  { id: "nearest_p90", label: "90th-percentile minutes to nearest venue", unit: "min" }
];
TH.forEach(t => {
  METRICS.push({ id: "venues_" + t + "min", label: "Average venues within " + t + " min", unit: "" });
  METRICS.push({ id: "pct_" + t + "min", label: "% of locations within " + t + " min of a venue", unit: "%", max: 100 });
});
METRICS.push({ id: "transit_saving_median_min", label: "Median minutes saved by bus vs walking", unit: "min", modeless: true });

const metricDef = () => METRICS.find(m => m.id === state.metric);
function wardScale() {
  const def = metricDef();
  const col = def.modeless ? def.id : state.mode + "_" + def.id;
  const hi = def.max != null ? def.max : quantile(DATA.wards.features.map(f => f.properties[col]), 0.98);
  return { col: col, lo: 0, hi: (isNum(hi) && hi > 0) ? hi : 1, def: def };
}
function originScale() {
  const col = state.mode + "_nearest_min";
  const hi = quantile(DATA.origins.features.map(f => f.properties[col]), 0.98);
  return { col: col, hi: (isNum(hi) && hi > 0) ? hi : 1 };
}
let SC = wardScale();
let OSC = originScale();

// ---- map and basemaps
const map = L.map("map", { preferCanvas: true });
const carto = L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  { maxZoom: 19, subdomains: "abcd", attribution: "&copy; OpenStreetMap contributors &copy; CARTO" });
const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" });
carto.addTo(map);
L.control.layers({ "Light (CARTO)": carto, "OpenStreetMap": osm, "No basemap": L.layerGroup() },
  null, { position: "bottomleft" }).addTo(map);

// ---- wards
function wardStyle(f) {
  const v = f.properties[SC.col];
  return { fillColor: isNum(v) ? rampColor((v - SC.lo) / (SC.hi - SC.lo)) : NA,
           fillOpacity: isNum(v) ? 0.78 : 0.5, color: SURFACE, weight: 1, opacity: 1 };
}
function wardTooltip(f) {
  const p = f.properties, d = SC.def;
  return "<b>" + esc(p.ward_name || ("Ward " + p.ward_id)) + "</b><br>" + esc(d.label) +
    (d.modeless ? "" : " (" + esc(modeLabel()) + ")") + ": <b>" +
    fmt(p[SC.col], d.unit === "%" ? 0 : 1) + (d.unit ? " " + d.unit : "") + "</b>";
}
function wardPopup(f) {
  const p = f.properties;
  const row = (label, key, d) => "<tr><td>" + label + "</td>" +
    MODES.map(m => "<td>" + fmt(p[m[0] + "_" + key], d) + "</td>").join("") + "</tr>";
  let h = "<div class='pop'><b>" + esc(p.ward_name || "Ward") + "</b> <span class='dim'>(ward " + esc(p.ward_id) + ")</span>";
  h += "<div class='dim'>" + fmt(p.n_origins, 0) + " sample points &middot; " + fmt(p.venues_in_ward, 0) + " venue(s) in ward</div>";
  h += "<table><tr><th></th>" + MODES.map(m => "<th>" + esc(m[1]) + "</th>").join("") + "</tr>";
  h += row("Median min to nearest", "nearest_median", 1) + row("Mean min", "nearest_mean", 1) + row("90th pct min", "nearest_p90", 1);
  TH.forEach(t => {
    h += row("Avg venues within " + t + " min", "venues_" + t + "min", 1);
    h += row("% of points within " + t + " min", "pct_" + t + "min", 0);
  });
  h += "</table><div class='dim'>Median minutes saved by bus vs walking: " + fmt(p.transit_saving_median_min) +
       " &middot; bus saves more than 1 min at " + fmt(p.pct_origins_bus_helps, 0) + "% of points</div></div>";
  return h;
}
const wardLayer = L.geoJSON(DATA.wards, {
  style: wardStyle,
  onEachFeature: (f, layer) => {
    layer.bindTooltip(() => wardTooltip(f), { sticky: true });
    layer.bindPopup(() => wardPopup(f), { maxWidth: 400 });
    layer.on("mouseover", () => layer.setStyle({ weight: 3, color: INK }));
    layer.on("mouseout", () => wardLayer.resetStyle(layer));
  }
});

// ---- venues, stops, routes
const venueByName = {};
DATA.venues.features.forEach(f => {
  venueByName[f.properties.venue_name] = [f.geometry.coordinates[1], f.geometry.coordinates[0]];
});
const venueLayer = L.geoJSON(DATA.venues, {
  pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 7, fillColor: VENUE, fillOpacity: 1, color: SURFACE, weight: 2 }),
  onEachFeature: (f, layer) => layer.bindTooltip(esc(f.properties.venue_name), { direction: "top" })
});
const firstProps = f => Object.values(f.properties).filter(v => v != null && v !== "").slice(0, 2).map(esc).join(" &middot; ");
const stopLayer = L.geoJSON(DATA.stops, {
  pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 2.5, fillColor: INK, fillOpacity: 0.85, weight: 0 }),
  onEachFeature: (f, layer) => layer.bindTooltip(() => firstProps(f), { direction: "top" })
});
const routeLayer = L.geoJSON(DATA.routes, {
  style: () => ({ color: INK, weight: 1.5, opacity: 0.5 }),
  onEachFeature: (f, layer) => layer.bindTooltip(() => "Route " + esc(f.properties.route), { sticky: true })
});

// ---- bus segments (speed-limit based ride times)
function segTip(f) {
  const p = f.properties;
  return "<b>Route " + esc(p.route) + "</b>, segment " + esc(p.seg_no) + "<br>" +
    fmt(p.length_m, 0) + " m &middot; ride " + fmt(p.ride_s, 0) + " s<br>" +
    "speed used: <b>" + fmt(p.mean_kph, 0) + " km/h</b><br>" +
    fmt(p.share_on_road, 0) + "% of segment matched to a road";
}
const segLayer = L.geoJSON(DATA.segments, {
  style: f => ({ color: speedColor(f.properties.mean_kph), weight: 5, opacity: 0.9 }),
  onEachFeature: (f, layer) => layer.bindTooltip(() => segTip(f), { sticky: true })
});

// ---- drive network by speed limit (optional)
let roadLayer = null;
if (DATA.roads && DATA.roads.features && DATA.roads.features.length) {
  const roadWeight = h => (h === "motorway" || h === "trunk") ? 3 : (h === "primary" ? 2.5 : 1.5);
  roadLayer = L.geoJSON(DATA.roads, {
    style: f => ({ color: speedColor(f.properties.speed_kph), weight: roadWeight(f.properties.highway), opacity: 0.85 }),
    onEachFeature: (f, layer) => layer.bindTooltip(() => {
      const p = f.properties;
      return "<b>" + esc(p.highway) + "</b>: " + fmt(p.speed_kph, 0) + " km/h<br>" +
        (p.tagged ? "from OSM maxspeed" : "default for this road class (no maxspeed tag)");
    }, { sticky: true })
  });
}

// ---- sample points
const originStyle = f => {
  const v = f.properties[OSC.col];
  return { radius: 3.5, weight: 0, fillOpacity: isNum(v) ? 0.9 : 0.35, fillColor: isNum(v) ? rampColor(v / OSC.hi) : NA };
};
function originTooltip(f) {
  const p = f.properties;
  return "<b>" + esc(p.ward || "") + "</b><br>" + MODES.map(m => esc(m[1]) + ": " + fmt(p[m[0] + "_nearest_min"]) + " min").join("<br>");
}
function originPopup(f) {
  const p = f.properties;
  let h = "<div class='pop'><b>" + esc(p.ward || "Sample point") + "</b><table><tr><th></th><th>Minutes</th><th>Nearest venue</th></tr>";
  MODES.forEach(m => {
    h += "<tr><td><span class='keyline' style='border-color:" + LINKS[m[0]].color + "'></span>" + esc(m[1]) + "</td><td>" +
      fmt(p[m[0] + "_nearest_min"]) + "</td><td>" + esc(p[m[0] + "_nearest_venue"]) + "</td></tr>";
  });
  h += "</table><div class='dim'>Bus saves " + fmt(p.transit_saving_min) + " min vs walking. Lines on the map are straight lines to each mode's nearest venue, not the route taken.</div></div>";
  return h;
}
const linkLayer = L.featureGroup();
function showLinks(f, ll) {
  linkLayer.clearLayers();
  MODES.forEach(m => {
    const v = venueByName[f.properties[m[0] + "_nearest_venue"]];
    if (!v) return;
    const opts = { color: LINKS[m[0]].color, weight: 2.5, interactive: false };
    if (LINKS[m[0]].dash) opts.dashArray = LINKS[m[0]].dash;
    L.polyline([ll, v], opts).addTo(linkLayer);
  });
  if (!map.hasLayer(linkLayer)) map.addLayer(linkLayer);
  restack();
}
const originLayer = L.geoJSON(DATA.origins, {
  pointToLayer: (f, ll) => L.circleMarker(ll, originStyle(f)),
  onEachFeature: (f, layer) => {
    layer.bindTooltip(() => originTooltip(f), { direction: "top" });
    layer.bindPopup(() => originPopup(f), { maxWidth: 380 });
    layer.on("click", () => showLinks(f, layer.getLatLng()));
  }
});

// ---- stacking order, layer toggles
const ORDER = [wardLayer, roadLayer, routeLayer, segLayer, originLayer, stopLayer, linkLayer, venueLayer];
function restack() { ORDER.forEach(l => { if (l && map.hasLayer(l)) l.bringToFront(); }); }
const LAYERS = [
  { label: "Wards (coloured by metric)", layer: wardLayer, on: true },
  { label: "Drive network by speed limit", layer: roadLayer, on: false },
  { label: "Bus routes", layer: routeLayer, on: true },
  { label: "Bus segments by speed used", layer: segLayer, on: false },
  { label: "Sample points (click for detail)", layer: originLayer, on: false },
  { label: "Bus stops", layer: stopLayer, on: false },
  { label: "Live music venues", layer: venueLayer, on: true }
];
function syncKeys() {
  const speedOn = (map.hasLayer(segLayer)) || (roadLayer && map.hasLayer(roadLayer));
  document.getElementById("speed-key").hidden = !speedOn;
  document.getElementById("origin-key").hidden = !map.hasLayer(originLayer);
}
const layersDiv = document.getElementById("layers");
LAYERS.filter(ly => ly.layer).forEach(ly => {
  const lab = document.createElement("label");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = !!ly.on;
  cb.addEventListener("change", () => {
    if (cb.checked) map.addLayer(ly.layer); else map.removeLayer(ly.layer);
    if (ly.layer === originLayer && !cb.checked) linkLayer.clearLayers();
    restack();
    syncKeys();
  });
  lab.appendChild(cb);
  lab.appendChild(document.createTextNode(" " + ly.label));
  layersDiv.appendChild(lab);
  if (ly.on) ly.layer.addTo(map);
});

// ---- controls and legends
function updateLegend() {
  const d = SC.def;
  document.getElementById("legend-title").textContent = d.label + (d.modeless ? "" : " – " + modeLabel());
  document.getElementById("legend-bar").style.background = GRADIENT;
  document.getElementById("legend-min").textContent = fmt(SC.lo, 0);
  document.getElementById("legend-max").textContent = fmt(SC.hi, d.unit === "%" ? 0 : 1) + (d.unit ? " " + d.unit : "") +
    (d.max == null ? " (98th pct)" : "");
  document.getElementById("origin-bar").style.background = GRADIENT;
  document.getElementById("origin-max").textContent = fmt(OSC.hi, 0) + " min (98th pct)";
}
function refresh() {
  SC = wardScale();
  OSC = originScale();
  wardLayer.setStyle(wardStyle);
  originLayer.eachLayer(l => l.setStyle(originStyle(l.feature)));
  linkLayer.clearLayers();
  updateLegend();
}
const modesDiv = document.getElementById("modes");
MODES.forEach(m => {
  const lab = document.createElement("label");
  const r = document.createElement("input");
  r.type = "radio";
  r.name = "mode";
  r.value = m[0];
  r.checked = m[0] === state.mode;
  r.addEventListener("change", () => { state.mode = m[0]; refresh(); });
  lab.appendChild(r);
  lab.appendChild(document.createTextNode(" " + m[1]));
  modesDiv.appendChild(lab);
});
const metricSel = document.getElementById("metric");
METRICS.forEach(m => {
  const o = document.createElement("option");
  o.value = m.id;
  o.textContent = m.label;
  metricSel.appendChild(o);
});
metricSel.value = state.metric;
metricSel.addEventListener("change", () => { state.metric = metricSel.value; refresh(); });

const sw = document.getElementById("speed-swatches");
speedLabels().forEach((lab, i) => {
  const s = document.createElement("span");
  s.style.marginRight = "8px";
  s.innerHTML = "<span class='swatch' style='background:" + SPEED_COLORS[i] + "'></span>" + lab;
  sw.appendChild(s);
});
document.getElementById("counts").textContent = DATA.wards.features.length + " wards · " +
  DATA.origins.features.length + " sample points · " + DATA.venues.features.length + " venues · " +
  DATA.stops.features.length + " bus stops";

const bounds = wardLayer.getBounds();
if (bounds && bounds.isValid()) map.fitBounds(bounds); else map.setView([-33.95, 18.5], 10);
restack();
syncKeys();
updateLegend();
})();
</script>
</body>
</html>
'''


def build_html(payload: dict) -> str:
    """Embed the GeoJSON payload in the page. ('</' is escaped so data can't close the script tag.)"""
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return HTML.replace("__DATA__", data)


# --------------------------------------------------------------------------- data preparation
def to_fc(g, cols=None, simplify=None, decimals=1):
    """GeoDataFrame (EPSG:4326) -> small GeoJSON dict: rounded coords/values, only needed columns."""
    import geopandas as gpd
    import pandas as pd
    import shapely

    g = g.copy()
    if cols is not None:
        g = g[[c for c in cols if c in g.columns] + [g.geometry.name]]
    if simplify:
        g = g.set_geometry(g.geometry.simplify(simplify, preserve_topology=True))
    snapped = [shapely.set_precision(geom, 1e-5) for geom in g.geometry]
    g = g.set_geometry(gpd.GeoSeries(snapped, index=g.index, crs=g.crs))
    for c in g.columns:
        if c == g.geometry.name:
            continue
        if pd.api.types.is_float_dtype(g[c]):
            g[c] = g[c].round(decimals)
        elif (pd.api.types.is_object_dtype(g[c]) or pd.api.types.is_string_dtype(g[c])
              or pd.api.types.is_datetime64_any_dtype(g[c])):
            g[c] = g[c].map(lambda v: "" if pd.isna(v) else str(v))
    return json.loads(g.to_json(drop_id=True))


def load_payload(folder: Path, road_classes, all_roads: bool) -> dict:
    import geopandas as gpd

    def read(name, layer):
        return gpd.read_file(folder / name, layer=layer).to_crs(4326)

    wards = read("wards_accessibility.gpkg", "wards")
    origins = read("origins_accessibility.gpkg", "origins")
    segments = read("bus_segments.gpkg", "bus_segments")
    venues = read("inputs_clean.gpkg", "venues")
    stops = read("inputs_clean.gpkg", "stops")
    routes = read("inputs_clean.gpkg", "routes")

    thresholds = sorted({int(m.group(1)) for c in wards.columns
                         if (m := re.match(r"walk_venues_(\d+)min$", c))})

    names = dict(zip(wards["ward_key"], wards["ward_name"].where(wards["ward_name"] != "", "Ward " + wards["ward_id"])))
    origins["ward"] = origins["ward_key"].map(names)
    o_cols = ["ward"] + [c for c in origins.columns
                         if c.endswith("_nearest_min") or c.endswith("_nearest_venue")
                         or c == "transit_saving_min"]
    segments["share_on_road"] = segments["share_on_road"] * 100
    stop_cols = [c for c in stops.columns if c != stops.geometry.name][:4]

    payload = {
        "thresholds": thresholds,
        "wards": to_fc(wards.drop(columns=["ward_key"]), simplify=0.0002),
        "origins": to_fc(origins, o_cols),
        "venues": to_fc(venues, ["venue_name"]),
        "stops": to_fc(stops, stop_cols),
        "routes": to_fc(routes, ["route"], simplify=0.00005),
        "segments": to_fc(segments, ["route", "seg_no", "length_m", "ride_s", "mean_kph", "share_on_road"],
                          simplify=0.00005),
        "roads": None,
    }
    roads_path = folder / "drive_roads.gpkg"
    if roads_path.exists():
        roads = read("drive_roads.gpkg", "drive_roads")
        if not all_roads:
            keep = set(road_classes) | {c + "_link" for c in road_classes}
            roads = roads[roads["highway"].isin(keep)]
        payload["roads"] = to_fc(roads, ["highway", "speed_kph", "tagged"], simplify=0.0001, decimals=0)
        print(f"roads layer: {len(roads)} edges")
    else:
        print("no drive_roads.gpkg found: re-run venue_access.py with --export-roads to add the speed-limit road layer")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", type=Path, help="folder written by venue_access.py")
    ap.add_argument("--out", type=Path, help="output HTML (default: <results>/interactive_map.html)")
    ap.add_argument("--road-classes", default="motorway,trunk,primary,secondary,tertiary",
                    help="road classes to show in the speed-limit layer (links are included automatically)")
    ap.add_argument("--all-roads", action="store_true", help="include every road class (large file for a metro)")
    ap.add_argument("--serve", action="store_true", help="serve the folder at http://localhost:8000 and open it")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()

    out = a.out or (a.results / "interactive_map.html")
    payload = load_payload(a.results, [c.strip() for c in a.road_classes.split(",") if c.strip()], a.all_roads)
    html = build_html(payload)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")

    if a.serve:
        import functools
        import http.server
        import webbrowser

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out.parent))
        url = f"http://localhost:{a.port}/{out.name}"
        print(f"serving {url}  (Ctrl+C to stop)")
        webbrowser.open(url)
        with http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler) as srv:
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
