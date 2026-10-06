# pricefc web

Read-only dashboard for the pricefc day-ahead price forecasts (SE1–SE4): the D+1 fan chart,
live performance against the naive 7-day baseline, and the served champion per zone.
Vite + React + TypeScript (strict), pnpm. The API contract lives with the API; `src/api.ts`
is the typed, runtime-checked client for exactly those endpoints.

## Commands

```sh
pnpm install
pnpm dev             # http://localhost:5173, /api proxied to http://127.0.0.1:8000
pnpm dev:fixtures    # same, but answered by src/fixtures (no API needed)
pnpm build           # type-check + production build into dist/ (served by the API)
pnpm build:fixtures  # a build that runs on fixtures, for design review
pnpm preview         # serve dist/ on http://localhost:4173 (also proxies /api)
pnpm test            # vitest + Testing Library (jsdom)
pnpm lint            # eslint (typescript-eslint strict, type-checked) + prettier --check
pnpm typecheck       # tsc
node scripts/contrast.mjs     # WCAG contrast of the colour tokens, both themes
node scripts/build-icons.mjs  # regenerate the bundled Tabler icon subset
node scripts/build-map.mjs    # regenerate the schematic zone map (src/assets/map/README.md)
```

Fixture mode is explicit: `VITE_USE_FIXTURES=1` swaps the client's `fetch` for
`fixtureFetch`, so URL building and parsing run exactly as against the real API, and the
status line shows a “Sample data” marker. Fixture dates follow the real Stockholm date (latest
origin = today, delivery = tomorrow; tests pin a date). They cover: all four zones, a stale
zone (SE3, last forecast two days ago), a zone with nothing scored (SE4), past origins with
actuals and the newest one without, and the origin before the most recent DST change (a 23- or
25-hour day) in SE1/SE2's origin picker.

## Layout

```
src/api.ts                 contract types, runtime parsers, client
src/fixtures/              deterministic contract-shaped fixtures + fake fetch
src/hooks/                 useApi (tiny fetch hook), status context, element width
src/lib/                   Stockholm time helpers, number formatting, day summary
src/components/chart/      fan chart (hand-built SVG, d3-scale/d3-shape), DST slot logic
src/components/            layout, status line, zone switcher + schematic zone map, table, states, icons
src/assets/map/            generated SE1–SE4 map paths (Natural Earth outline, approximate borders)
src/pages/                 Forecast (/), Performance (/performance), Model (/model)
src/styles/                fonts.css, tokens.css (all colours/type/space), base.css
```

## Design

Swiss/International Typographic Style: Switzer only (self-hosted, see
`src/assets/fonts/README.md`), a 12-column grid with an 8 px spacing unit, flush-left
ragged-right text, hierarchy through size and weight, tabular figures throughout. Black,
white and greys, with one signal red reserved for the median forecast. Every colour is a
custom property in `tokens.css`; dark mode follows `prefers-color-scheme` and can be forced
with the toggle (`<html data-theme="light|dark">`, remembered in localStorage). Icons are
Tabler via Iconify, bundled offline (`@iconify/react/offline` + a generated subset), so the
app makes no third-party requests at runtime.
