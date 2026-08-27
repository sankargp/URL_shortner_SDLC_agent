"""FastAPI URL shortener used by the governed SDLC demonstrations."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import string
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, HttpUrl, ValidationError
from sqlalchemy import Column, DateTime, Integer, String, create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

BASE62 = string.digits + string.ascii_letters
APP_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("URL_SHORTENER_DATABASE_PATH", str(APP_DIR / "urls.db")))

engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False},
)
Session = sessionmaker(bind=engine)
Base = declarative_base()


class Link(Base):
    __tablename__ = "links"

    id = Column(Integer, primary_key=True)
    code = Column(String(128), unique=True, index=True, nullable=False)
    alias = Column(String(128), unique=True, index=True, nullable=True)
    url = Column(String, nullable=False)
    clicks = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    password_salt = Column(String(64), nullable=True)
    password_hash = Column(String(64), nullable=True)


class IdempotencyRequest(Base):
    __tablename__ = "idempotency_requests"

    id = Column(Integer, primary_key=True)
    key_digest = Column(String(64), unique=True, index=True, nullable=False)
    request_digest = Column(String(64), nullable=False)
    response_json = Column(String, nullable=False)
    status_code = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def _initialize_database() -> None:
    Base.metadata.create_all(engine)
    existing_columns = {
        column["name"] for column in inspect(engine).get_columns("links")
    }
    migrations: list[str] = []
    if "alias" not in existing_columns:
        migrations.append("ALTER TABLE links ADD COLUMN alias VARCHAR(128)")
    if "expires_at" not in existing_columns:
        migrations.append("ALTER TABLE links ADD COLUMN expires_at DATETIME")
    if "password_salt" not in existing_columns:
        migrations.append("ALTER TABLE links ADD COLUMN password_salt VARCHAR(64)")
    if "password_hash" not in existing_columns:
        migrations.append("ALTER TABLE links ADD COLUMN password_hash VARCHAR(64)")
    if migrations:
        with engine.begin() as connection:
            for statement in migrations:
                connection.exec_driver_sql(statement)


_initialize_database()
app = FastAPI(title="URL Shortener", version="1.0.0")

_WINDOW_S = 60
_MAX_REQ = 60
_hits: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit(ip: str) -> None:
    now = time.time()
    requests = _hits[ip]
    while requests and now - requests[0] > _WINDOW_S:
        requests.popleft()
    if len(requests) >= _MAX_REQ:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    requests.append(now)


class ShortenRequest(BaseModel):
    url: HttpUrl
    custom_alias: str | None = None
    expiry_days: int | None = None
    password: str | None = None


class ShortenResponse(BaseModel):
    code: str
    short_url: str


class BatchShortenRequest(BaseModel):
    items: list[dict[str, object]] = Field(min_length=1, max_length=100)


_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_ALIAS_LENGTH = 64
_MAX_PASSWORD_LENGTH = 256
_PBKDF2_ITERATIONS = 310_000


def _encode(number: int) -> str:
    if number == 0:
        return BASE62[0]
    output: list[str] = []
    while number:
        number, remainder = divmod(number, len(BASE62))
        output.append(BASE62[remainder])
    return "".join(reversed(output))


def _validate_alias(alias: str | None) -> str | None:
    if alias is None:
        return None
    if not alias or len(alias) > _MAX_ALIAS_LENGTH:
        raise HTTPException(
            status_code=422,
            detail="custom_alias must be between 1 and 64 characters",
        )
    if not _ALIAS_PATTERN.fullmatch(alias):
        raise HTTPException(
            status_code=422,
            detail="custom_alias may contain only letters, numbers, '-' and '_'",
        )
    return alias


def _validate_expiry_days(expiry_days: int | None) -> int | None:
    if expiry_days is None:
        return None
    if isinstance(expiry_days, bool) or expiry_days <= 0:
        raise HTTPException(
            status_code=422,
            detail="expiry_days must be a positive integer",
        )
    return expiry_days


def _password_fields(password: str | None) -> tuple[str | None, str | None]:
    if password is None:
        return None, None
    if not password or len(password) > _MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail="password must be between 1 and 256 characters",
        )
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return salt.hex(), digest.hex()


def _password_matches(link: Link, supplied: str | None) -> bool:
    if link.password_hash is None:
        return True
    if supplied is None or link.password_salt is None:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        supplied.encode("utf-8"),
        bytes.fromhex(link.password_salt),
        _PBKDF2_ITERATIONS,
    ).hex()
    return hmac.compare_digest(candidate, link.password_hash)


def _new_generated_link(
    db,
    url: str,
    expires_at: datetime | None,
    password_salt: str | None = None,
    password_hash: str | None = None,
) -> Link:
    # The provisional namespace cannot collide with validated custom aliases.
    link = Link(
        code=f"~pending-{secrets.token_urlsafe(12)}",
        url=url,
        expires_at=expires_at,
        password_salt=password_salt,
        password_hash=password_hash,
    )
    db.add(link)
    db.flush()
    candidate_number = link.id
    while True:
        candidate = _encode(candidate_number)
        if db.query(Link.id).filter(Link.code == candidate).first() is None:
            link.code = candidate
            return link
        candidate_number += 1


def _create_link(db, body: ShortenRequest) -> Link:
    alias = _validate_alias(body.custom_alias)
    expiry_days = _validate_expiry_days(body.expiry_days)
    expires_at = (
        datetime.now(UTC).replace(tzinfo=None) + timedelta(days=expiry_days)
        if expiry_days is not None
        else None
    )
    password_salt, password_hash = _password_fields(body.password)
    if alias is not None:
        if db.query(Link.id).filter(Link.code == alias).first() is not None:
            raise HTTPException(status_code=409, detail="alias already taken")
        link = Link(
            code=alias,
            alias=alias,
            url=str(body.url),
            expires_at=expires_at,
            password_salt=password_salt,
            password_hash=password_hash,
        )
        db.add(link)
        db.flush()
        return link
    return _new_generated_link(
        db=db,
        url=str(body.url),
        expires_at=expires_at,
        password_salt=password_salt,
        password_hash=password_hash,
    )


def _shorten_result(link: Link) -> dict[str, str]:
    return {
        "code": link.code,
        "short_url": f"http://localhost:8080/{link.code}",
    }


def _canonical_batch_payload(body: BatchShortenRequest) -> str:
    return json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )


@app.post("/shorten", response_model=ShortenResponse)
def shorten(body: ShortenRequest, request: Request) -> ShortenResponse:
    _rate_limit(request.client.host if request.client else "local")
    db = Session()
    try:
        link = _create_link(db, body)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if body.custom_alias is not None:
                raise HTTPException(status_code=409, detail="alias already taken")
            raise HTTPException(status_code=503, detail="unable to allocate a short code")
        return ShortenResponse(**_shorten_result(link))
    finally:
        db.close()


@app.post("/shorten/batch")
def shorten_batch(
    body: BatchShortenRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=256),
):
    _rate_limit(request.client.host if request.client else "local")
    canonical_payload = _canonical_batch_payload(body)
    key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    request_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    db = Session()
    try:
        existing = (
            db.query(IdempotencyRequest)
            .filter(IdempotencyRequest.key_digest == key_digest)
            .first()
        )
        if existing is not None:
            if not hmac.compare_digest(existing.request_digest, request_digest):
                raise HTTPException(
                    status_code=409,
                    detail="idempotency key reused with different payload",
                )
            return JSONResponse(
                status_code=existing.status_code,
                content=json.loads(existing.response_json),
            )

        results: list[dict[str, object]] = []
        for index, raw_item in enumerate(body.items):
            try:
                with db.begin_nested():
                    item = ShortenRequest.model_validate(raw_item)
                    link = _create_link(db, item)
                    db.flush()
                    result: dict[str, object] = {
                        "index": index,
                        "status": 200,
                        **_shorten_result(link),
                    }
            except ValidationError:
                result = {"index": index, "status": 422, "detail": "invalid item"}
            except HTTPException as exc:
                result = {"index": index, "status": exc.status_code, "detail": exc.detail}
            except IntegrityError:
                result = {"index": index, "status": 409, "detail": "alias already taken"}
            results.append(result)

        status_code = 200 if all(item["status"] == 200 for item in results) else 207
        response = {"results": results}
        db.add(
            IdempotencyRequest(
                key_digest=key_digest,
                request_digest=request_digest,
                response_json=json.dumps(response, sort_keys=True, separators=(",", ":")),
                status_code=status_code,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            concurrent = (
                db.query(IdempotencyRequest)
                .filter(IdempotencyRequest.key_digest == key_digest)
                .first()
            )
            if concurrent is None or not hmac.compare_digest(
                concurrent.request_digest,
                request_digest,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="idempotency key reused with different payload",
                )
            return JSONResponse(
                status_code=concurrent.status_code,
                content=json.loads(concurrent.response_json),
            )
        return JSONResponse(status_code=status_code, content=response)
    finally:
        db.close()


@app.get("/{code}")
def redirect(
    code: str,
    x_link_password: str | None = Header(default=None, alias="X-Link-Password"),
):
    db = Session()
    try:
        link = db.query(Link).filter(Link.code == code).first()
        if link is None:
            raise HTTPException(status_code=404, detail="not found")
        if (
            link.expires_at is not None
            and datetime.now(UTC).replace(tzinfo=None) >= link.expires_at
        ):
            raise HTTPException(status_code=410, detail="link expired")
        if not _password_matches(link, x_link_password):
            raise HTTPException(status_code=401, detail="password required or incorrect")
        link.clicks = (link.clicks or 0) + 1
        db.commit()
        return RedirectResponse(url=link.url, status_code=307)
    finally:
        db.close()


@app.get("/{code}/stats")
def stats(code: str):
    db = Session()
    try:
        link = db.query(Link).filter(Link.code == code).first()
        if link is None:
            raise HTTPException(status_code=404, detail="not found")
        return {
            "code": link.code,
            "url": link.url,
            "clicks": link.clicks,
            "created_at": link.created_at.isoformat(),
            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        }
    finally:
        db.close()


@app.get("/{code}/preview")
def preview(code: str):
    db = Session()
    try:
        link = db.query(Link).filter(Link.code == code).first()
        if link is None:
            raise HTTPException(status_code=404, detail="not found")
        return {
            "code": link.code,
            "url": link.url,
            "clicks": link.clicks,
            "created_at": link.created_at.isoformat(),
            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
            "password_protected": link.password_hash is not None,
        }
    finally:
        db.close()
