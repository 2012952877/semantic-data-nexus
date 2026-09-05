from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import UTC, datetime

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg import Error as PostgresError
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from semantic_backend.auth_context import AccessDenied, TrustedContext, request_context
from semantic_backend.authorization import PostgresAuthorization

SERVICE_ISSUER = "https://control.example.test"
SERVICE_AUDIENCE = "semantic-backend"


class ServiceAuthentication(BaseHTTPMiddleware):
    def __init__(
        self, app: ASGIApp, *, authority: PostgresAuthorization, public_key: str | None = None
    ) -> None:
        super().__init__(app)
        material = public_key or os.environ.get("SEMANTIC_NEXUS_SERVICE_PUBLIC_KEY", "")
        key = serialization.load_pem_public_key(material.encode())
        if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
            raise ValueError(
                "Service authentication requires an RSA public key of at least 2048 bits"
            )
        self._key = key
        self._authority = authority

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in {"/health/live", "/health/ready"}:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or len(header) > 16384:
            return JSONResponse({"detail": "Service authentication required"}, status_code=401)
        try:
            token = header[7:]
            decoded = jwt.decode_complete(
                token,
                self._key,
                algorithms=["RS256"],
                issuer=SERVICE_ISSUER,
                audience=SERVICE_AUDIENCE,
                leeway=0,
                options={
                    "require": ["exp", "iat", "nbf", "iss", "aud", "jti", "ctx", "htm", "htu", "bh"]
                },
            )
            claims = decoded["payload"]
            body = await request.body()
            if len(body) > 65536:
                return JSONResponse({"detail": "Request is too large"}, status_code=413)
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if (
                decoded["header"].get("typ") != "nexus-service+jwt"
                or claims["htm"] != request.method
                or claims["htu"] != target
                or not isinstance(claims["bh"], str)
                or not hmac.compare_digest(claims["bh"], hashlib.sha256(body).hexdigest())
                or any(type(claims[name]) is not int for name in ("exp", "iat", "nbf"))
                or not 0 < claims["exp"] - claims["iat"] <= 30
                or claims["nbf"] != claims["iat"]
                or not isinstance(claims["jti"], str)
                or re.fullmatch(r"[a-f0-9]{32}", claims["jti"]) is None
            ):
                raise AccessDenied("Invalid service request binding.")
            context = TrustedContext.model_validate(claims["ctx"])
            permission = "run.reader" if request.method == "GET" else "run.contributor"
            await self._authority.accept_assertion(
                context, permission, claims["jti"], datetime.fromtimestamp(claims["exp"], UTC)
            )
        except (jwt.InvalidTokenError, ValidationError, AccessDenied, ValueError, TypeError):
            return JSONResponse({"detail": "Service authorization denied"}, status_code=403)
        except (PostgresError, TimeoutError):
            return JSONResponse({"detail": "Authorization store unavailable"}, status_code=503)
        context_token = request_context.set(context)
        try:
            response = await call_next(request)
            # Never release a result after revocation during slow orchestration/detail retrieval.
            await self._authority.reauthorize(context, permission)
            response.headers["Cache-Control"] = "no-store"
            return response
        except AccessDenied:
            return JSONResponse({"detail": "Service authorization denied"}, status_code=403)
        except (PostgresError, TimeoutError):
            return JSONResponse({"detail": "Authorization store unavailable"}, status_code=503)
        finally:
            request_context.reset(context_token)
