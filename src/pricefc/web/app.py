"""FastAPI routes for the read-only price forecast dashboard."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from pricefc.config import BaseConfig, load_config
from pricefc.web import data

BACKTEST_BASELINES: dict[str, dict[str, float]] = {
    "SE1": {"pinball": 4.61, "naive_7d_pinball": 11.12},
    "SE2": {"pinball": 4.53, "naive_7d_pinball": 11.48},
    "SE3": {"pinball": 5.25, "naive_7d_pinball": 11.61},
    "SE4": {"pinball": 6.39, "naive_7d_pinball": 13.69},
}


class _SPAStaticFiles(StaticFiles):
    """Return the frontend entry point when a client-side route is not a file."""

    async def get_response(self, path: str, scope: Any) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if path == "api" or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


def create_app(base: BaseConfig | None = None) -> FastAPI:
    """Build the read-only API, optionally mounting a built SPA at the root."""
    base = base or load_config(Path("configs/base.yaml"))
    env = os.environ.get("PRICEFC_ENV", "dev")
    app = FastAPI(title="pricefc web API")

    if env.lower() == "dev":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "env": os.environ.get("PRICEFC_ENV", "dev"),
            "git_sha": os.environ.get("PRICEFC_GIT_SHA", "unknown"),
        }

    @app.get("/api/zones")
    def zones() -> list[dict[str, Any]]:
        return data.zone_summaries(base)

    @app.get("/api/zones/{zone}/origins")
    def origins(zone: str) -> dict[str, Any]:
        _require_zone(zone)
        return {"zone": zone, "origins": data.forecast_origins(base, zone)}

    @app.get("/api/zones/{zone}/forecast")
    def forecast(zone: str, origin: date | None = None) -> dict[str, Any]:
        _require_zone(zone)
        result = data.forecast_data(base, zone, origin)
        if result is None:
            raise HTTPException(status_code=404, detail="Forecast origin not found")
        return result

    @app.get("/api/zones/{zone}/performance")
    def performance(zone: str, days: int = Query(default=30, ge=1, le=3650)) -> dict[str, Any]:
        _require_zone(zone)
        return data.performance_data(base, zone, days, BACKTEST_BASELINES[zone])

    @app.get("/api/zones/{zone}/model")
    def model(zone: str) -> dict[str, Any]:
        _require_zone(zone)
        return data.model_metadata(base, zone)

    dist = Path(os.environ.get("PRICEFC_WEB_DIST", "web/dist"))
    if dist.is_dir():
        app.mount("/", _SPAStaticFiles(directory=dist, html=True), name="web")
    return app


def _require_zone(zone: str) -> None:
    """Reject unknown zones before using the path on disk."""
    if zone not in data.ZONES:
        raise HTTPException(status_code=404, detail=f"Unknown zone: {zone}")
