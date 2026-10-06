import { useEffect, useState } from 'react';
import { Icon } from './Icon';

type Theme = 'light' | 'dark';
const KEY = 'pricefc-theme';

function stored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : null;
  } catch {
    return null;
  }
}

function systemTheme(): Theme {
  return typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

/** Light/dark switch. Without a stored choice the page follows prefers-color-scheme. */
export function ThemeToggle() {
  const [choice, setChoice] = useState<Theme | null>(stored);
  const effective = choice ?? systemTheme();

  useEffect(() => {
    const root = document.documentElement;
    if (choice) root.setAttribute('data-theme', choice);
    else root.removeAttribute('data-theme');
  }, [choice]);

  const next: Theme = effective === 'dark' ? 'light' : 'dark';
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={() => {
        setChoice(next);
        try {
          localStorage.setItem(KEY, next);
        } catch {
          // storage unavailable: the choice lasts for this page view
        }
      }}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
    >
      <Icon name={effective === 'dark' ? 'sun' : 'moon'} size={20} />
    </button>
  );
}
