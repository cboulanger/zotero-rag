"""Admin-only HTML pages: the chapter-review queue page (design spec
docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md section 6)
and the pipeline-runner page (design spec
docs/superpowers/specs/2026-07-31-admin-run-pipeline-design.md). Registered
without the /api prefix (see backend/main.py), matching
backend/api/public_query.py's own APIRouter(prefix=...) convention -- a
router mounted under /api in main.py cannot also serve an unprefixed path.

No server-side auth on either page route: /admin/* is not covered by
main.py's api_key_middleware (which only gates /api/*), and this codebase
has no cookie/session mechanism to authenticate a plain page GET. Both
pages are static shells; every actual data read/write happens via the
admin's own client-side fetch() calls, with the API key entered once in
the page and sent as the X-Zotero-API-Key header on each call.

- /admin/review's calls go to the admin-gated /api/chapter-linking/review/*
  endpoints (require_authorized_group_admin: caller must be an owner/admin
  of AUTHORIZED_GROUP_ID).
- /admin/run's calls go to /api/chapter-linking/{analyze,ocr,retrofit-link,
  segment-upload} -- these require a valid, gate-approved Zotero API key
  (enforced by api_key_middleware on every /api/* call) but, unlike the
  review endpoints, do NOT additionally require group-admin status. This
  page doesn't change that; it's the existing trust boundary for those four
  endpoints, same as the CLI scripts they mirror.
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


@router.get("/run", response_class=HTMLResponse, summary="Chapter-linking pipeline runner (admin page)")
async def run_page(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse("admin_run.html", {"request": request})
