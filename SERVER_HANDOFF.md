# Server Deployment Handoff

You are the implementation agent operating on my Ubuntu server. Your task is to turn this repository into a reliable, unattended Canvas academic planning service. Do not make arbitrary architectural rewrites; inspect the repo and existing environment first.

**First inspect** the current Canvas → Google Calendar → Todoist sync, current server browser automation, credential storage, user services/timers, and connected account permissions. Integrate with them rather than duplicate work or existing calendar events.

**Execution plan:**

1. Clone the repository URL I provide. Read `README.md`, `.env.example`, the package, tests, and deployment files.
2. Inspect how existing browser sessions are managed. Implement a browser exporter conforming to README's JSON contract, with Canvas login, traversal of all active courses and authorized links. No manual pasted course links, no MFA bypass.
3. Test Canvas API access first; use browser fallback for resources inaccessible via API. Collect files, linked Docs, pages, announcements, modules, and assignment details.
4. Configure AI extraction and a review/report workflow. Validate extracted dates against the originating source and retain URLs and evidence.
5. Implement cross-source duplicate resolution and read-only Canvas-calendar collision checking. Preserve user's manually edited calendar events and prevent duplicating events already displayed through the Canvas subscription or Todoist calendar overlay.
6. Configure secure OAuth refresh and the Todoist API. Run read-only/dry-run and show a sample report. Do not enable live writes until authorized and duplicates are checked.
7. Run unit and integration tests, validate systemd timer syntax with `systemd-analyze calendar`, enable the exact schedule at 05:00, 12:00, and 14:00–22:00 America/New_York (daily).
8. Verify retry, logging, error alerts, DST, browser session expiry, and idempotence. Document commands and any limitations.

**Non-negotiables:** no secrets in git, no automatic assignment submission or teacher communication, no made-up dates, no duplicate calendar entries, and no unsanctioned browser authentication bypass. Request approval before destructive actions or enabling writes.

**Deliverable:** provide a concise deployment report showing services, schedules, last scan results, sources scanned, new obligations, review queue, errors, and remaining limitations.
