# Valley Canal Roads

A map of Phoenix-metro canals, canal-bank roads, paved canal trails, the street openings onto them,
and the places where roads cross the Valley Metro Rail and Tempe Streetcar tracks.
Built from OpenStreetMap data.

**Open the map:** https://pubbb.github.io/valley-canal-roads/

On a phone, use your browser's "Add to Home Screen" to install it like an app.

## What's on the map

- **Canals:** open stretches solid, underground or covered stretches dashed.
- **Canal roads:** roads and paths that run alongside an open canal, grouped by drivability: drivable dirt or
  gravel, drivable paved, drivable with surface not recorded, cars not allowed, and trails (no cars).
- **Canal openings:** where a car could turn off a public street onto a canal road or trail.
  Red means no barrier is recorded in OpenStreetMap within 60 m; grey means a gate, bollard or other barrier is mapped.
- **Light rail crossings:** where a road a car can use meets the tracks, grouped as tracks in a street median,
  own right-of-way or shared streetcar lane, and gated. Hidden by default while work focuses on canals.
- **Collected:** tick any point as collected. Your list is saved in your own browser; use Back up / Restore to move it between devices.

Red means "no barrier recorded," not "confirmed open." OpenStreetMap barrier mapping on SRP canal banks is patchy,
and SRP generally prohibits private vehicles on canal banks.

## Team work

Crews can share zones and progress. In the map's panel, the team lead taps **Start a new team**, sets a PIN and
shares the invite link. Crew members open the link and pick their name.

- Canal openings are grouped into work zones that follow the canals and keep driving short: 4–8 stops
  (openings within 150 m count as one stop), at most 12 openings, with a stored best visiting order.
- Each day, people claim a zone (or a team lead assigns one); a claimed zone can't be taken by anyone else that day.
- Marking a point **Completed** or **Can't access** (with a reason) syncs to everyone and stays. It works offline
  and syncs when back in signal. A zone closes when every point is one or the other.
- Optional AM/PM tracking keeps completions and claims separately for day and night shifts.
- Team lead tools (PIN, optional): assign zones, carry over yesterday's unfinished zones, choose which points the
  team works on (all, no barrier recorded, or drivable canal roads only), AM/PM tracking, crew list, reopen or
  undo points.

Shared data lives in a free Firebase Firestore project (`team_config.json`, protected by `firestore.rules`).
Anyone with a team's invite link can read and change that team's data, so only share it with your crew.
The team lead PIN keeps the lead tools out of casual reach but is not real security. Never commit a team code
to this public repository (exported PDFs are git-ignored for that reason).

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
