"""URL Shortener — the target system the agents build and enhance.

Core APIs:
  POST /shorten        -> create a short code (optional custom_alias, expiry_days)
  GET  /{code}         -> redirect to the original URL (410 if expired)
  GET  /{code}/stats   -> per-link analytics (click count, created_at)

Reliability features: input validation, collision-safe base62 codes, expiry,
basic in-memory rate limiting, graceful errors. Persistence: SQLite.

FastAPI auto-exposes an OpenAPI schema at /docs and /openapi.json — this doubles
as the "API/schema definitions" deliverable.
"""
from __future__ import annotations

import string
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy import Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

BASE62 = string.digits + string.ascii_letters
engine = create_engine("sqlite:///target-app/urls.db", connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
Base = declarative_base()


class Link(Base):
    __tablename__ = "links"
    id = Column(Integer, primary_key=True)          # monotonic id -> collision-free code
    code = Column(String, unique=True, index=True)
    url = Column(String, nullable=False)
    clicks = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)     # brownfield enhancement


Base.metadata.create_all(engine)
app = FastAPI(title="URL Shortener", version="1.0.0")

# --- simple in-memory rate limiter (reliability) --------------------------
_WINDOW_S = 60
_MAX_REQ = 60
_hits: dict[str, deque] = defaultdict(deque)


def _rate_limit(ip: str) -> None:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > _WINDOW_S:
        q.popleft()
    if len(q) >= _MAX_REQ:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    q.append(now)


def _encode(n: int) -> str:
    if n == 0:
        return BASE62[0]
    out = []
    while n:
        n, r = divmod(n, 62)
        out.append(BASE62[r])
    return "".join(reversed(out))


class ShortenRequest(BaseModel):
    url: HttpUrl
    custom_alias: str | None = None
    expiry_days: int | None = None


class ShortenResponse(BaseModel):
    code: str
    short_url: str


@app.post("/shorten", response_model=ShortenResponse)
def shorten(body: ShortenRequest, request: Request):
    _rate_limit(request.client.host if request.client else "local")
    db = Session()
    try:
        expires = (datetime.utcnow() + timedelta(days=body.expiry_days)) if body.expiry_days else None
        if body.custom_alias:
            if db.query(Link).filter_by(code=body.custom_alias).first():
                raise HTTPException(status_code=409, detail="alias already taken")
            link = Link(code=body.custom_alias, url=str(body.url), expires_at=expires)
        else:
            link = Link(url=str(body.url), expires_at=expires)
            db.add(link); db.flush()               # get autoincrement id
            link.code = _encode(link.id)
        db.add(link); db.commit()
        return ShortenResponse(code=link.code, short_url=f"http://localhost:8080/{link.code}")
    finally:
        db.close()


@app.get("/{code}")
def redirect(code: str):
    db = Session()
    try:
        link = db.query(Link).filter_by(code=code).first()
        if not link:
            raise HTTPException(status_code=404, detail="not found")
        if link.expires_at and datetime.utcnow() > link.expires_at:
            raise HTTPException(status_code=410, detail="link expired")
        link.clicks += 1
        db.commit()
        return RedirectResponse(url=link.url, status_code=307)
    finally:
        db.close()


@app.get("/{code}/stats")
def stats(code: str):
    db = Session()
    try:
        link = db.query(Link).filter_by(code=code).first()
        if not link:
            raise HTTPException(status_code=404, detail="not found")
        return {"code": link.code, "url": link.url, "clicks": link.clicks,
                "created_at": link.created_at.isoformat(),
                "expires_at": link.expires_at.isoformat() if link.expires_at else None}
    finally:
        db.close()
