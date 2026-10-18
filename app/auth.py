from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status


def _extract_token(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if not authorization:
        return None
    scheme, separator, credential = authorization.partition(" ")
    if not separator or scheme.lower() not in {"bearer", "token"}:
        return None
    return credential.strip()


def require_api_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> None:
    settings = request.app.state.settings
    supplied = _extract_token(authorization, x_api_key)
    if supplied is None or not hmac.compare_digest(supplied, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

