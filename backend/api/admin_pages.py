"""Admin-only HTML pages. Currently just the chapter-review queue page
(design spec docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md
section 6). Registered without the /api prefix (see backend/main.py),
matching backend/api/public_query.py's own APIRouter(prefix=...)
convention -- a router mounted under /api in main.py cannot also serve an
unprefixed path.

No server-side auth on the page route itself: /admin/* is not covered by
main.py's api_key_middleware (which only gates /api/*), and this codebase
has no cookie/session mechanism to authenticate a plain page GET. The page
is a static shell; every actual data read/write happens via the admin's
own client-side fetch() calls to the already admin-gated
/api/chapter-linking/review/* endpoints, with the API key entered once in
the page and sent as the X-Zotero-API-Key header on each call.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/admin")

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/review", response_class=HTMLResponse, summary="Chapter-review queue admin page")
async def review_page(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse("admin_review.html", {"request": request})
