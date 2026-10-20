from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuthSession, DriverProfile, DriverVerification, RiderProfile, User
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.security import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
bearer_scheme = HTTPBearer(auto_error=False)
DatabaseSession = Annotated[Session, Depends(get_db)]
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
REFRESH_COOKIE_NAME = "cab_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _set_refresh_cookie(response: Response, request: Request, token: str, expires: datetime) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
        expires=expires,
    )


def _clear_refresh_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=request.app.state.settings.refresh_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _issue_access_token(user: User, request: Request) -> TokenResponse:
    settings = request.app.state.settings
    return TokenResponse(
        access_token=create_access_token(
            user.id, user.role, settings.jwt_secret_key, settings.access_token_minutes
        ),
        expires_in=settings.access_token_minutes * 60,
        user=UserResponse.model_validate(user),
    )


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: DatabaseSession) -> User:
    email = str(payload.email).strip().lower()
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")

    user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    if payload.role == "rider":
        user.rider_profile = RiderProfile()
    else:
        user.driver_profile = DriverProfile(verification_status=DriverVerification.UNVERIFIED)
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already registered"
        ) from exc
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DatabaseSession,
) -> TokenResponse:
    email = str(payload.email).strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(user.password_hash, payload.password) or not user.is_active:
        raise _unauthorized()

    now = datetime.now(UTC)
    refresh_token = create_refresh_token()
    expires_at = now + timedelta(days=request.app.state.settings.refresh_token_days)
    db.add(
        AuthSession(
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(refresh_token),
            expires_at=expires_at,
        )
    )
    db.commit()
    _set_refresh_cookie(response, request, refresh_token, expires_at)
    return _issue_access_token(user, request)


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, db: DatabaseSession) -> TokenResponse:
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_token:
        raise _unauthorized()

    session = db.scalar(
        select(AuthSession)
        .where(AuthSession.refresh_token_hash == hash_refresh_token(raw_token))
        .with_for_update()
    )
    now = datetime.now(UTC)
    expires_at = session.expires_at if session is not None else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    else:
        expires_at = expires_at.astimezone(UTC)
    if session is None or session.revoked_at is not None or expires_at <= now or not session.user.is_active:
        _clear_refresh_cookie(response, request)
        raise _unauthorized()

    user = session.user
    new_token = create_refresh_token()
    session.refresh_token_hash = hash_refresh_token(new_token)
    db.commit()
    _set_refresh_cookie(response, request, new_token, expires_at)
    return _issue_access_token(user, request)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: DatabaseSession) -> Response:
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_token:
        session = db.scalar(
            select(AuthSession).where(AuthSession.refresh_token_hash == hash_refresh_token(raw_token))
        )
        if session is not None and session.revoked_at is None:
            session.revoked_at = datetime.now(UTC)
            db.commit()
    _clear_refresh_cookie(response, request)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserResponse)
def get_me(
    request: Request,
    credentials: BearerCredentials,
    db: DatabaseSession,
) -> User:
    if credentials is None:
        raise _unauthorized()
    try:
        claims = jwt.decode(
            credentials.credentials,
            request.app.state.settings.jwt_secret_key,
            algorithms=["HS256"],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={"require": ["sub", "role", "iat", "exp", "iss", "aud", "jti"]},
        )
        user_id = UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise _unauthorized() from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active or user.role != claims.get("role"):
        raise _unauthorized()
    return user
