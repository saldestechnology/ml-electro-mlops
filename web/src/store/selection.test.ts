import { forecastPath, resetSelection, selectionFromParams, useSelection } from './selection';

describe('selection store', () => {
  it('starts at SE3, latest', () => {
    expect(useSelection.getState()).toMatchObject({ zone: 'SE3', origin: null });
  });

  it('sets zone and origin independently, so a zone switch keeps the day', () => {
    const s = useSelection.getState();
    s.setOrigin('2026-10-20');
    s.setZone('SE1');
    expect(useSelection.getState()).toMatchObject({ zone: 'SE1', origin: '2026-10-20' });
    useSelection.getState().setOrigin(null);
    expect(useSelection.getState().origin).toBeNull();
  });

  it('sync replaces both and does not notify when nothing changed', () => {
    const seen = vi.fn();
    const off = useSelection.subscribe(seen);
    useSelection.getState().sync('SE2', '2026-10-19');
    expect(useSelection.getState()).toMatchObject({ zone: 'SE2', origin: '2026-10-19' });
    expect(seen).toHaveBeenCalledTimes(1);
    useSelection.getState().sync('SE2', '2026-10-19');
    expect(seen).toHaveBeenCalledTimes(1);
    off();
  });

  it('resets', () => {
    useSelection.getState().sync('SE4', '2026-10-01');
    resetSelection();
    expect(useSelection.getState()).toMatchObject({ zone: 'SE3', origin: null });
  });
});

describe('selection URLs', () => {
  it('reads zone and origin, ignoring malformed values', () => {
    expect(selectionFromParams(new URLSearchParams('zone=SE2&origin=2026-10-20'))).toEqual({
      zone: 'SE2',
      origin: '2026-10-20',
    });
    expect(selectionFromParams(new URLSearchParams('zone=XX&origin=yesterday'))).toEqual({
      zone: 'SE3',
      origin: null,
    });
    expect(selectionFromParams(new URLSearchParams(''))).toEqual({ zone: 'SE3', origin: null });
  });

  it('builds a path with the origin only when there is one', () => {
    expect(forecastPath('SE1', null)).toBe('/?zone=SE1');
    expect(forecastPath('SE1', '2026-10-20')).toBe('/?zone=SE1&origin=2026-10-20');
  });
});
