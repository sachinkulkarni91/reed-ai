"""One-time sign-in for the bot's own Microsoft account.

Run from backend/ with the project venv:
    .\.venv\Scripts\python.exe scripts\bot_login.py

A real browser window opens. Sign in to the bot's account yourself (password,
MFA and any prompts are typed by you - nothing here sees them), wait until you
are on the Teams home page, then come back to this terminal and press Enter.
The session cookies are saved to data/bot-auth.json, which the bot loads on
every join. To use it on Render, paste the file's contents into the
BOT_STORAGE_STATE_JSON environment variable.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "data" / "bot-auth.json"

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, channel="chromium")
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto("https://teams.microsoft.com/")
    input("Sign in in the browser window, then press Enter here to save the session... ")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(OUT))
    browser.close()
print(f"Saved bot session to {OUT}")
