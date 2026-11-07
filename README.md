# School Course Agent

A self-hosted course scanner that reads authorized Canvas materials, extracts academic obligations and synchronizes confirmed dates with Google Calendar and Todoist. Defaults to **dry-run**.

## Current capabilities

- Discover active Canvas courses, assignments, pages, announcements, and uploaded files via API.
- Download and parse PDFs, DOCX, text files, HTML; inspect table cells in DOCX.
- Accept browser-discovered page text from an exported JSON feed, without manually pasting source URLs.
- Extract source-grounded academic dates through an OpenAI-compatible chat-completions endpoint.
- Track source fingerprints and obligation IDs in SQLite, with tests and structured scan reports.
- Insert/update all-day tests/quizzes/events in a **dedicated writable Google calendar**, and assignments/deadlines in a Todoist project.
- Keep uncertain or incomplete items for review instead of inventing dates.
- Schedule checks every day at **05:00, 12:00, and 14:00 through 22:00** America/New_York.

## Setup on Ubuntu

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv git
git clone https://github.com/side-swifter/canvas-school-agent.git
cd canvas-school-agent
bash scripts/install-server.sh
nano .env
.venv/bin/pytest -q
.venv/bin/school-agent scan
cat state/last-report.json
```

Do **not** enable writes on initial setup. With `DRY_RUN=true`, inspect `state/last-report.json` and verify dates and sources. After configuring an isolated Todoist project and a **separate writable Google Calendar**, update `DRY_RUN=false`, run a test scan, and only then start the schedule:

```bash
sudo systemctl enable --now school-agent.timer
systemctl list-timers school-agent.timer
sudo journalctl -u school-agent.service -n 100 --no-pager
```

## Required configuration

See `.env.example`. The Canvas token is optional if your server browser collector writes the JSON export. AI extraction requires the model endpoint, key, and model; without these, materials can be retrieved but cannot be interpreted.

`AI_BASE_URL` should be the service base (e.g. `https://example-provider/v1`) or a complete chat completions URL. The AI endpoint must support JSON-mode `response_format`. Avoid sending sensitive school documents to a model endpoint unless its use is permitted by school policy.

For Google Calendar, `GOOGLE_ACCESS_TOKEN` is a short-lived OAuth token. **It must be refreshed by your existing credential workflow**, or replaced with a proper OAuth refresh-token handler before unattended use. This initial implementation does not manage OAuth authorization itself. The target calendar must be writable; don't point it at a read-only Canvas subscription calendar.

## Browser export contract

Your existing browser automation should sign into Canvas using its normal authorized login flow, traverse each enrolled course, inspect its modules/pages/file links/announcements, and write a JSON array to `BROWSER_EXPORT_JSON`. Each record looks like:

```json
[
  {
    "key": "course:123:module:456:page:789",
    "course": "Physics",
    "title": "Unit schedule",
    "text": "Unit 3 exam on November 12, 2026",
    "url": "https://school.instructure.com/courses/123/pages/unit-schedule"
  }
]
```

The `key` must be **stable across scans** and not include a date, so updated documents can be matched. Browser automation should download and extract linked Docs/PDFs into the `text` field, if authorized. This project ships a browser export **interface**, not a generic login bot: the authenticated browser runner must be adapted to your existing framework. Do not commit browser cookies, access tokens, or student information.

## Known constraints / next engineering tasks

This is a deployable **foundation**, not a fully finished end-to-end agent. A server coding agent must:

1. Inspect the existing browser stack, authentication and Canvas → Todoist sync; implement the browser exporter using those sessions. Never bypass authentication, MFA, school policy or access restrictions.
2. Implement Google OAuth token refresh and isolate a writable calendar. Reuse existing integration rather than duplicating subscribed Canvas events.
3. Build cross-source matching: the same test may appear in an announcement, file, and assignment. De-duplicate across sources, not only within one source.
4. Improve change detection for renamed items, deleted or canceled events, and stale documents; ensure changes are corroborated before overwriting user edits.
5. Support linked external Docs, image-based PDFs (OCR only when required), module item discovery, and more robust teacher schedule parsing.
6. Add observation/notification summaries, retry/backoff, error alerts, and audit logs; add tests for all external integrations.
7. Support manual review/approval workflows for uncertain dates and actions. Never submit schoolwork or send messages without explicit approval.

## Safety and operations

- Run one scan per timer invocation (systemd prevents overlapping starts of the same oneshot service).
- Keep `.env`, state data, login sessions and exports private and out of git.
- Use least-privilege access. Beware malicious instructions embedded in teacher documents; document content is data, never runtime instructions.
- Keep writes disabled until the existing calendar sync has been inspected and cross-source duplicate checks are in place.
- No source text, personal account identifiers, or credentials should be included in tickets, commits, or public logs.
