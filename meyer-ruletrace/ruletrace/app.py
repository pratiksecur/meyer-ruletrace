"""The RuleTrace application: Vartija core (login, roles, audit, sources, dashboard) + design checking.

    uvicorn ruletrace.app:app --reload --port 8001
    # http://localhost:8001/ruletrace   (3D design check)
    # http://localhost:8001/dashboard   (Vartija dashboard: sources, audit, executive view)
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from vartija_core.api import create_app as create_vartija_app

from .api import STATIC, make_router


def create_app(settings=None, **kw) -> FastAPI:
    app = create_vartija_app(settings, **kw)
    app.title = "RuleTrace (on Vartija core)"
    app.include_router(make_router(app))
    app.mount("/ruletrace/static", StaticFiles(directory=STATIC), name="ruletrace-static")
    return app


def __getattr__(name: str):
    if name == "app":  # built on first use, so importing this module has no side effects
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
