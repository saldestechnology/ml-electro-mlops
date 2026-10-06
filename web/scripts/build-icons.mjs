// Extracts the few Tabler icons the app uses from @iconify-json/tabler into a small local
// collection, so nothing is fetched from api.iconify.design at runtime and the bundle stays
// small. Run `node scripts/build-icons.mjs` after changing ICONS.
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const ICONS = ['sun', 'moon', 'alert-triangle', 'circle-dashed', 'chevron-down', 'arrow-up-right'];

const require = createRequire(import.meta.url);
const full = JSON.parse(readFileSync(require.resolve('@iconify-json/tabler/icons.json'), 'utf8'));
const icons = {};
for (const name of ICONS) {
  if (!full.icons[name]) throw new Error(`tabler has no icon ${name}`);
  icons[name] = full.icons[name];
}
const subset = { prefix: full.prefix, width: full.width, height: full.height, icons };
const out = new URL('../src/icons/tabler-subset.json', import.meta.url);
writeFileSync(out, JSON.stringify(subset, null, 2) + '\n');
console.log(`wrote ${ICONS.length} icons to ${out.pathname}`);
