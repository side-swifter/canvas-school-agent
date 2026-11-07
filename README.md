# School Course Agent

Self-hosted scanner that reads your authorized Canvas materials, extracts dated obligations, reconciles them across sources, and syncs **assignments → Todoist** and **tests/quizzes/events → a dedicated Google Calendar**. Defaults to **dry-run**; live writes need two separate opt-ins.

## How it works

```
Canvas API ─┐                       ┌─ structured dates (due_at, calendar events) ─┐
 syllabus, assignments, quizzes,    │                                              │
 modules(+items), pages,            ├─ documents → text (PDF/DOCX/HTML/Google Doc) │→ observations
 announcements, files, linked docs ─┤        → AI extraction (approved provider)   │   (per source,
Browser fallback (your session) ────┘        → validation against source text      ┘    with evidence+URL)
                                                         │
                       reconcile (cross-source clusters, reschedules, cancellations, conflicts)
                                                         │
                 obligations (persistent oid) ──► review queue (ambiguous / conflict / user-edited / unreadable)
                                                         │
                         sync: Todoist | Google Calendar  (dedupe, adopt, never overwrite user edits, never delete)
```

* **Discovery is automatic.** API: courses → syllabus, assignments, quizzes, modules and their page/file/external-doc items, pages, announcements, files, course calendar events, plus any Canvas file or public Google Doc *linked from* those. When the API returns 403/404 for a resource, or there is no usable token, the authenticated browser fallback visits those Canvas pages (see below). No links need to be supplied.
* **No invented dates.** An extracted item is accepted only if its quoted evidence exists verbatim in the source, the evidence itself states the day and month, and confidence ≥ `MIN_CONFIDENCE`. Everything else (relative dates like "next Friday", missing year/day, low confidence, model-flagged ambiguity, conflicts) goes to the **review queue** and raises an alert.
* **Cross-source identity.** Each obligation has a persistent `oid`, linked to every `(source, item)` that mentions it. "Unit 3 Test" in a PDF, a module page and a schedule table is one entry. Numbers are significant (`Quiz 1` ≠ `Quiz 2`). Renames and date changes keep the same `oid`, so remote entries are updated, not re-created. Reschedule/cancel notices supersede the older date; disagreements hold the item (previous remote value untouched) until you choose with `review approve`.
* **Canvas is authoritative for assignment dates** (`due_at`, converted to America/New_York); conflicting text is recorded in the item's notes.
* **Todoist rules.** Marker `agent-oid:<oid>` in the description. Before creating, existing tasks in *all* projects are searched (same course/title/due date) and adopted instead of duplicated (this also covers Canvas→Todoist integrations). Adopted or user-edited tasks are never modified; tasks you deleted/completed are never recreated. Assignments go to Todoist **only** (never the calendar), so Todoist's Google Calendar overlay can't produce duplicates.
* **Calendar rules.** One dedicated calendar, all-day events tagged with a private `agent_oid`. Lookup by that tag survives lost state. Same-day similar events on `GOOGLE_READONLY_CALENDAR_IDS` (Canvas feed, Todoist overlay) are treated as already present. Edited/deleted events are left alone. Cancellations are marked `[CANCELLED]`, never deleted.
* **Privacy gate.** Documents go to a model only if the endpoint is localhost or `AI_DATA_APPROVED=true`. Without that, structured Canvas dates (assignments, quiz/calendar events) still sync and everything else waits (`ai_pending` in the report plus an alert).
* **Reliability.** Retry with backoff + `Retry-After` on 408/429/5xx; per-source error isolation; incomplete collection never marks sources "missing"; scan lock; unchanged sources are not re-sent to the model; alerts (deduplicated 20h) go to `state/alerts.jsonl` and an optional webhook for: Canvas token rejected, browser session expired, Google re-auth needed, extraction paused, new review items, scan errors.

## Setup (nothing here enables writes)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'      # add ',browser' for the Playwright fallback
install -d -m 700 ~/.config/school-agent && cp .env.example ~/.config/school-agent/env && chmod 600 ~/.config/school-agent/env
.venv/bin/pytest -q                                              # 80 tests, no network/credentials needed
```

Authentication steps only you can do:

| Step | Command / action |
|---|---|
| Canvas token | Canvas → Account → Settings → *New Access Token*; put in `CANVAS_TOKEN`. (Or log in to Canvas in the server browser yourself, MFA included, and save Playwright `storage_state` to `CANVAS_BROWSER_STATE`.) |
| Model provider | Choose provider; set `AI_PROVIDER/AI_BASE_URL/AI_API_KEY/AI_MODEL`. Review its data policy, then set `AI_DATA_APPROVED=true`. |
| Todoist | Create a *test* project; set `TODOIST_API_TOKEN`, `TODOIST_PROJECT_ID`. |
| Google | Create an OAuth client (Desktop app) in your Google Cloud project, create a dedicated "School" calendar, set `GOOGLE_CALENDAR_ID`, run `school-agent auth-google` (offline refresh token → `~/.config/school-agent/google.json`, mode 600). Publish the consent screen ("In production") or tokens expire after 7 days. |

## Operating

```bash
school-agent scan                    # one scan (dry-run unless enabled below)
school-agent status                  # last report (state/last-report.json)
school-agent review list             # items needing a human decision
school-agent review approve 12 --date 2026-11-12   # supply/confirm a date; conflicts require --date
school-agent review reject 12
school-agent approve-live            # one-time, interactive; required before DRY_RUN=false does anything
```

Go-live checklist: run dry-runs → read `state/last-report.json` (every action shows `dedupe_checked: true`; `would-create` with `dedupe_checked: false` means that target wasn't connected so duplicates were **not** checked) → clear/approve the review queue → `approve-live` → set `DRY_RUN=false` with the *test* Todoist project and the dedicated calendar → inspect → install the timer.

### Schedule (not installed by this repo until you run the installer)

`deploy/school-agent.timer`: 05:00, 12:00, and 14:00–22:00 hourly, `America/New_York` (DST handled by systemd). Install with `scripts/install-server.sh`, then `sudo systemctl enable --now school-agent.timer`.

## Browser fallback
`browser.py` reuses a Playwright session you created; it never types credentials or handles MFA. A redirect to a login page raises `SessionExpired` → alert, and fallback scanning pauses until you log in again. Navigation is limited to the Canvas host and Google Docs/Drive. Documents from other hosts are ignored.

## Known limitations
* Playwright driver and the Canvas/Todoist/Google/model HTTP layers are tested with fakes, not live services; first live dry-run may surface API-shape differences (Todoist API v1 field names, Canvas customizations).
* Image-only PDFs are *reported* (`needs_ocr`), not OCR'd.
* Linked Google Docs must be link-shared; private ones fall back to the browser or the review queue. Other external sites are not followed.
* Times are not synced (all-day calendar entries / date-only tasks).
* "Study" suggestions are queued for approval, never auto-created. The agent never submits work or contacts teachers.
