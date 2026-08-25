"""Integration tests for the URL shortener target app."""
from fastapi.testclient import TestClient
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Ensure a clean db per test session
import importlib
main = importlib.import_module("target-app.main") if False else None

def _client():
    # import here so the sqlite file path resolves from repo root
    from importlib import import_module
    mod = __import__("target-app.main", fromlist=["app"])
    return TestClient(mod.app)


def test_shorten_and_redirect():
    c = _client()
    r = c.post("/shorten", json={"url": "https://example.com"})
    assert r.status_code == 200
    code = r.json()["code"]
    # redirect returns 307 to original (follow disabled to inspect)
    r2 = c.get(f"/{code}", follow_redirects=False)
    assert r2.status_code == 307
    assert r2.headers["location"] == "https://example.com/"


def test_stats_increments():
    c = _client()
    code = c.post("/shorten", json={"url": "https://a.co"}).json()["code"]
    c.get(f"/{code}", follow_redirects=False)
    s = c.get(f"/{code}/stats").json()
    assert s["clicks"] >= 1


def test_invalid_url_rejected():
    c = _client()
    r = c.post("/shorten", json={"url": "not-a-url"})
    assert r.status_code == 422
