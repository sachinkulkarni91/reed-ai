"""Headless meeting bot.

Launches Chromium, joins a Google Meet / Zoom / Teams call as a named guest,
starts tab-audio capture (see ``capture.js``), and stays until the meeting ends,
the host removes it, or the duration cap is hit.

Chromium flags do the consent work that a human would otherwise click:
  --use-fake-ui-for-media-stream     auto-grant mic/camera
  --use-fake-device-for-media-stream feed silence, so the bot never broadcasts
  --auto-accept-this-tab-capture     auto-grant getDisplayMedia on the current tab

Joining a call as a bot is a policy question as much as a technical one: several
platforms require the host to admit the bot, and recording consent rules vary by
jurisdiction. The bot announces itself by display name and does nothing to hide
its presence.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import websockets
from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright
from playwright._impl._errors import TargetClosedError  # not re-exported from async_api in 1.49

from ..config import settings
from ..models import Platform

log = logging.getLogger(__name__)

CAPTURE_JS = (Path(__file__).parent / "capture.js").read_text(encoding="utf-8")

CHROMIUM_ARGS = [
    "--auto-accept-this-tab-capture",
    # Deliberately NOT here:
    #   --use-fake-device-for-media-stream - replaces capture audio with Chromium's
    #     synthetic test signal (a pulse every 0.5s). With it set, getDisplayMedia
    #     tab capture recorded that beep pattern INSTEAD of the meeting - two real
    #     meetings came back as "no speech detected" with a perfectly flat energy
    #     profile and a 0.5s-periodic envelope, the fake device's fingerprint.
    #   --use-fake-ui-for-media-stream - unneeded (the tab-capture picker is
    #     auto-accepted by the flag above), and with a real mic present it would
    #     let the meeting page broadcast the host machine's actual microphone.
    # The bot gets no mic/camera permission at all (see new_context) - platforms
    # treat it as a muted, camera-less guest, which is exactly what a listener is.
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--autoplay-policy=no-user-gesture-required",
]


def detect_platform(url: str) -> Platform:
    host = url.lower()
    if "meet.google.com" in host:
        return Platform.google_meet
    if "zoom.us" in host or "zoom.com" in host:
        return Platform.zoom
    if "teams.microsoft.com" in host or "teams.live.com" in host:
        return Platform.teams
    raise ValueError(f"Unrecognised meeting URL: {url}")


def zoom_web_url(url: str) -> str:
    """Rewrite a Zoom invite link to the browser client, preserving the passcode."""
    match = re.search(r"/j/(\d+)", url)
    if not match:
        return url
    meeting_id = match.group(1)
    pwd = re.search(r"[?&]pwd=([^&]+)", url)
    domain = re.match(r"https?://([^/]+)", url)
    host = domain.group(1) if domain else "zoom.us"
    web = f"https://{host}/wc/{meeting_id}/join"
    if pwd:
        web += f"?pwd={pwd.group(1)}"
    return web


@dataclass
class BotResult:
    joined: bool
    reason: str = ""


class MeetingBot:
    def __init__(
        self,
        meeting_url: str,
        ingest_ws_url: str,
        display_name: str | None = None,
        meeting_id: int | None = None,
    ):
        self.meeting_url = meeting_url
        self.ingest_ws_url = ingest_ws_url
        self.display_name = display_name or settings.bot_display_name
        self.platform = detect_platform(meeting_url)
        self.meeting_id = meeting_id
        self._stop = asyncio.Event()
        self._page: Page | None = None
        self._browser: Browser | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._name_samples: list[dict] = []
        self._frame_count = 0

    def request_stop(self) -> None:
        """Safe to call from any thread - ``run()`` may be executing on a
        different event loop than the caller's (see ``run_bot_sync``). Also
        safe to call after the session has already ended on its own: by then
        the bot's dedicated loop is closed, and there's nothing left to stop."""
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
                return
            except RuntimeError:
                pass  # the bot's loop already closed - session is already over
        self._stop.set()

    # -- main loop ---------------------------------------------------------- #
    async def run(self) -> BotResult:
        self._loop = asyncio.get_running_loop()
        async with async_playwright() as pw:
            self._browser = await pw.chromium.launch(
                headless=settings.bot_headless,
                # Playwright 1.49+ defaults headless=True to the "headless shell" -
                # a stripped build without the tab-capture / WebRTC media pipeline
                # this bot depends on (getDisplayMedia). channel="chromium" pins the
                # full browser build (still headless via --headless=new), which has
                # every feature real Chrome has.
                channel="chromium",
                args=CHROMIUM_ARGS,
                # Playwright launches with --mute-audio by default; in headless
                # that stops tab audio from being rendered at all, so tab capture
                # records pure silence. Verified: with it, a tab playing known
                # speech captured 2.6KB of encoded silence; without it, the same
                # test captured real audio that transcribed word-for-word.
                ignore_default_args=["--mute-audio"],
            )
            context = await self._browser.new_context(
                # No mic/camera permission: the bot is a listener. Granting mic
                # (with fake-ui/fake-device flags) is how the fake test signal
                # ended up in recordings; denied permission just means the
                # platform shows "mic blocked" and joins the bot muted.
                permissions=[],
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            self._page = page
            ingest_ws = None

            try:
                joined = await self._join(page)
                if not joined.joined:
                    dump_path = await self._dump_diagnostics(page, "join_failed")
                    reason = joined.reason
                    if dump_path:
                        reason = f"{reason} (diagnostics saved: {dump_path})"
                    try:
                        seen = await page.evaluate(
                            """() => {
                              const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                              const d = [...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],[class*=DialogSurface],[id*=cookie i],[class*=cookie i],[class*=consent i]')]
                                .filter(vis).map(e => (e.getAttribute('role')||e.className||e.id||'').toString().slice(0,50) + ': ' + (e.innerText||'').replace(/\\s+/g,' ').slice(0,200));
                              const b = [...document.querySelectorAll('button')].filter(vis).map(e => (e.innerText||e.ariaLabel||'').trim().slice(0,30)).filter(Boolean).slice(0,15);
                              return 'DIALOGS=' + JSON.stringify(d.slice(0,4)) + ' BUTTONS=' + JSON.stringify(b) + ' URL=' + location.href.slice(0,120);
                            }"""
                        )
                        reason = f"{reason} [{seen}]"
                    except Exception:
                        pass
                    return BotResult(False, reason)

                await page.evaluate(CAPTURE_JS)
                result = await page.evaluate("([ms]) => reedStartCapture(ms)", [250])
                if result != "started":
                    return BotResult(False, f"Capture did not start: {result}")
                log.info("capture started on %s", self.platform.value)

                # The page never opens this connection itself: meeting sites set
                # their own CSP, and a strict connect-src (Teams' is a hard
                # allow-list of Microsoft's own domains) blocks any outbound
                # WebSocket the page tries to make, including one we inject -
                # confirmed via Teams' actual CSP header, and the instant
                # "ingest socket error" it produces from inside the page. Only
                # this Python process talks to the ingest socket; audio crosses
                # from the page via page.evaluate() (DevTools protocol, not a
                # page-initiated network request, so no CSP applies to it).
                try:
                    ingest_ws = await websockets.connect(self.ingest_ws_url, max_size=None)
                except Exception as exc:
                    return BotResult(False, f"Could not reach the ingest socket: {exc}")

                await self._watch(page, ingest_ws)
                return BotResult(True, "Meeting ended")
            except TargetClosedError:
                # The Chromium window was closed by hand (easy to do in headed
                # mode - that window IS the bot) or the tab crashed. Not a code
                # failure; report it plainly.
                return BotResult(
                    False,
                    "The bot's browser window was closed before the meeting ended - "
                    "don't close the Chromium window while the bot is in a call "
                    "(or set BOT_HEADLESS=true so there is no window to close).",
                )
            finally:
                self._save_name_samples()
                await self._shutdown(page, ingest_ws)
                try:
                    await self._browser.close()
                except Exception:
                    pass  # browser already gone - nothing left to close

    async def _dump_diagnostics(self, page: Page, label: str) -> str | None:
        """Screenshot + visible text of wherever the bot got stuck.

        Meeting UIs are selector-based and drift over time; when a join flow
        can't find its expected button, guessing at the new DOM blind wastes
        rounds. A screenshot and text dump turn "could not find X" into
        something fixable in one pass.
        """
        try:
            stem = f"meeting-{self.meeting_id}-{label}" if self.meeting_id else f"{label}-{self.platform.value}"
            base = settings.media_path / stem
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            base.with_suffix(".txt").write_text(text[:20000], encoding="utf-8")
            log.warning("join diagnostics saved to %s.{png,txt}", base)
            return str(base)
        except Exception as exc:
            log.warning("could not save join diagnostics: %s", exc)
            return None

    async def _shutdown(self, page: Page, ingest_ws) -> None:
        try:
            await page.evaluate("() => window.__reed && window.__reed.stop()")
            await asyncio.sleep(0.3)  # let MediaRecorder flush its final chunk
            if ingest_ws is not None:
                await self._drain_audio(page, ingest_ws)
                if ingest_ws.close_code is None:
                    await ingest_ws.send(json.dumps({"type": "eos"}))
                await ingest_ws.close()
        except Exception:
            pass

    async def _drain_audio(self, page: Page, ingest_ws) -> None:
        """Pulls buffered base64 audio chunks out of the page and forwards them
        to the ingest socket - see the CSP note in ``run()`` for why the page
        can't push them out itself."""
        drained = await page.evaluate("() => window.__reed && window.__reed.drain()")
        if not drained:
            return
        for chunk_b64 in drained.get("chunks", []):
            await ingest_ws.send(base64.b64decode(chunk_b64))

    async def _watch(self, page: Page, ingest_ws) -> None:
        """Stay in the call until it ends, we are removed, or the cap is reached.

        Also pumps buffered audio out of the page every cycle, and samples
        who-is-speaking evidence for the naming pass: the DOM's active-speaker
        indicator every few seconds (free, exact text) and a screenshot every
        ~30s (vision fallback for speakers the DOM misses).
        """
        deadline = asyncio.get_event_loop().time() + settings.bot_max_meeting_minutes * 60
        started = asyncio.get_event_loop().time()
        tick = 0
        rejoins = 0
        while not self._stop.is_set():
            if asyncio.get_event_loop().time() > deadline:
                log.info("duration cap reached; leaving")
                return
            try:
                await self._drain_audio(page, ingest_ws)
                if ingest_ws.close_code is not None:
                    log.warning("ingest socket closed (code=%s); leaving", ingest_ws.close_code)
                    return
                if await self._meeting_over(page):
                    log.info("meeting appears to be over; leaving")
                    return
                if await self._capture_ended(page):
                    # Teams sometimes drops the call ("Sorry, we couldn't connect
                    # you"), navigating away and killing the recorder. If a
                    # Rejoin button is on screen this is recoverable; a genuine
                    # meeting end has no such button and falls through.
                    if rejoins < 3 and await self._recover(page):
                        rejoins += 1
                        continue
                    log.info("tab capture stopped (stream ended); leaving")
                    return
                elapsed = asyncio.get_event_loop().time() - started
                # 2s DOM cadence so even a ~10-second turn collects several
                # samples; 15s snapshots so short turns land inside a frame.
                if tick % 2 == 0:
                    await self._sample_active_speaker(page, elapsed)
                if tick % 15 == 0 and self._frame_count < 80:
                    await self._snapshot(page, elapsed)
                tick += 1
            except Exception as exc:
                if rejoins < 3 and await self._recover(page):
                    rejoins += 1
                    continue
                log.warning("watch loop lost the page or socket: %s", exc)
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                continue

    # -- who-is-speaking sampling ------------------------------------------- #
    ACTIVE_SPEAKER_JS = """
    () => {
      const sels = [
        '[class*="speaking" i]', '[aria-label*="is speaking" i]',
        '[data-tid*="speaking" i]', '[class*="active-speaker" i]',
      ];
      for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
          const scope = el.closest('[aria-label]') || el.closest('[data-tid*="participant" i]') || el;
          const label = (scope.getAttribute && scope.getAttribute('aria-label')) || scope.textContent || '';
          const text = label.trim();
          if (text && text.length < 80) return text;
        }
      }
      return null;
    }
    """

    async def _sample_active_speaker(self, page: Page, elapsed: float) -> None:
        try:
            raw = await page.evaluate(self.ACTIVE_SPEAKER_JS)
        except Exception:
            return
        if not raw:
            return
        # aria-labels read like "Kapil, muted, is speaking" - keep the name part.
        name = str(raw).split(",")[0].strip()
        if name and len(name) < 60:
            self._name_samples.append({"t": round(elapsed, 1), "name": name})

    async def _snapshot(self, page: Page, elapsed: float) -> None:
        if self.meeting_id is None:
            return
        try:
            frames_dir = settings.media_path / f"meeting-{self.meeting_id}-frames"
            frames_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(
                path=str(frames_dir / f"t{int(elapsed)}.jpg"), type="jpeg", quality=70
            )
            self._frame_count += 1
        except Exception as exc:
            log.debug("snapshot failed at %.0fs: %s", elapsed, exc)

    def _save_name_samples(self) -> None:
        if self.meeting_id is None or not self._name_samples:
            return
        try:
            import json as _json

            path = settings.media_path / f"meeting-{self.meeting_id}-names.json"
            path.write_text(_json.dumps(self._name_samples), encoding="utf-8")
            log.info("saved %d active-speaker samples to %s", len(self._name_samples), path)
        except Exception:
            log.exception("could not save active-speaker samples")

    MEETING_OVER_PATTERNS = {
        Platform.google_meet: [
            "you've been removed",
            "return to home screen",
            "your meeting has ended",
            "you left the meeting",
        ],
        Platform.zoom: [
            "this meeting has been ended by host",
            "meeting ended",
            "you have left the meeting",
        ],
        Platform.teams: [
            "call ended",
            "you've been removed",
            "you have been removed",
            "you left this meeting",
            "left the meeting",
            "meeting ended",
            "meeting has ended",
            "you have been disconnected",
        ],
    }

    async def _meeting_over(self, page: Page) -> bool:
        """Text-based end-of-meeting detection.

        Checks every frame, not just the top-level page - Teams in particular
        nests its meeting UI inside iframes, and a removal notice rendered
        there is invisible to a plain ``page.locator(...)`` call.
        """
        patterns = self.MEETING_OVER_PATTERNS[self.platform]
        for frame in page.frames:
            try:
                text = (await frame.evaluate("() => document.body ? document.body.innerText : ''")).lower()
            except Exception:
                continue
            if any(p in text for p in patterns):
                return True
        return False

    async def _capture_ended(self, page: Page) -> bool:
        """Independent fallback: if the tab-capture stream itself stopped
        (e.g. the tab navigated away after being removed, ending the
        MediaRecorder), there's nothing left worth staying for - regardless
        of whether we recognised the on-screen wording."""
        try:
            state = await page.evaluate(
                "() => window.__reed && {running: window.__reed.running, error: window.__reed.error}"
            )
        except Exception:
            return True  # page itself is gone - definitely done
        if not state:
            return False
        return bool(state.get("error")) or state.get("running") is False

    # -- per-platform join flows -------------------------------------------- #
    async def _join(self, page: Page) -> BotResult:
        if self.platform is Platform.google_meet:
            return await self._join_meet(page)
        if self.platform is Platform.zoom:
            return await self._join_zoom(page)
        return await self._join_teams(page)

    async def _join_meet(self, page: Page) -> BotResult:
        await page.goto(self.meeting_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4000)

        # With mic/camera permission denied, Meet offers "Continue without
        # microphone and camera" before the pre-join screen. Take it.
        await self._confirm_no_media(page, label="Continue without microphone")

        name_box = page.locator('input[placeholder*="name" i], input[aria-label*="name" i]').first
        if await name_box.count():
            await name_box.fill(self.display_name)

        # If mic/camera toggles exist at all (they won't when permission is
        # denied), leave the call silent and dark; the bot is a listener.
        for label in ("Turn off microphone", "Turn off camera"):
            button = page.locator(f'[aria-label*="{label}" i]').first
            if await button.count():
                try:
                    await button.click(timeout=2000)
                except Exception:
                    pass

        for label in ("Ask to join", "Join now", "Join anyway"):
            button = page.get_by_role("button", name=re.compile(label, re.I)).first
            if await button.count():
                await button.click()
                break
        else:
            return BotResult(False, "Could not find the Google Meet join button")

        # "Ask to join" means a host has to admit us.
        for _ in range(60):
            if self._stop.is_set():
                return BotResult(False, "Stop requested while waiting to be admitted")
            if await page.locator('[aria-label*="Leave call" i], [aria-label*="End call" i]').count():
                return BotResult(True)
            if await page.locator("text=You can't join this call").count():
                return BotResult(False, "Google Meet refused the join request")
            await page.wait_for_timeout(2000)
        return BotResult(False, "Timed out in the Google Meet waiting room")

    async def _join_zoom(self, page: Page) -> BotResult:
        await page.goto(zoom_web_url(self.meeting_url), wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(5000)

        accept = page.get_by_role("button", name=re.compile("I Agree|Accept Cookies", re.I)).first
        if await accept.count():
            try:
                await accept.click(timeout=2000)
            except Exception:
                pass

        name_box = page.locator("#input-for-name, input[placeholder*='name' i]").first
        if await name_box.count():
            await name_box.fill(self.display_name)

        join = page.get_by_role("button", name=re.compile(r"^Join$|Join Meeting", re.I)).first
        if not await join.count():
            return BotResult(False, "Could not find the Zoom join button")
        await join.click()

        for _ in range(60):
            if self._stop.is_set():
                return BotResult(False, "Stop requested while waiting to be admitted")
            if await page.locator("button:has-text('Leave'), [aria-label*='Leave' i]").count():
                return BotResult(True)
            if await page.locator(":text('has been ended'), :text('Invalid meeting')").count():
                return BotResult(False, "Zoom rejected the join")
            await page.wait_for_timeout(2000)
        return BotResult(False, "Timed out in the Zoom waiting room")

    async def _join_teams(self, page: Page) -> BotResult:
        await page.goto(self.meeting_url, wait_until="domcontentloaded", timeout=60_000)

        # Teams' web client is a heavy SPA - a short fixed sleep isn't enough for
        # the pre-join lobby to render, especially on a cold cache (bundle fetch +
        # boot can take 10-20s). Poll for a real lobby marker instead of guessing
        # a sleep duration; a blank page at this point is the common failure mode.
        lobby_marker = (
            'input[placeholder*="name" i], '
            'button:has-text("Continue on this browser"), '
            'button:has-text("Join on the web"), '
            'button:has-text("Join now")'
        )
        try:
            await page.wait_for_selector(lobby_marker, timeout=45_000, state="visible")
        except PlaywrightTimeoutError:
            return BotResult(False, "Teams lobby never rendered (timed out waiting for it to load)")

        browser_button = page.get_by_role(
            "button", name=re.compile("Continue on this browser|Join on the web", re.I)
        ).first
        if await browser_button.count():
            await browser_button.click()
            try:
                await page.wait_for_selector(
                    'input[placeholder*="name" i], button:has-text("Join now")',
                    timeout=20_000,
                    state="visible",
                )
            except PlaywrightTimeoutError:
                return BotResult(False, "Teams did not reach the join screen after 'Continue on this browser'")

        frame = page.frame_locator("iframe").first if await page.locator("iframe").count() else None
        scope = frame if frame is not None else page

        # The "Are you sure you don't want audio or video?" modal can appear at
        # ANY point once Teams notices mic/camera are blocked - sometimes before
        # Join now is even clicked, where it blocks the whole pre-join screen.
        # Dismiss it whenever it shows up: before the name fill, before Join,
        # after Join, and on every lobby-wait tick.
        await self._confirm_no_media(page, label="Continue without audio", quiet=True)

        name_box = scope.locator('input[placeholder*="name" i]').first
        if await name_box.count():
            await name_box.fill(self.display_name)

        join = scope.get_by_role("button", name=re.compile("Join now", re.I)).first
        try:
            await join.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError:
            # A late modal may be covering the button - clear it and retry once.
            await self._confirm_no_media(page, label="Continue without audio")
            try:
                await join.wait_for(state="visible", timeout=10_000)
            except PlaywrightTimeoutError:
                return BotResult(False, "Could not find the Teams join button")
        # The "no audio/video?" dialog can drop its backdrop over the Join
        # button at the exact moment of clicking (meeting 10: 30s of click
        # retries, every one intercepted by fui-DialogSurface__backdrop).
        # Alternate dismiss+click, then fall back to a direct JS click, which
        # no backdrop can intercept.
        try:
            await join.click(timeout=5_000)
        except Exception:
            await self._confirm_no_media(page, label="Continue without audio")
            try:
                await join.click(timeout=5_000)
            except Exception:
                try:
                    await join.evaluate("el => el.click()")
                except Exception:
                    return BotResult(False, "Could not click the Teams join button (a dialog kept covering it)")

        await self._confirm_no_media(page, label="Continue without audio")

        for _ in range(60):
            if self._stop.is_set():
                return BotResult(False, "Stop requested while waiting to be admitted")
            try:
                if await page.locator('[aria-label*="Leave" i], button:has-text("Leave")').count():
                    return BotResult(True)
                # "Sorry, we couldn't connect you." can replace the lobby at any
                # point; its "Rejoin call" button restarts the join in place.
                await self._click_text(page, "rejoin call")
                await self._confirm_no_media(page, scope, label="Continue without audio", quiet=True)
            except Exception:
                pass  # transient navigation (e.g. mid-rejoin) - retry next tick
            await page.wait_for_timeout(2000)
        return BotResult(False, "Timed out in the Teams lobby")

    async def _recover(self, page: Page) -> bool:
        """Mid-call drop recovery: click 'Rejoin call', wait to get back into the
        meeting, and restart tab capture (the drop's navigation destroyed the old
        MediaRecorder). Returns True only once capture is running again; a page
        with no Rejoin button (a real meeting end, a crash) returns False so the
        caller leaves normally."""
        try:
            clicked = await self._click_text(page, "rejoin call")
        except Exception:
            return False
        if not clicked:
            return False
        log.warning("call dropped - clicked 'Rejoin call', waiting to get back in")
        for _ in range(30):
            if self._stop.is_set():
                return False
            try:
                await self._confirm_no_media(page, label="Continue without audio", quiet=True)
                if await page.locator('[aria-label*="Leave" i], button:has-text("Leave")').count():
                    await page.evaluate(CAPTURE_JS)
                    result = await page.evaluate("([ms]) => reedStartCapture(ms)", [250])
                    if result == "started":
                        log.info("rejoined the call and restarted capture")
                        return True
                    log.warning("rejoined but capture would not restart: %s", result)
                    return False
            except Exception:
                pass  # still navigating - try again next tick
            await page.wait_for_timeout(2000)
        log.warning("rejoin did not get back into the call within 60s")
        return False

    @staticmethod
    async def _click_text(page: Page, needle: str) -> str | None:
        """One-shot JS text-click across all frames (same rationale as
        ``_confirm_no_media``: role-based locators miss these dialogs)."""
        for frame in page.frames:
            try:
                clicked = await frame.evaluate(
                    """(needle) => {
                        for (const sel of ['button, [role=button], a', 'div, span']) {
                            for (const el of document.querySelectorAll(sel)) {
                                const t = (el.innerText || '').trim().toLowerCase();
                                if (t && t.length < 80 && t.includes(needle)) { el.click(); return t; }
                            }
                        }
                        return null;
                    }""",
                    needle.lower(),
                )
                if clicked:
                    return clicked
            except Exception:
                continue
        return None

    @staticmethod
    async def _confirm_no_media(page: Page, *_ignored, label: str, quiet: bool = False) -> None:
        """Clicks the 'join without audio/video?' confirmation if it appears.

        Deliberately low-tech: a JS click on any element whose text matches, in
        every frame. Role-based locators kept missing this dialog (it doesn't
        reliably expose a button role, and doesn't reliably live in the same
        document as the join button), and each miss costs a whole failed join.
        """
        deadline = 2 if quiet else 8
        for _ in range(deadline):
            for frame in page.frames:
                try:
                    btn = frame.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
                    if await btn.count():
                        await btn.click(timeout=3_000)
                        log.info("confirmed joining without audio/video (real click)")
                        return
                except Exception:
                    pass
                try:
                    clicked = await frame.evaluate(
                        """(needle) => {
                            for (const sel of ['button, [role=button], a', 'div, span']) {
                                for (const el of document.querySelectorAll(sel)) {
                                    const t = (el.innerText || '').trim().toLowerCase();
                                    if (t && t.length < 80 && t.includes(needle)) { el.click(); return t; }
                                }
                            }
                            return null;
                        }""",
                        label.lower(),
                    )
                    if clicked:
                        log.info("confirmed joining without audio/video (clicked %r)", clicked)
                        return
                except Exception:
                    continue
            await page.wait_for_timeout(1000)


def run_bot_sync(bot: MeetingBot) -> BotResult:
    """Runs one meeting bot to completion on its own dedicated event loop.

    Playwright's async driver launches Chromium via ``asyncio.create_subprocess_exec``.
    On Windows, uvicorn's request-handling loop is Selector-based, and Selector
    does not support subprocesses at all - ``async_playwright()`` fails immediately
    with ``NotImplementedError``. Proactor is the Windows loop that does support
    subprocesses, so each bot session gets its own thread running one, entirely
    separate from the server's loop (which stays Selector-compatible for uvicorn
    and websockets). Call via ``loop.run_in_executor(None, run_bot_sync, bot)``.
    """
    if sys.platform == "win32":
        loop = asyncio.WindowsProactorEventLoopPolicy().new_event_loop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(bot.run())
    finally:
        loop.close()
