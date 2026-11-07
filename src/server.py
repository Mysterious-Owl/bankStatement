"""
Application entry point.

Routes live in `api/`, one module per area. This file only assembles them, so
adding an endpoint means touching the router it belongs to rather than a single
1600-line module.
"""
import os

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import config
from api import (routes_annotations, routes_audit, routes_dashboard,
                 routes_ingest, routes_insights, routes_pages, routes_rules,
                 routes_tags, routes_transfers)

app = FastAPI(title="Bank Statement Analytics")

STATIC_DIR = os.path.join(config.SRC_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

for router in (
    routes_pages.router,
    routes_rules.router,
    routes_ingest.router,
    routes_dashboard.router,
    routes_transfers.router,
    routes_tags.router,
    routes_annotations.router,
    routes_audit.router,
    routes_insights.router,
):
    app.include_router(router)


if __name__ == "__main__":
    print("Server running at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
