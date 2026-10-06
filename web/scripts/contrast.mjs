// Checks WCAG contrast of the colour tokens in src/styles/tokens.css (both themes).
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8');
function block(sel) {
  const i = css.indexOf(sel);
  const body = css.slice(css.indexOf('{', i) + 1, css.indexOf('}', i));
  return Object.fromEntries(
    [...body.matchAll(/--(c-[\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]),
  );
}
const lum = (hex) => {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const l = c.map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};
const themes = { light: block(':root {'), dark: block(":root[data-theme='dark']") };
const checks = [
  ['c-fg', 'c-bg', 4.5],
  ['c-fg-2', 'c-bg', 4.5],
  ['c-fg-3', 'c-bg', 4.5],
  ['c-fg-3', 'c-surface', 4.5],
  ['c-fg-2', 'c-surface', 4.5],
  ['c-signal-text', 'c-bg', 4.5],
  ['c-invert-fg', 'c-invert-bg', 4.5],
  ['c-signal', 'c-bg', 3],
  ['c-signal', 'c-band-50', 3],
  ['c-signal', 'c-band-90', 3],
  ['c-fg', 'c-band-50', 3],
  ['c-naive', 'c-bg', 3],
  ['c-naive', 'c-band-90', 3],
  ['c-rule-strong', 'c-bg', 3],
  // the chart's zero/base rule: visible as a reference, yet clearly apart from the Actual line
  ['c-rule-zero', 'c-bg', 2],
  ['c-fg', 'c-rule-zero', 3],
];
let bad = 0;
for (const [name, t] of Object.entries(themes)) {
  for (const [a, b, min] of checks) {
    const r = ratio(t[a], t[b]);
    const ok = r >= min;
    if (!ok) bad++;
    console.log(
      `${name.padEnd(6)} ${a.padEnd(14)} on ${b.padEnd(10)} ${r.toFixed(2).padStart(5)} ${ok ? 'ok' : `FAIL (< ${min})`}`,
    );
  }
}
process.exit(bad ? 1 : 0);
