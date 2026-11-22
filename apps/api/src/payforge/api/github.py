from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse

from payforge.auth import github
from payforge.auth.sessions import create_session, set_session_cookie
from payforge.errors import AppError


async def _enabled() -> None:
    if not github.github_login_enabled():
        raise AppError(503, "github_login_disabled", "GitHub login is not configured.")


router = APIRouter(prefix="/auth", tags=["Dashboard auth"], dependencies=[Depends(_enabled)])


@router.get("/github", status_code=302, response_class=RedirectResponse)
async def start() -> RedirectResponse:
    """Redirects the browser to GitHub's consent page."""
    return RedirectResponse(await github.authorize_url(), status_code=302)


@router.get("/github/callback", status_code=302, response_class=RedirectResponse)
async def callback(
    state: Annotated[str, Query(min_length=1, max_length=100)],
    code: Annotated[str | None, Query(max_length=200)] = None,
    error: Annotated[str | None, Query(max_length=100)] = None,
) -> RedirectResponse:
    """GitHub redirects here. `error` is set when the user clicked Cancel."""
    if error or not code:
        return RedirectResponse("/app/login.html?error=github_cancelled", status_code=302)
    user = await github.finish_login(code, state)
    session = await create_session(user)
    response = RedirectResponse("/app/", status_code=302)
    set_session_cookie(response, session.id)
    return response
