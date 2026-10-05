"""Open-Meteo weather client (spec section 5.2). No API key; data is CC BY 4.0.

Endpoints:
- historical_forecast: stitched first hours of successive runs (training history).
- previous_runs: variables at fixed lead days (`{var}_previous_day{N}`), for honest backtests.
- single_runs: individual model runs. Used live to rebuild exactly the values Previous Runs
  will later report at a fixed lead (`{var}_previous_day{N}` at hour T comes from the run
  initialised at floor(T - N days) on the run grid), since for future hours the Previous Runs
  API substitutes the latest run.
- forecast: live forecast, archived as issued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal, get_args

import pandas as pd
import requests

from pricefc.config import Location, OpenMeteoConfig
from pricefc.ingest.throttle import RateLimiter, RetryableError, with_retries

SOURCE = "open_meteo"
Endpoint = Literal["historical_forecast", "previous_runs", "single_runs", "forecast"]
ENDPOINTS: tuple[Endpoint, ...] = get_args(Endpoint)


@dataclass
class WeatherPull:
    endpoint: str
    location: Location
    data: pd.DataFrame
    requested_start: pd.Timestamp
    requested_end: pd.Timestamp
    query: dict[str, Any]
    chunks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def dataset(self) -> str:
        return f"{self.endpoint}__{self.query['models']}"


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split an inclusive date range into inclusive calendar-year chunks."""
    if end < start:
        raise ValueError("end before start")
    chunks = []
    cur = start
    while cur <= end:
        chunk_end = min(date(cur.year, 12, 31), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def lead_run(t: pd.Timestamp, lead_days: int, interval_hours: int) -> pd.Timestamp:
    """Initialisation time of the run whose value Previous Runs reports for hour `t` at
    `lead_days` (the latest run at least `lead_days` days before `t`)."""
    return (t - pd.Timedelta(days=lead_days)).floor(f"{interval_hours}h")


def weather_source_tag(endpoint: str, model: str) -> str:
    """Value for the `weather_source` MLflow tag."""
    return f"open-meteo:{endpoint}:{model}"


class OpenMeteoClient:
    def __init__(
        self,
        cfg: OpenMeteoConfig,
        session: requests.Session | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.cfg = cfg
        self.session = session or requests.Session()
        self.limiter = limiter or RateLimiter(cfg.min_interval_s)

    def hourly_variables(self, endpoint: Endpoint) -> list[str]:
        if endpoint in ("previous_runs", "single_runs"):
            leads = self.cfg.endpoints[endpoint].lead_days
            if not leads:
                raise ValueError(f"{endpoint} requires lead_days")
            return [f"{v}_previous_day{n}" for n in leads for v in self.cfg.variables]
        return list(self.cfg.variables)

    def base_params(self, endpoint: Endpoint, loc: Location) -> dict[str, Any]:
        variables = self.cfg.variables if endpoint == "single_runs" else None
        return {
            "latitude": loc.lat,
            "longitude": loc.lon,
            "hourly": ",".join(variables or self.hourly_variables(endpoint)),
            "models": self.cfg.model,
            "timezone": "GMT",
            "timeformat": "unixtime",
            "wind_speed_unit": self.cfg.wind_speed_unit,
        }

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        def call() -> dict[str, Any]:
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=self.cfg.timeout_s)
            except (requests.ConnectionError, requests.Timeout) as e:
                raise RetryableError(str(e)) from e
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code != 200:
                raise RuntimeError(f"Open-Meteo HTTP {resp.status_code}: {resp.text[:500]}")
            body: dict[str, Any] = resp.json()
            if body.get("error"):
                raise RuntimeError(f"Open-Meteo error: {body.get('reason')}")
            return body

        return with_retries(call, max_retries=self.cfg.max_retries)

    @staticmethod
    def parse_hourly(body: dict[str, Any], variables: list[str]) -> pd.DataFrame:
        hourly = body["hourly"]
        missing = [v for v in variables if v not in hourly]
        if missing:
            raise RuntimeError(f"Open-Meteo response missing variables: {missing}")
        df = pd.DataFrame(
            {v: pd.to_numeric(pd.Series(hourly[v]), errors="coerce") for v in variables}
        )
        df.insert(0, "timestamp", pd.to_datetime(pd.Series(hourly["time"]), unit="s", utc=True))
        return df.astype({v: "float64" for v in variables})

    @staticmethod
    def _response_meta(body: dict[str, Any]) -> dict[str, Any]:
        keys = ("latitude", "longitude", "elevation", "generationtime_ms", "utc_offset_seconds")
        return {k: body.get(k) for k in keys}

    def fetch_range(
        self,
        endpoint: Literal["historical_forecast", "previous_runs"],
        loc: Location,
        start: date,
        end: date,
    ) -> WeatherPull:
        """Historical endpoints: inclusive UTC date range, chunked by calendar year."""
        if endpoint not in ("historical_forecast", "previous_runs"):
            raise ValueError(f"{endpoint} does not support date ranges")
        ep = self.cfg.endpoints[endpoint]
        variables = self.hourly_variables(endpoint)
        params = self.base_params(endpoint, loc)
        frames, chunks = [], []
        for c_start, c_end in year_chunks(start, end):
            p = {**params, "start_date": c_start.isoformat(), "end_date": c_end.isoformat()}
            body = self._get(ep.url, p)
            frames.append(self.parse_hourly(body, variables))
            chunks.append(
                {
                    "start_date": p["start_date"],
                    "end_date": p["end_date"],
                    **self._response_meta(body),
                }
            )
        data = pd.concat(frames, ignore_index=True)
        data = data.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        return WeatherPull(
            endpoint=endpoint,
            location=loc,
            data=data,
            requested_start=pd.Timestamp(start, tz="UTC"),
            requested_end=pd.Timestamp(end + timedelta(days=1), tz="UTC"),
            query={
                **params,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "url": ep.url,
            },
            chunks=chunks,
        )

    def fetch_lead_runs(self, loc: Location, start: date, end: date) -> WeatherPull:
        """Fixed-lead values for the inclusive UTC date range, from the runs themselves.

        Same columns as `previous_runs` (`{var}_previous_day{N}`). Hours whose run is not
        published yet stay null and are listed in the chunks, so validation fails loudly.
        """
        ep = self.cfg.endpoints["single_runs"]
        if not ep.lead_days or ep.run_interval_hours is None:
            raise ValueError("single_runs requires lead_days and run_interval_hours")
        hours = pd.date_range(
            pd.Timestamp(start, tz="UTC"),
            pd.Timestamp(end + timedelta(days=1), tz="UTC"),
            freq="h",
            inclusive="left",
        )
        params = self.base_params("single_runs", loc)
        out = pd.DataFrame({"timestamp": hours})
        chunks: list[dict[str, Any]] = []
        for lead in ep.lead_days:
            cols = {v: f"{v}_previous_day{lead}" for v in self.cfg.variables}
            runs = pd.DatetimeIndex([lead_run(t, lead, ep.run_interval_hours) for t in hours])
            parts = []
            for run in runs.unique():
                targets = hours[runs == run]
                days = (targets.max().normalize() - run.normalize()).days + 1
                p = {**params, "run": run.strftime("%Y-%m-%dT%H:%M"), "forecast_days": days}
                try:
                    body = self._get(ep.url, p)
                except RuntimeError as e:
                    if "not available" not in str(e):
                        raise
                    chunks.append({"lead_days": lead, "run": p["run"], "available": False})
                    continue
                frame = self.parse_hourly(body, list(cols)).set_index("timestamp")
                parts.append(frame.reindex(targets).rename(columns=cols))
                chunks.append(
                    {
                        "lead_days": lead,
                        "run": p["run"],
                        "available": True,
                        "hours": len(targets),
                        **self._response_meta(body),
                    }
                )
            lead_frame = pd.concat(parts) if parts else pd.DataFrame(columns=list(cols.values()))
            lead_frame = lead_frame.reindex(hours).astype("float64")
            out = out.join(lead_frame, on="timestamp")
        return WeatherPull(
            endpoint="single_runs",
            location=loc,
            data=out,
            requested_start=hours[0],
            requested_end=hours[-1] + pd.Timedelta(hours=1),
            query={
                **params,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "lead_days": list(ep.lead_days),
                "run_interval_hours": ep.run_interval_hours,
                "url": ep.url,
            },
            chunks=chunks,
        )

    def fetch_forecast(self, loc: Location) -> WeatherPull:
        """Live forecast from today 00:00 UTC for `forecast_days` days, as issued now."""
        ep = self.cfg.endpoints["forecast"]
        if ep.forecast_days is None:
            raise ValueError("forecast endpoint requires forecast_days")
        variables = self.hourly_variables("forecast")
        params = {
            **self.base_params("forecast", loc),
            "forecast_days": ep.forecast_days,
            "past_days": 0,
        }
        body = self._get(ep.url, params)
        data = self.parse_hourly(body, variables)
        return WeatherPull(
            endpoint="forecast",
            location=loc,
            data=data,
            requested_start=data["timestamp"].min(),
            requested_end=data["timestamp"].max() + pd.Timedelta(hours=1),
            query={**params, "url": ep.url},
            chunks=[self._response_meta(body)],
        )
