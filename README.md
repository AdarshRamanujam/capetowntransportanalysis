# Live-music-venue accessibility by ward — Cape Town

Travel time from every ward to every live music venue by **walking**, **walk + bus**, and **driving**,
using OSM speed limits. Built on OSMnx (graphs, speed limits, snapping) with scipy for the routing.

## Install
```bash
conda create -n ctaccess -c conda-forge python=3.12 osmnx geopandas shapely scipy matplotlib osmium-tool
conda activate ctaccess
```

## Run
```bash
# 1. PBF -> clipped drive.osm / walk.osm  (OSMnx reads OSM XML, not PBF)
python prepare_osm.py south-africa-latest.osm.pbf --out-dir osm_prepared \
       --bbox 18.30,-34.40,19.05,-33.45

# 2. analysis
python venue_access.py \
    --drive-xml osm_prepared/drive.osm --walk-xml osm_prepared/walk.osm \
    --wards wards.shp --stops stops.geojson --routes routes.geojson \
    --venues venues.geojson --out results
```
```bash
# 3. interactive map (add --export-roads to step 2 first to get the speed-limit road layer)
python make_web_map.py results --serve        # opens http://localhost:8000/interactive_map.html
```
The map has a mode switcher, a ward-metric dropdown, toggleable layers (wards, bus routes, bus segments coloured
by the speed used, drive network by speed limit, sample points, stops, venues), hover tooltips, and straight
lines from a clicked sample point to its nearest venue by each mode. It is one self-contained `.html` file
(Leaflet and the basemap tiles load from the internet).

If a layer lands in the wrong place, use `--venues-crs 4326` and/or `--swap-xy venues` (also `stops`, `routes`)
in step 2; the script stops with a message if a layer is nowhere near the wards.

Add `--ward-query 'MunicName == "City of Cape Town"'` if the wards file covers more than the city,
and `--wards-crs 4326` if it has no `.prj`. Run `python selftest.py` to check the routing core.

## What it does
1. **Drive graph** from `drive.osm`: `speed_kph` = OSM `maxspeed`, else a South-African default per road class
   (`SA_HWY_SPEEDS` in `venue_access.py`); `travel_time` = length / speed.
2. **Walk graph** from `walk.osm`, 5 km/h, every way two-way.
3. **Bus ride times**: each stop is placed on its route line; the line between consecutive stops is sampled every
   25 m and each sample takes the speed limit of the road beneath it (`ox.nearest_edges`, 35 m tolerance). Time =
   sum(length / min(limit, 80 km/h) x factor). No road matched -> 30 km/h. A transfer-aware transit graph adds
   boarding (access walk + 5 min expected wait), riding (+20 s dwell per stop) and alighting.
4. **Origins**: 500 m grid inside each ward. Door-to-door time to every venue (origin and venue are each snapped to
   the network; the gap is walked).
5. **Ward metrics** (per mode `walk_`, `transit_`, `drive_`): `nearest_median/mean/p90` minutes to the nearest venue,
   `venues_<T>min` (average venues reachable within T min), `pct_<T>min` (% of origins with >= 1 venue within T),
   plus `transit_saving_median_min` and `pct_origins_bus_helps`.

## Outputs (`results/`)
`wards_accessibility.gpkg|.geojson|.csv`, `origins_accessibility.gpkg`, `bus_segments.gpkg` (check the
speed-limit ride times: `mean_kph`, `share_on_road`), `accessibility_map.png`.

## Assumptions to review
- No timetable data, so waits are a flat 5 min and buses run at the speed limit (`--bus-speed-factor 0.7` is more
  realistic for congested corridors). Walking/driving are free-flow; no hills, traffic or parking time.
- Each route line is ridden in its drawn direction only; add `--two-way-routes` if your GeoJSON has one line per
  route rather than one per direction.
- The OSM XML for a whole metro is large; keep the bbox tight if loading is slow or memory-hungry.
