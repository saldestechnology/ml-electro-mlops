# Sweden bidding-zone map (schematic)

`sweden.ts` holds four SVG paths, one per bidding zone (SE1–SE4), drawn by
`components/SwedenMap.tsx`. It is **generated** by `scripts/build-map.mjs`; do not edit it by
hand. The map is deliberately schematic and says so under the drawing: "Schematic — zone
borders approximate. Outline: Natural Earth."

## Outline

- Source: Natural Earth, _Admin 0 – Countries_, 1:10m, feature `ADM0_A3 = SWE`
  (all of Sweden's polygons, including Gotland, Fårö and Öland).
- URL: https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson
  (repository version `5.2.0-pre`; file SHA-256
  `239eec57ac17f100a11e2536cffc56752c318b50ae765b0918ff7aab4ce8f255`).
  Project page: https://www.naturalearthdata.com/downloads/10m-cultural-vectors/
- Retrieved: 7 October 2026.
- Licence: public domain (https://www.naturalearthdata.com/about/terms-of-use/). No
  attribution is required; we credit it anyway in the caption.
- The Sweden feature alone, coordinates rounded to 4 decimals, is committed as
  `scripts/map-data/ne_10m_sweden.json` so the build runs offline. To refresh it:
  `node scripts/build-map.mjs --extract path/to/ne_10m_admin_0_countries.geojson`.

## Steps (`node scripts/build-map.mjs`)

1. Outer rings of Sweden's polygons; islands under 100 km² are dropped (keeps the mainland,
   Gotland, Öland, Fårö, Orust and Tjörn).
2. Spherical transverse Mercator about 15° E (the central meridian of SWEREF 99 TM), so the
   shape matches the familiar Swedish maps; scaled into a 200-unit-wide viewBox.
3. Douglas–Peucker simplification, tolerance 0.2 viewBox units (about 0.25 px at 240 px wide).
4. The mainland is cut by three schematic lines (below). The script requires each line to
   cross the simplified outline exactly twice and closes both halves along the same line, so
   neighbouring zones share their border vertex for vertex: no gaps, no overlaps.
5. Islands are never cut: each goes whole to the zone its centroid falls in (Gotland and Fårö
   → SE3, Öland → SE4).
6. Each zone's label sits at the interior point farthest from the zone's edge.
7. Output: one path per zone on a 0.1-unit grid with relative coordinates (about 13 KB).

## Zone borders: approximate, not traced

There is no openly licensed vector of the official SE1–SE4 borders. Svenska kraftnät
publishes the division only as a picture (the _elområden_ map on svk.se); the polygons used
by Electricity Maps are hand-traced and AGPL-licensed; and the borders do not follow
municipal or county lines, so they cannot be assembled from administrative data. The cut
lines here are a few straight segments placed from general knowledge of that published map
and of which well-known places lie in which zone (which electricity suppliers and the
regulator, Energimarknadsinspektionen, state per address):

| Border  | Line (lon, lat)                                               | Approximates                                                                                                                             |
| ------- | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| SE1/SE2 | 11.5,66.3 → 15.5,66.1 → 18.5,65.75 → 21.4,65.12 → 24.5,64.6   | roughly the Norrbotten–Västerbotten line: Luleå and Piteå in SE1, Skellefteå in SE2; reaches the coast near 65.1° N                      |
| SE2/SE3 | 10.5,61.95 → 12.6,61.7 → 15.0,61.3 → 17.2,60.95 → 20.5,60.7   | from the Norwegian border between Härjedalen and northern Dalarna to the Gulf of Bothnia just north of Gävle (Gävle, Falun, Mora in SE3) |
| SE3/SE4 | 11.0,56.95 → 12.7,56.86 → 14.2,57.0 → 16.5,57.12 → 17.6,57.05 | west coast between Halmstad (SE4) and Falkenberg; across Småland north of Växjö; east coast between Kalmar (SE4) and Oskarshamn (SE3)    |

Gotland belongs to SE3 and Öland to SE4. Where exactly a border meets the coast is uncertain
to within some tens of kilometres; the caption says the borders are approximate, and the
map is a navigation aid next to the zone switcher, not a reference for which zone an address
is in. To change a border, edit `CUTS` in the script and re-run it.
