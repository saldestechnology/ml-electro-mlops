// Builds src/assets/map/sweden.ts: a schematic map of Sweden's four bidding zones (SE1–SE4)
// as static SVG paths. Offline, no dependencies. See src/assets/map/README.md for the source,
// licence and the reasoning behind the cut lines.
//
//   node scripts/build-map.mjs                        # rebuild from the committed extract
//   node scripts/build-map.mjs --extract <ne.geojson> # first refresh the extract from a
//                                                     # Natural Earth admin-0 countries file
//
// Steps: Sweden's polygons (Natural Earth 1:10m admin-0) -> drop islets -> spherical
// transverse Mercator about 15°E -> scale into the viewBox -> Douglas–Peucker -> cut the
// mainland with three schematic lines (SE1/SE2, SE2/SE3, SE3/SE4) -> whole islands go to the
// zone their centroid falls in -> one path per zone. A shared border is the same crossing
// points and cut vertices on both sides, so the zones tile without gaps or overlaps.
import { readFileSync, writeFileSync } from 'node:fs';

const here = (p) => new URL(p, import.meta.url);
const EXTRACT = here('./map-data/ne_10m_sweden.json');
const OUT = here('../src/assets/map/sweden.ts');

// ---- 0. optional: refresh the extract from the full Natural Earth file ---------------------
const ix = process.argv.indexOf('--extract');
if (ix > 0) {
  const file = process.argv[ix + 1];
  if (!file) throw new Error('--extract needs a path to ne_10m_admin_0_countries.geojson');
  const all = JSON.parse(readFileSync(file, 'utf8'));
  const swe = all.features.find((ft) => ft.properties.ADM0_A3 === 'SWE');
  if (!swe) throw new Error('no SWE feature in ' + file);
  const r = (v) => Math.round(v * 1e4) / 1e4;
  const polys =
    swe.geometry.type === 'Polygon' ? [swe.geometry.coordinates] : swe.geometry.coordinates;
  const geometry = {
    type: 'MultiPolygon',
    coordinates: polys.map((poly) => poly.map((ring) => ring.map(([x, y]) => [r(x), r(y)]))),
  };
  const fc = {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', properties: { NAME: 'Sweden', ADM0_A3: 'SWE' }, geometry }],
  };
  writeFileSync(EXTRACT, JSON.stringify(fc));
  console.log('wrote', EXTRACT.pathname);
}

// ---- 1. parameters ------------------------------------------------------------------------
const WIDTH = 200; // viewBox width; the height follows from the projection
const PAD = 4;
const TOLERANCE = 0.2; // Douglas–Peucker, viewBox units (~0.25 px at 240 px wide)
const MIN_AREA_KM2 = 100; // keep Gotland, Öland, Fårö and the larger islands; drop the islets
const LON0 = 15; // central meridian (as SWEREF 99 TM)
const ZONES = ['SE1', 'SE2', 'SE3', 'SE4'];

// Schematic zone borders, [lon, lat] from west to east. Each starts west of the Norwegian
// border and ends out at sea, and must cross the simplified mainland exactly twice.
// Deliberately a few straight segments, not a tracing; README.md says what they approximate.
const CUTS = [
  {
    between: ['SE1', 'SE2'],
    line: [
      [11.5, 66.3],
      [15.5, 66.1],
      [18.5, 65.75],
      [21.4, 65.12],
      [24.5, 64.6],
    ],
  },
  {
    between: ['SE2', 'SE3'],
    line: [
      [10.5, 61.95],
      [12.6, 61.7],
      [15.0, 61.3],
      [17.2, 60.95],
      [20.5, 60.7],
    ],
  },
  {
    between: ['SE3', 'SE4'],
    line: [
      [11.0, 56.95],
      [12.7, 56.86],
      [14.2, 57.0],
      [16.5, 57.12],
      [17.6, 57.05],
    ],
  },
];

// ---- 2. projection -------------------------------------------------------------------------
const rad = Math.PI / 180;
/** Spherical transverse Mercator about LON0, in Earth radii; y grows southwards (SVG). */
function tm([lon, lat]) {
  const phi = lat * rad;
  const dl = (lon - LON0) * rad;
  const b = Math.cos(phi) * Math.sin(dl);
  const x = 0.5 * Math.log((1 + b) / (1 - b));
  const y = Math.atan2(Math.tan(phi), Math.cos(dl));
  return [x, -y];
}
const R_KM = 6371;
function areaOf(ring) {
  let a = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++)
    a += (ring[j][0] + ring[i][0]) * (ring[j][1] - ring[i][1]);
  return a / 2;
}

const src = JSON.parse(readFileSync(EXTRACT, 'utf8')).features[0].geometry.coordinates;
// outer rings only (Natural Earth's Sweden has no holes); keep lon/lat alongside for centroids
let parts = src
  .map((poly) => ({ ll: poly[0], xy: poly[0].map(tm) }))
  .map((p) => ({ ...p, km2: Math.abs(areaOf(p.xy)) * R_KM * R_KM }))
  .filter((p) => p.km2 >= MIN_AREA_KM2)
  .sort((a, b) => b.km2 - a.km2);

let minX = Infinity,
  minY = Infinity,
  maxX = -Infinity,
  maxY = -Infinity;
for (const p of parts)
  for (const [x, y] of p.xy) {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  }
const k = (WIDTH - 2 * PAD) / (maxX - minX);
const HEIGHT = Math.ceil((maxY - minY) * k + 2 * PAD);
const toView = ([x, y]) => [(x - minX) * k + PAD, (y - minY) * k + PAD];
const project = (ll) => toView(tm(ll));

// ---- 3. simplification ---------------------------------------------------------------------
function segDist(p, a, b) {
  const dx = b[0] - a[0],
    dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  let t = l2 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
}
function dp(pts, tol) {
  if (pts.length < 3) return pts;
  let idx = 0,
    max = 0;
  for (let i = 1; i < pts.length - 1; i++) {
    const d = segDist(pts[i], pts[0], pts[pts.length - 1]);
    if (d > max) [idx, max] = [i, d];
  }
  if (max <= tol) return [pts[0], pts[pts.length - 1]];
  return [...dp(pts.slice(0, idx + 1), tol).slice(0, -1), ...dp(pts.slice(idx), tol)];
}
/** Simplify a closed ring: split at the vertex farthest from the first, simplify both halves. */
function simplifyRing(ring) {
  const closed = ring[0][0] === ring.at(-1)[0] && ring[0][1] === ring.at(-1)[1];
  const r = closed ? ring.slice(0, -1) : ring;
  let far = 0,
    best = 0;
  for (let i = 1; i < r.length; i++) {
    const d = Math.hypot(r[i][0] - r[0][0], r[i][1] - r[0][1]);
    if (d > best) [far, best] = [i, d];
  }
  const a = dp(r.slice(0, far + 1), TOLERANCE);
  const b = dp([...r.slice(far), r[0]], TOLERANCE);
  return [...a.slice(0, -1), ...b.slice(0, -1)];
}
parts = parts.map((p) => ({ ...p, ring: simplifyRing(p.xy.map(toView)) }));

// ---- 4. cutting ------------------------------------------------------------------------------
function intersect(p, p2, q, q2) {
  const r = [p2[0] - p[0], p2[1] - p[1]];
  const s = [q2[0] - q[0], q2[1] - q[1]];
  const den = r[0] * s[1] - r[1] * s[0];
  if (den === 0) return null;
  const t = ((q[0] - p[0]) * s[1] - (q[1] - p[1]) * s[0]) / den;
  const u = ((q[0] - p[0]) * r[1] - (q[1] - p[1]) * r[0]) / den;
  if (t < 0 || t >= 1 || u < 0 || u > 1) return null;
  return { t, u, pt: [p[0] + t * r[0], p[1] + t * r[1]] };
}
const meanY = (r) => r.reduce((s, p) => s + p[1], 0) / r.length;

/** Split a ring by a polyline that crosses it exactly twice; returns [north, south]. */
function split(ring, cut, name) {
  const xs = [];
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i],
      b = ring[(i + 1) % ring.length];
    for (let j = 0; j < cut.length - 1; j++) {
      const hit = intersect(a, b, cut[j], cut[j + 1]);
      if (hit) xs.push({ i, t: hit.t, s: j + hit.u, j, pt: hit.pt });
    }
  }
  if (xs.length !== 2)
    throw new Error(`${name}: cut crosses the outline ${xs.length} times, expected 2`);
  xs.sort((a, b) => a.i - b.i || a.t - b.t);
  const [c1, c2] = xs;
  // the cut's own vertices strictly between two crossings, walked from one to the other
  const along = (from, to) => {
    const out = [];
    if (from.s < to.s) for (let j = from.j + 1; j <= to.j; j++) out.push(cut[j]);
    else for (let j = from.j; j > to.j; j--) out.push(cut[j]);
    return out;
  };
  const a = [c1.pt, ...ring.slice(c1.i + 1, c2.i + 1), c2.pt, ...along(c2, c1)];
  const b = [c2.pt, ...ring.slice(c2.i + 1), ...ring.slice(0, c1.i + 1), c1.pt, ...along(c1, c2)];
  return meanY(a) < meanY(b) ? [a, b] : [b, a];
}

const zonePolys = Object.fromEntries(ZONES.map((z) => [z, []]));
let rest = parts[0].ring;
for (const { between, line } of CUTS) {
  const [north, south] = split(rest, line.map(project), between.join('/'));
  zonePolys[between[0]].push(north);
  rest = south;
}
zonePolys.SE4.push(rest);

// islands: whole, to the zone their lon/lat centroid falls in
function cutLatAt(line, lon) {
  if (lon <= line[0][0]) return line[0][1];
  for (let j = 0; j < line.length - 1; j++) {
    const [a, b] = [line[j], line[j + 1]];
    if (lon <= b[0]) return a[1] + ((lon - a[0]) / (b[0] - a[0])) * (b[1] - a[1]);
  }
  return line.at(-1)[1];
}
function zoneOf([lon, lat]) {
  for (const { between, line } of CUTS) if (lat > cutLatAt(line, lon)) return between[0];
  return 'SE4';
}
const islands = parts.slice(1).map((p) => {
  const c = [0, 1].map((d) => p.ll.reduce((s, q) => s + q[d], 0) / p.ll.length);
  const z = zoneOf(c);
  zonePolys[z].push(p.ring);
  return { z, c, km2: p.km2, n: p.ring.length };
});

// ---- 5. labels: the point of the zone's main polygon farthest from its edge (grid search) ---
function inside(p, ring) {
  let c = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [a, b] = [ring[i], ring[j]];
    if (
      a[1] > p[1] !== b[1] > p[1] &&
      p[0] < ((b[0] - a[0]) * (p[1] - a[1])) / (b[1] - a[1]) + a[0]
    )
      c = !c;
  }
  return c;
}
function edgeDist(p, ring) {
  let d = Infinity;
  for (let i = 0; i < ring.length; i++)
    d = Math.min(d, segDist(p, ring[i], ring[(i + 1) % ring.length]));
  return d;
}
function labelPoint(ring) {
  let best = null,
    bestD = -1;
  const xs = ring.map((p) => p[0]),
    ys = ring.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  for (let x = x0; x <= x1; x += 0.5)
    for (let y = y0; y <= y1; y += 0.5) {
      const p = [x, y];
      if (!inside(p, ring)) continue;
      const d = edgeDist(p, ring);
      if (d > bestD) [best, bestD] = [p, d];
    }
  return { at: best, room: bestD };
}

// ---- 6. output: absolute move, then relative lines on a 0.1 grid ----------------------------
const f = (v) => (Math.round(v * 10) / 10).toString();
function pathOf(polys) {
  return polys
    .map((r) => {
      const pts = r.map(([x, y]) => [Math.round(x * 10), Math.round(y * 10)]);
      const rel = [];
      for (let i = 1; i < pts.length; i++) {
        const dx = pts[i][0] - pts[i - 1][0],
          dy = pts[i][1] - pts[i - 1][1];
        if (dx === 0 && dy === 0) continue;
        rel.push(`${f(dx / 10)} ${f(dy / 10)}`);
      }
      const d = `M${f(pts[0][0] / 10)} ${f(pts[0][1] / 10)}l${rel.join(' ')}z`;
      return d.replace(/ -/g, '-');
    })
    .join('');
}

const zones = ZONES.map((z) => {
  const main = zonePolys[z].reduce((a, b) => (Math.abs(areaOf(b)) > Math.abs(areaOf(a)) ? b : a));
  const { at, room } = labelPoint(main);
  return { zone: z, d: pathOf(zonePolys[z]), label: [+f(at[0]), +f(at[1])], room: +f(room) };
});

const body = zones
  .map(
    (z) =>
      `  {\n    zone: '${z.zone}',\n    label: [${z.label[0]}, ${z.label[1]}],\n    d: '${z.d}',\n  },`,
  )
  .join('\n');
const ts = `// Generated by scripts/build-map.mjs -- do not edit. Schematic: the zone borders are
// approximate cut lines, not the official ones. Outline: Natural Earth 1:10m admin-0
// (public domain). See ./README.md.
import type { Zone } from '../../api';

export interface ZoneShape {
  zone: Zone;
  /** label anchor: the interior point farthest from the zone's edge, viewBox units */
  label: readonly [number, number];
  /** SVG path, one closed subpath per polygon (mainland part, then whole islands) */
  d: string;
}

export const MAP_WIDTH = ${WIDTH};
export const MAP_HEIGHT = ${HEIGHT};

export const ZONE_SHAPES: readonly ZoneShape[] = [
${body}
];
`;
writeFileSync(OUT, ts);
console.log(`viewBox 0 0 ${WIDTH} ${HEIGHT}; ${parts.length} polygons kept`);
for (const z of zones)
  console.log(z.zone, 'path chars', z.d.length, 'label', z.label, 'room', z.room);
for (const i of islands)
  console.log('island ->', i.z, i.c.map((v) => v.toFixed(2)).join(','), `${Math.round(i.km2)} km2`);
console.log('bytes', Buffer.byteLength(ts));
