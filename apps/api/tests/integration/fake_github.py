"""A tiny stand-in for GitHub's OAuth + API endpoints. It really checks the client
secret and the PKCE verifier, so the tests prove PayForge sends them correctly."""

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request


@dataclass
class FakeGithub:
    codes: dict[str, str] = field(default_factory=dict)  # code -> code_challenge
    tokens: set[str] = field(default_factory=set)
    profile: dict[str, Any] = field(
        default_factory=lambda: {"id": 4242, "login": "kiran-dev", "name": "Kiran"}
    )
    emails: list[dict[str, Any]] = field(
        default_factory=lambda: [{"email": "kiran@example.com", "primary": True, "verified": True}]
    )

    def approve(self, authorize_url: str) -> tuple[str, str]:
        """What GitHub does when the user clicks Authorize: a code bound to the challenge."""
        query = parse_qs(urlparse(authorize_url).query)
        code = secrets.token_hex(8)
        self.codes[code] = query["code_challenge"][0]
        return code, query["state"][0]


def create_fake_github() -> tuple[FastAPI, FakeGithub]:
    fake = FakeGithub()
    app = FastAPI()

    @app.post("/login/oauth/access_token")
    async def access_token(request: Request) -> dict[str, str]:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        challenge = fake.codes.pop(form.get("code", ""), None)  # codes are single-use
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        verifier_ok = challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")
        if (
            form.get("client_id") != os.environ["GITHUB_CLIENT_ID"]
            or form.get("client_secret") != os.environ["GITHUB_CLIENT_SECRET"]
            or not verifier_ok
        ):
            return {
                "error": "bad_verification_code",
                "error_description": "The code or verifier is wrong.",
            }
        token = secrets.token_hex(12)
        fake.tokens.add(token)
        return {"access_token": token, "token_type": "bearer"}

    def check(request: Request) -> None:
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in fake.tokens:
            raise HTTPException(401, "Bad credentials")

    @app.get("/api/user")
    async def user(request: Request) -> dict[str, Any]:
        check(request)
        return fake.profile

    @app.get("/api/user/emails")
    async def emails(request: Request) -> list[dict[str, Any]]:
        check(request)
        return fake.emails

    return app, fake
