#!/usr/bin/env python3
"""Capture a logged-in browser session for the school agent. Run this on YOUR OWN computer (it opens a real window).

    pip install playwright && playwright install chromium
    python capture_session.py https://wcpss.instructure.com  [--google-doc URL ...]

You log in yourself (school SSO, MFA, Google account). The script never sees or stores your password; it saves only the
browser's cookies/storage to a private file. Copy that file to the server, e.g.:
    scp canvas_state.json server1:~/.config/school-agent/canvas_state.json   (then chmod 600 it)
and set CANVAS_BROWSER_STATE=/home/server1/.config/school-agent/canvas_state.json
Sessions expire; when the agent alerts 'Canvas browser session expired', run this again."""
import argparse, os, sys


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('canvas_url'); ap.add_argument('--out', default='canvas_state.json')
    ap.add_argument('--google-doc', action='append', default=[], help='a private Google Doc link from your courses, so the Google login is captured too')
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(a.canvas_url)
        input('\n1) Log in to Canvas in the opened window (finish any MFA).\n   When you can see your dashboard, press Enter here... ')
        for link in a.google_doc:
            page.goto(link)
            input(f'2) If Google asks you to sign in, do so with your school account, until the document opens.\n   Press Enter here... ')
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        ctx.storage_state(path=a.out)
        os.chmod(a.out, 0o600)
        browser.close()
    print(f'\nSaved session to {a.out} (mode 600). Keep it private; do not commit it.')


if __name__ == '__main__':
    sys.exit(main())
