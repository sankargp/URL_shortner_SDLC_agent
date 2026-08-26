"""Database-backed Governance backlog, lifecycle controls, and approvals."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from html import escape
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.parse import quote, urlparse

import uvicorn
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from agents.requirements import analyze_requirement
from orchestrator.cli import _prepare_run, rehydrate_run
from orchestrator.context import RunStore, latest_run
from orchestrator.gates import decide, pending_approvals
from orchestrator.kernel import Kernel
from orchestrator.requirements_store import (
    AnalysisStatus,
    AuthoringStatus,
    ExecutionStatus,
    RequirementConflict,
    RequirementNotFound,
    RequirementRecord,
    RequirementsRepository,
)
from orchestrator.source_control import RepositoryConfig, SourceControlError
from orchestrator.state import NodeState

app = FastAPI(title="Agentic SDLC Governance Dashboard")
WORKSPACE = os.getenv("WORKSPACE_DIR", "workspace")
REQUIREMENTS_DIR = Path("workspace/requirements")
_DECISION_LOCK = Lock()
_IMPLEMENT_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.getenv("IMPLEMENT_WORKERS", "2"))),
    thread_name_prefix="implementation",
)

THEME_CSS = """
      :root {
        color-scheme: light;
        --canvas: #f5f5f3;
        --surface: #ffffff;
        --surface-muted: #f8f8f7;
        --ink: #1d252d;
        --ink-muted: #65707b;
        --line: #dedfdd;
        --line-strong: #c8cac7;
        --accent: #334c5e;
        --accent-hover: #253c4c;
        --accent-soft: #e8eef1;
        --danger: #9b3b35;
        --danger-soft: #f8eae8;
        --focus: #2f6f9f;
        --shadow: 0 1px 2px rgba(20, 27, 32, .04), 0 10px 30px rgba(20, 27, 32, .04);
      }

      * { box-sizing: border-box; }
      html { background: var(--canvas); }

      body {
        margin: 0;
        min-width: 320px;
        min-height: 100vh;
        background: var(--canvas);
        color: var(--ink);
        font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-size: 15px;
        line-height: 1.55;
        -webkit-font-smoothing: antialiased;
      }

      a {
        color: var(--accent);
        font-weight: 650;
        text-decoration-color: #aebbc2;
        text-underline-offset: .2em;
      }
      a:hover { color: var(--accent-hover); }

      .masthead {
        border-bottom: 1px solid var(--line);
        background: rgba(255, 255, 255, .84);
        backdrop-filter: blur(14px);
      }
      .masthead-inner {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 14px;
        max-width: 1180px;
        margin: 0 auto;
        padding: 20px 24px;
      }
      .brand-lockup {
        display: flex;
        min-width: 0;
        align-items: center;
        gap: 14px;
      }
      .brand-mark {
        display: grid;
        width: 38px;
        height: 38px;
        flex: 0 0 auto;
        place-items: center;
        border-radius: 10px;
        background: var(--ink);
        color: #fff;
        font-size: 11px;
        font-weight: 800;
        letter-spacing: .08em;
      }
      .brand-icon {
        width: 21px;
        height: 21px;
        stroke: currentColor;
      }
      .masthead h1 {
        margin: 0;
        font-size: clamp(1.15rem, 2vw, 1.35rem);
        font-weight: 720;
        letter-spacing: -.025em;
        line-height: 1.2;
      }
      .masthead p {
        margin: 3px 0 0;
        color: var(--ink-muted);
        font-size: .84rem;
      }
      .service-nav {
        display: flex;
        flex: 0 0 auto;
        align-items: center;
        gap: 8px;
        margin-left: auto;
      }
      .service-nav a {
        border: 1px solid var(--line);
        border-radius: 8px;
        background: var(--surface);
        padding: 7px 10px;
        color: #46515b;
        font-size: .76rem;
        text-decoration: none;
      }
      .service-nav a:hover {
        border-color: var(--line-strong);
        background: var(--surface-muted);
        color: var(--ink);
      }

      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        clip-path: inset(50%);
      }

      .app-shell {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 300px;
        gap: 28px;
        width: min(1180px, 100%);
        margin: 0 auto;
        padding: 38px 24px 64px;
      }
      .backlog:only-child { grid-column: 1 / -1; }
      .section-heading {
        display: flex;
        align-items: end;
        justify-content: space-between;
        gap: 20px;
        margin: 36px 2px 14px;
      }
      .section-heading h2,
      .run-panel h2,
      .audit-shell h2 {
        margin: 0;
        font-size: 1.05rem;
        font-weight: 720;
        letter-spacing: -.015em;
      }
      .eyebrow {
        margin: 0 0 5px;
        color: var(--ink-muted);
        font-size: .7rem;
        font-weight: 760;
        letter-spacing: .12em;
        text-transform: uppercase;
      }
      .count { color: var(--ink-muted); font-size: .78rem; white-space: nowrap; }

      .card,
      .create,
      .run-panel {
        overflow: hidden;
        border: 1px solid var(--line);
        border-radius: 14px;
        background: var(--surface);
        box-shadow: var(--shadow);
      }
      .card { margin: 10px 0; }
      .create { margin: 0; border-style: dashed; box-shadow: none; }
      .card:hover { border-color: var(--line-strong); }

      summary {
        display: flex;
        min-height: 60px;
        align-items: center;
        gap: 14px;
        padding: 17px 20px;
        cursor: pointer;
        list-style: none;
        user-select: none;
      }
      summary::-webkit-details-marker { display: none; }
      summary::after {
        content: "+";
        display: grid;
        width: 28px;
        height: 28px;
        margin-left: auto;
        flex: 0 0 auto;
        place-items: center;
        border-radius: 50%;
        background: var(--surface-muted);
        color: var(--ink-muted);
        font-size: 1rem;
        font-weight: 500;
        transition: transform .18s ease, background .18s ease;
      }
      details[open] > summary {
        border-bottom: 1px solid var(--line);
        background: var(--surface-muted);
      }
      details[open] > summary::after { content: "-"; transform: none; }
      summary h2, summary h3 { margin: 0; }
      summary h2 { font-size: .98rem; }
      summary h3 { font-size: .94rem; font-weight: 680; letter-spacing: -.01em; }
      summary:focus-visible {
        outline: 3px solid #9fc3dc;
        outline: 3px solid color-mix(in srgb, var(--focus) 38%, transparent);
        outline-offset: -3px;
      }

      .create form { padding: 4px 20px 22px; }
      .card > :not(summary) { margin-right: 20px; margin-left: 20px; }
      .card > p { color: #3f4952; }
      .card > h4 { margin-top: 24px; margin-bottom: 7px; }
      .card > ul { padding-left: 19px; }
      .card[open] { padding-bottom: 20px; }
      .approval {
        margin-top: 20px !important;
        padding: 17px;
        border: 1px solid #e7d7bd;
        border-radius: 10px;
        background: #fbf7ef;
      }
      .badge {
        display: inline-flex;
        align-items: center;
        border: 1px solid #d5dee2;
        border-radius: 999px;
        background: var(--accent-soft);
        color: var(--accent);
        padding: 3px 9px;
        font-size: .73rem;
        font-weight: 720;
        letter-spacing: .015em;
      }
      .empty { color: var(--ink-muted); font-style: italic; }
      .analysis-error { color: var(--danger); }
      .inline { display: inline-block; margin: 4px 6px 4px 0; }
      .actions { margin-top: 24px !important; }

      label {
        display: block;
        margin: 15px 0 6px;
        color: #3f4952;
        font-size: .8rem;
        font-weight: 680;
      }
      input, select, textarea {
        width: 100%;
        border: 1px solid var(--line-strong);
        border-radius: 9px;
        background: var(--surface);
        color: var(--ink);
        font: inherit;
        outline: none;
        padding: 10px 12px;
        transition: border-color .15s ease, box-shadow .15s ease;
      }
      input:focus, select:focus, textarea:focus {
        border-color: var(--focus);
        box-shadow: 0 0 0 3px rgba(47, 111, 159, .15);
        box-shadow: 0 0 0 3px color-mix(in srgb, var(--focus) 15%, transparent);
      }
      textarea { min-height: 84px; resize: vertical; }

      button {
        border: 1px solid var(--ink);
        border-radius: 8px;
        background: var(--ink);
        color: #fff;
        cursor: pointer;
        font: inherit;
        font-size: .8rem;
        font-weight: 680;
        padding: 8px 13px;
        transition: background .15s ease, border-color .15s ease, transform .15s ease;
      }
      button:hover { border-color: var(--accent-hover); background: var(--accent-hover); }
      button:active { transform: translateY(1px); }
      button:focus-visible { outline: 3px solid #9fc3dc; outline-offset: 2px; }
      button.secondary { border-color: var(--line-strong); background: var(--surface); color: var(--ink); }
      button.secondary:hover { border-color: #aeb2af; background: var(--surface-muted); }
      button.danger { border-color: #e4c5c1; background: var(--danger-soft); color: var(--danger); }
      button.danger:hover { border-color: #d3a29c; background: #f2dcd9; }

      code {
        border: 1px solid var(--line);
        border-radius: 5px;
        background: var(--surface-muted);
        color: #48525b;
        padding: 2px 5px;
        font-size: .82em;
      }

      .run-panel {
        position: sticky;
        top: 24px;
        align-self: start;
        padding: 20px;
        box-shadow: none;
      }
      .run-panel .run-id { margin: 10px 0 22px; color: var(--ink-muted); }
      .metrics { margin: 12px 0 20px; padding: 0; list-style: none; }
      .metrics li {
        display: flex;
        justify-content: space-between;
        gap: 14px;
        padding: 8px 0;
        border-bottom: 1px solid #ececea;
        font-size: .78rem;
      }
      .metrics li b { color: var(--ink-muted); font-weight: 590; }
      .run-panel > p:last-child { margin-bottom: 0; }

      .audit-shell { width: min(1000px, calc(100% - 48px)); margin: 38px auto; }
      .audit-shell pre {
        overflow: auto;
        margin-top: 16px;
        border: 1px solid var(--line);
        border-radius: 14px;
        background: var(--surface);
        padding: 20px;
        color: #38434d;
        font-size: .82rem;
        line-height: 1.65;
        box-shadow: var(--shadow);
        white-space: pre-wrap;
      }

      @media (max-width: 860px) {
        .app-shell { grid-template-columns: 1fr; }
        .run-panel { position: static; grid-row: 1; }
      }
      @media (max-width: 600px) {
        .masthead-inner { align-items: stretch; padding: 16px; }
        .brand-lockup { flex: 1 1 100%; }
        .service-nav { flex: 1 1 100%; margin-left: 52px; }
        .app-shell { gap: 20px; padding: 26px 14px 48px; }
        .section-heading { align-items: start; margin-top: 30px; }
        summary { padding: 15px 16px; }
        .card > :not(summary) { margin-right: 16px; margin-left: 16px; }
        .create form { padding-right: 16px; padding-left: 16px; }
        .count { display: none; }
      }
      @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
      }
"""

APPROVAL_SCRIPT = """
      (() => {
        const formSelector = "form[data-approval-form]";

        const fallbackUrl = (card) => {
          const requirementId = card?.dataset.requirementId;
          if (!requirementId) return "/";
          const encoded = encodeURIComponent(requirementId);
          return `/?focus=${encoded}#requirement-${encoded}`;
        };

        document.addEventListener("submit", async (event) => {
          const form = event.target.closest(formSelector);
          if (!form) return;
          if (form.dataset.submitting === "true") {
            event.preventDefault();
            return;
          }

          const submitter = event.submitter;
          if (!submitter || submitter.name !== "decision") return;
          event.preventDefault();

          const card = form.closest("details.card");
          const recoveryUrl = fallbackUrl(card);
          const originalTop = card.getBoundingClientRect().top;
          const buttons = [...form.querySelectorAll("button")];
          const status = document.getElementById("approval-status");

          form.dataset.submitting = "true";
          form.setAttribute("aria-busy", "true");
          buttons.forEach((button) => { button.disabled = true; });
          submitter.textContent = submitter.value === "approve" ? "Approving..." : "Rejecting...";
          if (status) status.textContent = "Recording approval decision.";

          try {
            const payload = new FormData(form);
            payload.set(submitter.name, submitter.value);
            const response = await fetch(form.action, {
              method: "POST",
              body: payload,
              headers: {"X-Requested-With": "fetch"},
            });
            if (!response.ok) throw new Error(`Decision failed with ${response.status}`);

            const nextDocument = new DOMParser().parseFromString(
              await response.text(),
              "text/html",
            );
            const currentShell = document.querySelector("main.app-shell");
            const nextShell = nextDocument.querySelector("main.app-shell");
            const requirementId = card.dataset.requirementId;
            const nextCard = nextDocument.getElementById(`requirement-${requirementId}`);
            if (!currentShell || !nextShell || !nextCard) {
              throw new Error("Updated requirement could not be rendered");
            }

            nextCard.open = true;
            currentShell.replaceWith(nextShell);

            const updatedCard = document.getElementById(`requirement-${requirementId}`);
            const updatedSummary = updatedCard?.querySelector(":scope > summary");
            if (!updatedCard || !updatedSummary) {
              throw new Error("Updated requirement could not be focused");
            }
            window.scrollBy(0, updatedCard.getBoundingClientRect().top - originalTop);
            updatedSummary.focus({preventScroll: true});
            window.history.replaceState(null, "", recoveryUrl);
            if (status) status.textContent = "Approval decision recorded.";
          } catch (error) {
            window.location.assign(recoveryUrl);
          }
        });
      })();
"""


@lru_cache(maxsize=8)
def _repository_for(workspace: str, seed_dir: str) -> RequirementsRepository:
    return RequirementsRepository(workspace, seed_dir=seed_dir)


def requirements_repository() -> RequirementsRepository:
    return _repository_for(WORKSPACE, str(REQUIREMENTS_DIR))


def _store() -> RunStore | None:
    run_id = latest_run(WORKSPACE)
    return RunStore(run_id, WORKSPACE) if run_id else None


def _store_for_run(run_id: str) -> RunStore:
    """Return an existing direct child of workspace/runs, never an arbitrary path."""
    runs_root = (Path(WORKSPACE) / "runs").resolve()
    candidate = (runs_root / run_id).resolve()
    if candidate.parent != runs_root or not candidate.is_dir() or not (candidate / "state.json").is_file():
        raise HTTPException(status_code=404, detail="Run not found")
    return RunStore(run_id, WORKSPACE)


def _html(value: Any) -> str:
    return escape(str(value), quote=True)


def _render_items(items: list[Any]) -> str:
    if not items:
        return '<p class="empty">None</p>'
    return "<ul>" + "".join(f"<li>{_html(item)}</li>" for item in items) + "</ul>"


def _display(value: str) -> str:
    return value.replace("_", " ").capitalize()


def _priority(record: RequirementRecord) -> tuple[int, int]:
    if record.authoring_status == AuthoringStatus.ARCHIVED:
        order = 6
    elif record.execution_status == ExecutionStatus.AWAITING_APPROVAL:
        order = 0
    elif record.execution_status == ExecutionStatus.IN_PROGRESS:
        order = 1
    elif (
        record.authoring_status == AuthoringStatus.READY
        and record.execution_status == ExecutionStatus.NOT_STARTED
    ):
        order = 2
    elif record.authoring_status == AuthoringStatus.DRAFT:
        order = 3
    elif record.execution_status == ExecutionStatus.STOPPED:
        order = 4
    elif record.execution_status == ExecutionStatus.IMPLEMENTED:
        order = 5
    else:
        order = 7
    return order, record.id


def _approvals_for(record: RequirementRecord) -> list[dict[str, Any]]:
    if (
        record.execution_status != ExecutionStatus.AWAITING_APPROVAL
        or not record.current_run_id
    ):
        return []
    try:
        return pending_approvals(_store_for_run(record.current_run_id))
    except (HTTPException, OSError, json.JSONDecodeError):
        return []


def _publication_for(record: RequirementRecord) -> dict[str, Any]:
    if not record.current_run_id:
        return {}
    try:
        context = _store_for_run(record.current_run_id).read_context()
    except (HTTPException, OSError, json.JSONDecodeError):
        return {}
    return context.get("publication") or {}


def _render_publication(record: RequirementRecord) -> str:
    publication = _publication_for(record)
    pr_url = str(publication.get("pr_url") or "")
    parsed_url = urlparse(pr_url)
    trusted_pr_url = parsed_url.scheme == "https" and parsed_url.hostname == "github.com"
    if publication.get("outcome") == "pr_opened" and trusted_pr_url:
        return (
            '<p><b>Delivery:</b> '
            f'<a href="{_html(pr_url)}" target="_blank" '
            f'rel="noopener noreferrer">Pull request '
            f'#{_html(publication.get("pr_number", ""))}</a></p>'
        )
    if publication.get("outcome") == "no_changes":
        return '<p><b>Delivery:</b> No changes required; no pull request was created.</p>'
    return ""


def _render_analysis(record: RequirementRecord) -> str:
    if record.analysis_status == AnalysisStatus.FAILED:
        content = (
            '<p class="analysis-error">Analysis failed: '
            f"{_html(record.analysis_error or 'Unknown error')}</p>"
        )
    elif record.analysis_status == AnalysisStatus.READY and record.analysis:
        analysis = record.analysis
        content = f"""
          <p><b>Ambiguity verdict:</b> {'Ambiguous' if analysis.get('ambiguous') else 'Clear'}</p>
          <h4>Analyzed acceptance criteria</h4>
          {_render_items(analysis.get('acceptance') or [])}
          <h4>Ambiguity details</h4>
          {_render_items(analysis.get('ambiguities') or [])}
          <h4>Analyzed interpretations</h4>
          {_render_items(analysis.get('interpretations') or [])}
        """
    else:
        content = '<p class="empty">Analysis not requested.</p>'
    if record.execution_status in {
        ExecutionStatus.IN_PROGRESS,
        ExecutionStatus.AWAITING_APPROVAL,
    }:
        return f'{content}<p class="empty">Analysis is locked while this run is active.</p>'
    label = "Retry analysis" if record.analysis_status == AnalysisStatus.FAILED else "Analyze"
    return f"""
      {content}
      <form class="inline" action="/requirements/{_html(record.requirement_id)}/analyze" method="post">
        <button>{label}</button>
      </form>
    """


def _render_actions(record: RequirementRecord) -> str:
    actions: list[tuple[str, str]] = []
    if record.authoring_status == AuthoringStatus.DRAFT:
        actions.append(("ready", "Mark ready"))
    if (
        record.authoring_status == AuthoringStatus.READY
        and record.execution_status in {
            ExecutionStatus.NOT_STARTED,
            ExecutionStatus.STOPPED,
        }
        and record.analysis_status == AnalysisStatus.READY
    ):
        label = (
            "Retry implementation"
            if record.execution_status == ExecutionStatus.STOPPED
            else "Implement"
        )
        actions.append(("implement", label))
    if record.authoring_status == AuthoringStatus.ARCHIVED:
        actions.append(("restore", "Restore as draft"))
    elif record.execution_status not in {
        ExecutionStatus.IN_PROGRESS,
        ExecutionStatus.AWAITING_APPROVAL,
    }:
        actions.append(("archive", "Archive"))
    rendered = []
    for action, label in actions:
        button_class = "" if action == "implement" else ' class="secondary"'
        rendered.append(
            f'<form class="inline" action="/requirements/{_html(record.requirement_id)}/{action}" '
            f'method="post"><button{button_class}>{label}</button></form>'
        )
    return "".join(rendered)


def _render_card(record: RequirementRecord, *, focused: bool = False) -> str:
    approvals_html = ""
    for approval in _approvals_for(record):
        approvals_html += f"""
          <div class="approval">
            <p><b>{_html(approval.get('id', 'Unknown approval'))}</b> · node
               <code>{_html(approval.get('node', 'unknown'))}</code> · impact
               {_html(approval.get('impact', 'unknown'))}</p>
            <p><b>Approval question:</b> {_html(approval.get('question', ''))}</p>
            <form data-approval-form action="/decide" method="post">
              <input type="hidden" name="run_id" value="{_html(record.current_run_id)}">
              <input type="hidden" name="approval" value="{_html(approval.get('id', ''))}">
              <button name="decision" value="approve">Approve</button>
              <button class="danger" name="decision" value="reject">Reject</button>
            </form>
          </div>
        """
    run_html = (
        f" · Run <code>{_html(record.current_run_id)}</code>" if record.current_run_id else ""
    )
    open_attribute = " open" if focused else ""
    requirement_id = _html(record.requirement_id)
    return f"""
      <details class="card" id="requirement-{requirement_id}" data-requirement-id="{requirement_id}"{open_attribute}>
        <summary><h3>{_html(record.requirement_id)} · {_html(record.title)}</h3></summary>
        <p><b>Identity:</b> {_html(record.requirement_type)} requirement</p>
        <p><span class="badge">{_html(_display(record.execution_status.value))}</span>{run_html}</p>
        <p><b>Source status:</b> {_html(_display(record.authoring_status.value))}
           · <b>Run state:</b> {_html(_display(record.execution_status.value))}</p>
        <p><b>Intent:</b> {_html(record.intent)}</p>
        <h4>Acceptance criteria</h4>{_render_items(record.acceptance)}
        <h4>Constraints</h4>{_render_items(record.constraints)}
        <h4>Possible interpretations</h4>{_render_items(record.possible_interpretations)}
        <h4>LLM analysis</h4>{_render_analysis(record)}
        {_render_publication(record)}
        <div class="actions">{_render_actions(record)}</div>
        {approvals_html}
      </details>
    """


def _redirect_home(requirement_id: str | None = None) -> RedirectResponse:
    if requirement_id is None:
        return RedirectResponse("/", status_code=303)
    encoded = quote(requirement_id, safe="")
    return RedirectResponse(
        f"/?focus={encoded}#requirement-{encoded}",
        status_code=303,
    )


def _raise_repository_error(exc: Exception) -> None:
    if isinstance(exc, RequirementNotFound):
        raise HTTPException(status_code=404, detail="Requirement not found") from exc
    if isinstance(exc, RequirementConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@app.get("/", response_class=HTMLResponse)
def home(focus: str | None = None):
    records = sorted(requirements_repository().list_requirements(), key=_priority)
    rows = "".join(
        _render_card(record, focused=record.requirement_id == focus) for record in records
    )
    if not rows:
        rows = "<p>No requirements have been added.</p>"

    latest_html = ""
    store = _store()
    if store:
        metrics_path = store.root / "metrics.json"
        try:
            metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            metrics = {}
        metric_html = "".join(
            f"<li><b>{_html(key)}</b>: {_html(value)}</li>" for key, value in metrics.items()
        )
        latest_html = f"""
          <aside class="run-panel" aria-label="Latest run summary">
            <p class="eyebrow">Current activity</p>
            <h2>Latest Run</h2>
            <p class="run-id">Run <code>{_html(store.run_id)}</code></p>
            <h2>Reliability Metrics</h2>
            <ul class="metrics">{metric_html}</ul>
            <p><a href="/audit">View audit log</a></p>
          </aside>
        """

    return f"""
    <!doctype html>
    <html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Agentic SDLC Governance</title>
    <style>{THEME_CSS}</style></head>
    <body>
      <header class="masthead">
        <div class="masthead-inner">
          <div class="brand-lockup">
            <div class="brand-mark" aria-hidden="true">
              <svg class="brand-icon" viewBox="0 0 24 24" fill="none">
                <path d="M12 3 19 6v5c0 4.8-2.9 8.1-7 10-4.1-1.9-7-5.2-7-10V6l7-3Z"
                      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
                <path d="m8.8 12.1 2.1 2.1 4.5-4.6"
                      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </div>
            <div>
              <h1>Agentic SDLC Governance</h1>
              <p>Requirements, approvals, and delivery oversight</p>
            </div>
          </div>
          <nav class="service-nav" aria-label="Service links">
            <a href="http://localhost:8080/docs" target="_blank" rel="noopener noreferrer">Swagger</a>
            <a href="http://localhost:8080" target="_blank" rel="noopener noreferrer">URL shortener</a>
          </nav>
        </div>
      </header>
      <p class="sr-only" id="approval-status" aria-live="polite"></p>
      <main class="app-shell">
        <section class="backlog" aria-labelledby="backlog-heading">
          <details class="create">
            <summary><h2>Add requirement</h2></summary>
            <form action="/requirements" method="post">
              <label for="requirement-type">Type</label>
              <select id="requirement-type" name="requirement_type" required>
                <option value="greenfield">Greenfield</option>
                <option value="brownfield">Brownfield</option>
                <option value="ambiguous">Ambiguous</option>
              </select>
              <label for="requirement-title">Title</label>
              <input id="requirement-title" name="title" required>
              <label for="requirement-intent">Intent</label>
              <textarea id="requirement-intent" name="intent" required></textarea>
              <label for="requirement-acceptance">Acceptance criteria (one per line)</label>
              <textarea id="requirement-acceptance" name="acceptance"></textarea>
              <label for="requirement-constraints">Constraints (one per line)</label>
              <textarea id="requirement-constraints" name="constraints"></textarea>
              <label for="requirement-interpretations">Possible interpretations (one per line)</label>
              <textarea id="requirement-interpretations" name="interpretations"></textarea>
              <button>Add requirement</button>
            </form>
          </details>
          <div class="section-heading">
            <div>
              <p class="eyebrow">Governed delivery</p>
              <h2 id="backlog-heading">Requirements backlog</h2>
            </div>
            <span class="count">{len(records)} requirement{'s' if len(records) != 1 else ''}</span>
          </div>
          {rows}
        </section>
        {latest_html}
      </main>
      <script data-approval-enhancement>{APPROVAL_SCRIPT}</script>
    </body></html>"""


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


@app.post("/requirements")
def create_requirement(
    requirement_type: str = Form(...),
    title: str = Form(...),
    intent: str = Form(...),
    acceptance: str = Form(""),
    constraints: str = Form(""),
    interpretations: str = Form(""),
):
    try:
        requirements_repository().create(
            requirement_type=requirement_type,
            title=title,
            intent=intent,
            acceptance=_lines(acceptance),
            constraints=_lines(constraints),
            possible_interpretations=_lines(interpretations),
        )
    except (RequirementNotFound, RequirementConflict, ValueError) as exc:
        _raise_repository_error(exc)
    return _redirect_home()


def _transition_requirement(requirement_id: str, target: AuthoringStatus) -> RedirectResponse:
    try:
        requirements_repository().transition_authoring(requirement_id, target)
    except (RequirementNotFound, RequirementConflict, ValueError) as exc:
        _raise_repository_error(exc)
    return _redirect_home()


@app.post("/requirements/{requirement_id}/ready")
def mark_ready(requirement_id: str):
    return _transition_requirement(requirement_id, AuthoringStatus.READY)


@app.post("/requirements/{requirement_id}/archive")
def archive(requirement_id: str):
    return _transition_requirement(requirement_id, AuthoringStatus.ARCHIVED)


@app.post("/requirements/{requirement_id}/restore")
def restore(requirement_id: str):
    return _transition_requirement(requirement_id, AuthoringStatus.DRAFT)


@app.post("/requirements/{requirement_id}/analyze")
def analyze(requirement_id: str):
    repository = requirements_repository()
    try:
        record = repository.get(requirement_id)
    except RequirementNotFound as exc:
        _raise_repository_error(exc)
    if record.execution_status in {
        ExecutionStatus.IN_PROGRESS,
        ExecutionStatus.AWAITING_APPROVAL,
    }:
        raise HTTPException(status_code=409, detail="Analysis is locked during active execution")
    try:
        analysis = analyze_requirement(record.to_requirement_dict())
    except Exception as exc:  # noqa: BLE001 - provider failure is persisted for retry
        try:
            repository.record_analysis(
                requirement_id,
                error=str(exc),
                expected_revision=record.revision,
            )
        except (RequirementNotFound, RequirementConflict, ValueError) as persistence_exc:
            _raise_repository_error(persistence_exc)
    else:
        try:
            repository.record_analysis(
                requirement_id,
                analysis=analysis,
                expected_revision=record.revision,
            )
        except (RequirementNotFound, RequirementConflict, ValueError) as exc:
            _raise_repository_error(exc)
    return _redirect_home()


@app.post("/requirements/{requirement_id}/implement")
def implement(requirement_id: str):
    repository = requirements_repository()
    try:
        record = repository.get(requirement_id)
    except RequirementNotFound as exc:
        _raise_repository_error(exc)
    if not (
        record.authoring_status == AuthoringStatus.READY
        and record.execution_status in {
            ExecutionStatus.NOT_STARTED,
            ExecutionStatus.STOPPED,
        }
        and record.analysis_status == AnalysisStatus.READY
    ):
        raise HTTPException(
            status_code=409,
            detail="Requirement must be ready, analyzed, and inactive",
        )
    try:
        RepositoryConfig.discover()
    except SourceControlError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        run, store, run_repository = _prepare_run(requirement_id, publish_changes=True)
    except (RequirementNotFound, RequirementConflict, ValueError) as exc:
        _raise_repository_error(exc)
    try:
        _IMPLEMENT_EXECUTOR.submit(_run_implementation, run, store, run_repository)
    except RuntimeError as exc:
        store.audit("background_submission_failed", error=type(exc).__name__)
        run_repository.sync_execution(requirement_id, run.id, ExecutionStatus.STOPPED)
        raise HTTPException(status_code=503, detail="Implementation worker is unavailable") from exc
    return _redirect_home()


def _run_implementation(run, store: RunStore, repository: RequirementsRepository) -> None:
    try:
        Kernel(
            run,
            store,
            dict(os.environ),
            requirements_repository=repository,
        ).run_until_blocked()
    except Exception as exc:  # noqa: BLE001 - persist failures at the worker boundary
        store.audit("background_run_failed", error=type(exc).__name__)
        try:
            repository.sync_execution(run.requirement_id, run.id, ExecutionStatus.STOPPED)
        except RequirementConflict:
            store.audit("background_failure_status_stale", requirement=run.requirement_id)


@app.post("/decide")
def decide_route(
    run_id: str = Form(...),
    approval: str = Form(...),
    decision: Literal["approve", "reject"] = Form(...),
):
    with _DECISION_LOCK:
        store = _store_for_run(run_id)
        approvals_root = store.approvals.resolve()
        approval_path = (approvals_root / f"{approval}.json").resolve()
        if approval_path.parent != approvals_root or not approval_path.is_file():
            raise HTTPException(status_code=404, detail="Approval not found")
        try:
            approval_data = json.loads(approval_path.read_text())
            state = json.loads((store.root / "state.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if approval_data.get("status") != "pending":
            raise HTTPException(status_code=409, detail="Approval is no longer pending")

        repository = requirements_repository()
        try:
            record = repository.get(str(state["requirement_id"]))
        except (KeyError, RequirementNotFound) as exc:
            raise HTTPException(status_code=404, detail="Requirement not found") from exc
        if (
            record.current_run_id != run_id
            or record.execution_status != ExecutionStatus.AWAITING_APPROVAL
        ):
            raise HTTPException(status_code=409, detail="Run is no longer current")

        run, hydrated_store = rehydrate_run(run_id)
        if hydrated_store.root.resolve() != store.root.resolve():
            raise HTTPException(status_code=404, detail="Run not found")
        node = run.nodes.get(approval_data.get("node"))
        if not node or node.state != NodeState.AWAITING_APPROVAL:
            raise HTTPException(status_code=409, detail="Run is not awaiting this approval")

        decide(store, approval, decision, by="dashboard-user")
        Kernel(
            run,
            hydrated_store,
            dict(os.environ),
            requirements_repository=repository,
        ).resume()
    return _redirect_home(record.requirement_id)


@app.get("/audit", response_class=HTMLResponse)
def audit():
    store = _store()
    if not store:
        return "no runs"
    log = store.root / "audit.log"
    lines = log.read_text().splitlines() if log.exists() else []
    body = "\n".join(_html(line) for line in lines[-200:])
    return f"""
      <!doctype html>
      <html lang="en"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Audit log · Agentic SDLC Governance</title>
      <style>{THEME_CSS}</style></head>
      <body>
        <main class="audit-shell">
          <p class="eyebrow">Execution history</p>
          <h2>Audit log ({_html(store.run_id)})</h2>
          <p><a href="/">Back to requirements</a></p>
          <pre>{body}</pre>
        </main>
      </body></html>
    """


def main():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
