import { NavLink, Outlet } from 'react-router';
import { StatusLine } from './StatusLine';
import { ThemeToggle } from './ThemeToggle';
import './Layout.css';

const NAV = [
  { to: '/', label: 'Forecast', end: true },
  { to: '/performance', label: 'Performance', end: false },
  { to: '/model', label: 'Model', end: false },
];

export function Layout() {
  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <StatusLine />
      <header className="page grid masthead">
        <p className="masthead__mark">
          <span className="masthead__name">pricefc</span>
          <span className="masthead__what">Day-ahead electricity prices, Sweden</span>
        </p>
        <nav className="masthead__nav" aria-label="Pages">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className="masthead__link">
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="masthead__tools">
          <ThemeToggle />
        </div>
      </header>
      <main id="main" className="page">
        <Outlet />
      </main>
      <footer className="page grid colophon">
        <p>
          Forecasts are quantiles of the hourly day-ahead price in EUR/MWh, made every morning at
          09:05 Stockholm time for the following day. Hours are Stockholm local time.
        </p>
      </footer>
    </>
  );
}
