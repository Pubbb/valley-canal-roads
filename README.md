# Valley Canal Roads

A map of Phoenix-metro canals, canal-bank roads, paved canal trails, the street openings onto them,
and the places where roads cross the Valley Metro Rail and Tempe Streetcar tracks.
Built from OpenStreetMap data.

**Open the map:** https://pubbb.github.io/valley-canal-roads/

On a phone, use your browser's "Add to Home Screen" to install it like an app.

## What's on the map

- **Canals:** open stretches solid, underground or covered stretches dashed.
- **Canal-bank roads and paved canal trails:** roads and paths that run alongside an open canal.
- **Canal openings:** where a car could turn off a public street onto a canal road or trail.
  Red means no barrier is recorded in OpenStreetMap within 60 m; grey means a gate, bollard or other barrier is mapped.
- **Light rail crossings:** where a road a car can use meets the tracks, grouped as tracks in a street median,
  own right-of-way or shared streetcar lane, and gated.
- **Collected:** tick any point as collected. Your list is saved in your own browser; use Back up / Restore to move it between devices.

Red means "no barrier recorded," not "confirmed open." OpenStreetMap barrier mapping on SRP canal banks is patchy,
and SRP generally prohibits private vehicles on canal banks.

## Refreshing the data

Requires Python 3 with `pip install osmium shapely pillow`.

```bash
python build_canal_map.py
git add -A
git commit -m "Refresh map data"
git push
```

The script downloads Geofabrik's Arizona extract (about 300 MB), rebuilds `docs/` (the site) and
`valley-canal-roads.html` (a single file that works offline from disk), then deletes the extract.
Use `--keep-pbf` to keep it, or `--html-only` to re-render after editing `map_template.html`.

Map data © OpenStreetMap contributors (ODbL). Basemap tiles © Esri.
