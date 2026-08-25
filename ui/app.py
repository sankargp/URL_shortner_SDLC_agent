"""Governance surface: a thin FastAPI app that lists pending human-approval
checkpoints and live reliability metrics for the latest run. The approval file
remains the source of truth (auditable); the UI just writes the decision.

Run with: `dashboard`  (serves http://localhost:8000)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from orchestrator.context import RunStore, latest_run
from orchestrator.gates import pending_approvals, decide

app = FastAPI(title="Agentic SDLC — Governance Dashboard")
WORKSPACE = os.getenv("WORKSPACE_DIR", "workspace")


def _store() -> RunStore | None:
    rid = latest_run(WORKSPACE)
    return RunStore(rid, WORKSPACE) if rid else None


@app.get("/", response_class=HTMLResponse)
def home():
    store = _store()
    if not store:
        return "<h2>No runs yet.</h2><p>Run <code>make demo</code> first.</p>"
    approvals = pending_approvals(store)
    metrics_path = store.root / "metrics.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}

    rows = ""
    for a in approvals:
        rows += f"""
        <div class="card">
          <h3>{a['id']} · node <code>{a['node']}</code> · impact {a['impact']}</h3>
          <p>{a['question']}</p>
          <form action="/decide" method="post">
            <input type="hidden" name="approval" value="{a['id']}">
            <button name="decision" value="approve">✅ Approve</button>
            <button name="decision" value="reject">⛔ Reject</button>
          </form>
        </div>"""
    if not approvals:
        rows = "<p>No pending approvals. 🎉</p>"

    metric_html = "".join(f"<li><b>{k}</b>: {v}</li>" for k, v in metrics.items())
    return f"""
    <html><head><title>Governance Dashboard</title>
    <style>
      body{{font-family:system-ui;margin:2rem;max-width:820px}}
      .card{{border:1px solid #ddd;border-radius:10px;padding:1rem;margin:1rem 0}}
      button{{margin-right:.5rem;padding:.4rem .8rem;border-radius:6px;cursor:pointer}}
      code{{background:#f3f3f3;padding:2px 4px;border-radius:4px}}
    </style></head>
    <body>
      <h1>Agentic SDLC — Governance</h1>
      <p>Run: <code>{store.run_id}</code></p>
      <h2>Pending Approvals</h2>{rows}
      <h2>Reliability Metrics</h2><ul>{metric_html}</ul>
      <p><a href="/audit">View audit log</a></p>
    </body></html>"""


@app.post("/decide")
def decide_route(approval: str, decision: str):
    store = _store()
    if store:
        decide(store, approval, decision, by="dashboard-user")
    return RedirectResponse("/", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
def audit():
    store = _store()
    if not store:
        return "no runs"
    log = (store.root / "audit.log")
    lines = log.read_text().splitlines() if log.exists() else []
    body = "<br>".join(l for l in lines[-200:])
    return f"<h2>Audit log ({store.run_id})</h2><pre style='white-space:pre-wrap'>{body}</pre>"


def main():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
