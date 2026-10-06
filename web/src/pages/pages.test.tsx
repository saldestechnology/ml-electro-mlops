import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { emptyFetch, pinToday, renderApp } from '../test/render';

// Fixtures follow the clock: pin it to Sat 24 Oct 2026 so tomorrow is the 25-hour DST day.
beforeEach(() => {
  pinToday();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('forecast page', () => {
  it('renders the default zone (SE3) with a stale warning, figures, chart and table', async () => {
    renderApp('/');
    expect(await screen.findByText('Friday 23 October 2026')).toBeInTheDocument();
    expect(screen.getByText('Stale: no forecast made today.')).toBeInTheDocument();
    expect(screen.getByText(/2 days ago, on Thu 22 Oct/)).toBeInTheDocument();
    expect(screen.getByText('Daily mean, median')).toBeInTheDocument();
    expect(screen.getByRole('application', { name: /SE3 price forecast/ })).toBeInTheDocument();
    const table = screen.getByRole('table');
    // header + 24 hours on a normal day
    expect(within(table).getAllByRole('row')).toHaveLength(25);
    expect(within(table).getByRole('columnheader', { name: 'Actual' })).toBeInTheDocument();
    // status line marks SE3 as stale
    expect(screen.getByText(/stale · last forecast 22 Oct/)).toBeInTheDocument();
    expect(screen.getByText('Sample data')).toBeInTheDocument();
  });

  it('summarises the forecast error by part of the day when actuals exist', async () => {
    renderApp('/');
    const section = await screen.findByRole('region', { name: /Forecast error/ });
    expect(within(section).getByText(/over all 24 hours/)).toBeInTheDocument();
    // the fixture's SE3 night is badly under-forecast: the one figure marked as largest
    const largest = within(section).getByText('(largest)').closest('div');
    expect(largest).toHaveTextContent(/Night 00–05/);
    expect(largest).toHaveTextContent(/−\d/);
    expect(within(section).getByText('Mean absolute error, median')).toBeInTheDocument();
    expect(section).toHaveTextContent(/\d+ above q95 · \d+ below q05/);
    const table = screen.getByRole('table');
    expect(within(table).getByRole('columnheader', { name: 'Bias' })).toBeInTheDocument();
  });

  it('labels the 25-hour DST day and its repeated hour', async () => {
    renderApp('/?zone=SE1');
    expect(await screen.findByText(/25-hour day/)).toBeInTheDocument();
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(26);
    expect(within(table).getByRole('rowheader', { name: '02′' })).toBeInTheDocument();
    // no actuals yet for the newest day: say so, and no actual column
    expect(screen.getByText(/not in the dataset yet/)).toBeInTheDocument();
    expect(within(table).queryByRole('columnheader', { name: 'Actual' })).toBeNull();
    expect(within(table).queryByRole('columnheader', { name: 'Bias' })).toBeNull();
    expect(screen.queryByRole('region', { name: /Forecast error/ })).toBeNull();
  });

  it('reads out quantiles with the keyboard', async () => {
    renderApp('/?zone=SE1');
    const chart = await screen.findByRole('application');
    chart.focus();
    fireEvent.keyDown(chart, { key: 'ArrowRight' });
    expect(screen.getByText('00:00–01:00')).toBeInTheDocument();
    fireEvent.keyDown(chart, { key: 'ArrowRight' });
    fireEvent.keyDown(chart, { key: 'ArrowRight' });
    fireEvent.keyDown(chart, { key: 'ArrowRight' });
    expect(screen.getByText(/02′:00–03:00/)).toBeInTheDocument();
    expect(screen.getByText('q50 median')).toBeInTheDocument();
    fireEvent.keyDown(chart, { key: 'End' });
    expect(screen.getByText('23:00–00:00')).toBeInTheDocument();
  });

  it('switches origin through the picker', async () => {
    renderApp('/?zone=SE2');
    const picker = await screen.findByLabelText(/Origin \(forecast made on\)/);
    await userEvent.selectOptions(picker, '2026-10-20');
    expect(await screen.findByText('Wednesday 21 October 2026')).toBeInTheDocument();
    expect(screen.getByText(/Viewing an earlier origin/)).toBeInTheDocument();
  });

  it('shows a designed empty state when a zone has no forecasts', async () => {
    renderApp('/?zone=SE2', emptyFetch);
    expect(await screen.findByText('No forecasts for SE2 yet')).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
  });
});

describe('performance page', () => {
  it('compares all four zones and marks the unscored one', async () => {
    renderApp('/performance');
    const table = await screen.findByRole('table');
    for (const z of ['SE1', 'SE2', 'SE3', 'SE4'])
      expect(within(table).getByRole('columnheader', { name: z })).toBeInTheDocument();
    expect(within(table).getAllByText('not scored yet').length).toBeGreaterThan(0);
    expect(within(table).getByText('4.61')).toBeInTheDocument(); // SE1 backtest reference
    expect(screen.getByText(/SE4 has no forecast day with published actuals/)).toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: /daily pinball loss/ })).toHaveLength(3);
  });

  it('de-emphasises skill and coverage below the minimum sample', async () => {
    renderApp('/performance');
    const table = await screen.findByRole('table');
    expect(screen.getByText(/at least 7 scored days/)).toBeInTheDocument();
    // SE3 has one scored day: skill and both coverages carry the caveat, muted
    const caveats = within(table).getAllByText('1 day scored — too early to judge');
    expect(caveats).toHaveLength(3);
    for (const c of caveats) expect(c.closest('td')).toHaveClass('matrix__thin');
    // SE1 has a full month: no caveat, skill at full weight
    const rows = within(table).getAllByRole('row');
    const skill = rows.find((r) => r.textContent.startsWith('Skill vs naive 7d'));
    const cells = within(skill as HTMLElement).getAllByRole('cell');
    expect(cells[0]).not.toHaveClass('matrix__thin');
    expect(cells[2]).toHaveClass('matrix__thin');
  });

  it('marks a single scored day and breaks lines across missing days', async () => {
    renderApp('/performance');
    const se3 = await screen.findByRole('img', { name: /SE3 daily pinball loss, 1 day scored/ });
    expect(se3.querySelectorAll('circle.daily__model')).toHaveLength(1);
    expect(se3.querySelectorAll('circle.daily__naive')).toHaveLength(1);
    expect(se3.querySelectorAll('path.fan__median')).toHaveLength(0);
    // SE2 has a three-day gap: two separate line runs
    const se2 = screen.getByRole('img', { name: /SE2 daily pinball loss/ });
    expect(se2.querySelectorAll('path.fan__median')).toHaveLength(2);
    expect(se2.querySelectorAll('path.fan__naive')).toHaveLength(2);
  });

  it('shows the nothing-scored state when no zone has scores', async () => {
    renderApp('/performance', emptyFetch);
    expect(await screen.findByRole('region', { name: 'Nothing scored yet' })).toBeInTheDocument();
    expect(screen.getAllByText('Nothing scored yet')).toHaveLength(5); // summary + four panels
  });
});

describe('model page', () => {
  it('renders a champion card per zone', async () => {
    renderApp('/model');
    await waitFor(() => {
      expect(screen.getAllByRole('article')).toHaveLength(4);
    });
    expect(await screen.findAllByText('ensemble_hourly_exp')).toHaveLength(4);
    expect(screen.getByText('v5')).toBeInTheDocument();
    expect(screen.getAllByText('lightgbm')).toHaveLength(4);
  });

  it('shows per-zone empty cards when nothing is served', async () => {
    renderApp('/model', emptyFetch);
    expect(await screen.findAllByText('No served model')).toHaveLength(4);
  });
});
