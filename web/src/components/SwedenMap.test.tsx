import { act, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { MAP_HEIGHT, MAP_WIDTH, ZONE_SHAPES } from '../assets/map/sweden';
import { SwedenMap } from './SwedenMap';

const renderMap = (ui: React.ReactElement) => render(<MemoryRouter>{ui}</MemoryRouter>);

describe('zone shapes', () => {
  it('has the four zones in order, each a non-empty set of closed subpaths', () => {
    expect(ZONE_SHAPES.map((s) => s.zone)).toEqual(['SE1', 'SE2', 'SE3', 'SE4']);
    for (const { d, label } of ZONE_SHAPES) {
      const subpaths = d.split('M').filter(Boolean);
      expect(subpaths.length).toBeGreaterThan(0);
      for (const sp of subpaths) {
        expect(sp).toMatch(/z$/);
        // a move plus at least a triangle's worth of relative lines
        expect(sp.match(/-?\d+(\.\d)?/g)?.length ?? 0).toBeGreaterThanOrEqual(6);
      }
      expect(label[0]).toBeGreaterThan(0);
      expect(label[0]).toBeLessThan(MAP_WIDTH);
      expect(label[1]).toBeGreaterThan(0);
      expect(label[1]).toBeLessThan(MAP_HEIGHT);
    }
    // islands go whole to a zone: Gotland and Fårö with SE3, Öland with SE4
    const count = (z: string) =>
      (ZONE_SHAPES.find((s) => s.zone === z)?.d.match(/M/g) ?? []).length;
    expect(count('SE3')).toBeGreaterThanOrEqual(3);
    expect(count('SE4')).toBe(2);
  });

  it('stays small', () => {
    const bytes = ZONE_SHAPES.reduce((n, s) => n + s.d.length, 0);
    expect(bytes).toBeLessThan(25_000);
  });
});

describe('SwedenMap', () => {
  it('renders one link per zone, pointing at that zone, with the selected one current', () => {
    renderMap(<SwedenMap zone="SE3" />);
    const map = screen.getByRole('group', { name: 'Bidding zones of Sweden' });
    const links = within(map).getAllByRole('link');
    expect(links).toHaveLength(4);
    expect(links.map((l) => l.getAttribute('href'))).toEqual([
      '/?zone=SE1',
      '/?zone=SE2',
      '/?zone=SE3',
      '/?zone=SE4',
    ]);
    const current = within(map).getByRole('link', { name: 'SE3, Stockholm (selected)' });
    expect(current).toHaveAttribute('aria-current', 'true');
    expect(within(map).getByRole('link', { name: 'SE1, Luleå' })).not.toHaveAttribute(
      'aria-current',
    );
    for (const l of links) expect(l.querySelector('path')?.getAttribute('d')).toMatch(/^M.+z$/);
    expect(screen.getByText(/Schematic — zone borders approximate/)).toBeInTheDocument();
  });

  it('marks stale zones like the zone switcher does', () => {
    renderMap(<SwedenMap zone="SE1" stale={{ SE3: true, SE4: false }} />);
    expect(screen.getByRole('link', { name: 'SE3, Stockholm (stale)' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'SE4, Malmö' })).toBeInTheDocument();
    expect(screen.getByTestId('stale-SE3')).toBeInTheDocument();
    expect(screen.queryByTestId('stale-SE4')).toBeNull();
  });

  it('draws a focus outline around the keyboard-focused zone', () => {
    const { container } = renderMap(<SwedenMap zone="SE1" />);
    const se2 = screen.getByRole('link', { name: 'SE2, Sundsvall' });
    act(() => {
      se2.focus();
    });
    const ring = container.querySelector('.smap__focus');
    expect(ring?.getAttribute('d')).toBe(ZONE_SHAPES[1]?.d);
    act(() => {
      se2.blur();
    });
    expect(container.querySelector('.smap__focus')).toBeNull();
  });
});
