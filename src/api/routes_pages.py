"""Static page routes."""
import os

from fastapi import APIRouter
from fastapi.responses import FileResponse

import config

router = APIRouter()

STATIC_DIR = os.path.join(config.SRC_DIR, "static")

# path -> file. Every page was its own three-line handler; they only ever
# differed by filename.
PAGES = {
    "/": "dashboard.html",
    "/upload": "upload.html",
    "/reconcile": "reconcile.html",
    "/rules": "rules.html",
    "/tags": "tags.html",
    "/category-tags": "category-tags.html",
    "/data": "data.html",
    "/self-transfers": "self-transfers.html",
    "/audit": "audit.html",
    "/budgets": "budgets.html",
    "/insights": "insights.html",
}


def _page(filename):
    async def handler():
        return FileResponse(os.path.join(STATIC_DIR, filename))
    return handler


for _path, _filename in PAGES.items():
    router.add_api_route(_path, _page(_filename), methods=["GET"], include_in_schema=False)
