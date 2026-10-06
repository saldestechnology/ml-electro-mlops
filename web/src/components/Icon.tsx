// Tabler icons through Iconify, fully offline: the offline build never calls the Iconify API,
// and only the subset in src/icons/tabler-subset.json (see scripts/build-icons.mjs) is bundled.
import { addCollection, Icon as IconifyIcon } from '@iconify/react/offline';
import tabler from '../icons/tabler-subset.json';

addCollection(tabler);

export type IconName =
  'sun' | 'moon' | 'alert-triangle' | 'circle-dashed' | 'chevron-down' | 'arrow-up-right';

export function Icon({
  name,
  size = 20,
  label,
}: {
  name: IconName;
  size?: number;
  label?: string;
}) {
  return (
    <IconifyIcon
      icon={`tabler:${name}`}
      width={size}
      height={size}
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? 'img' : undefined}
    />
  );
}
