"""Partiful RSVP bot — auto-RSVPs to events you describe in plain English.

Quick start
-----------
    pip install playwright && playwright install chromium
    python3 rsvp_bot.py login       # one-time, opens Chrome for SMS auth
    python3 rsvp_bot.py rsvp \\
        --calendar https://www.tech-week.com/calendar/nyc \\
        --types "AI, founder breakfasts, VC drinks"

The bot scrolls the calendar, fetches each event's title + description,
matches them against your `--types`, and RSVPs to the matches.

Matching modes
--------------
  default (free):  keyword/substring match against the types you list
  smart  (LLM):    set ANTHROPIC_API_KEY env var. Bot asks Claude Haiku
                   to judge each event against your description. Catches
                   semantic matches ('drinks party' → wine tasting).
                   Costs ~$0.001 per event.

License
-------
MIT — see LICENSE. Not affiliated with Partiful. Use at your own risk;
Partiful's ToS prohibits automation, you may get account-banned for
high volume.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
except ImportError:
    async_playwright = None  # type: ignore

log = logging.getLogger("rsvp_bot")

STATE_FILE = Path("partiful_state.json")
PROFILE_FILE = Path("partiful_profile.json")
LOG_FILE = Path("rsvp_log.csv")

_PARTIFUL_URL_RE = re.compile(r"partiful\.com/e/[A-Za-z0-9]+")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# =========================================================================
# login
# =========================================================================

async def cmd_login() -> None:
    """Open headed Chrome to partiful.com. User logs in. We save state."""
    if not async_playwright:
        log.error("playwright not installed. pip install playwright && playwright install chromium")
        sys.exit(1)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1200, "height": 900}, user_agent=UA)
        page = await context.new_page()
        await page.goto("https://partiful.com/", wait_until="networkidle")
        print()
        print("=" * 70)
        print("  Partiful login window opened.")
        print("  - Click Sign in / Log in")
        print("  - Enter your phone, type the SMS code")
        print("  - Wait until you see your home feed")
        print(f"  - Then return here and press ENTER to save state to")
        print(f"    {STATE_FILE.absolute()}")
        print("=" * 70)
        input("Press ENTER once logged in... ")
        await context.storage_state(path=str(STATE_FILE))
        print(f"\n✓ saved login state to {STATE_FILE}")
        await browser.close()


# =========================================================================
# calendar scrape
# =========================================================================

async def scroll_calendar(calendar_url: str) -> list[str]:
    """Scroll a Tech Week (or any infinite-scroll) calendar page in headless
    Chromium until no new Partiful event URLs appear. Returns sorted URLs."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            log.info("scrolling %s", calendar_url)
            await page.goto(calendar_url, wait_until="networkidle", timeout=45_000)
            prev, stable = 0, 0
            for i in range(80):
                html = await page.content()
                urls = set(_PARTIFUL_URL_RE.findall(html))
                if i % 5 == 0:
                    log.info("  scroll #%d: %d events", i, len(urls))
                if len(urls) == prev:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                prev = len(urls)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1200)
            html = await page.content()
            urls = sorted({"https://" + u for u in _PARTIFUL_URL_RE.findall(html)})
        finally:
            await browser.close()
    log.info("calendar done: %d unique events", len(urls))
    return urls


# =========================================================================
# event metadata fetch (for filtering)
# =========================================================================

def _fetch_event_meta(event_url: str, timeout: float = 12.0) -> Optional[dict]:
    """Return {title, description, host_orgs} or None on failure."""
    try:
        req = urllib.request.Request(event_url, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=timeout).read().decode(errors="replace")
    except Exception:
        return None
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    pp = data.get("props", {}).get("pageProps", {})
    event = pp.get("event") or {}
    hosts = pp.get("hosts") or []
    host_names = [h.get("name") for h in hosts if isinstance(h, dict) and h.get("name")]
    return {
        "url": event_url,
        "title": event.get("title") or "",
        "description": (event.get("description") or "")[:800],
        "hosts": host_names,
    }


# =========================================================================
# matching (keyword OR LLM)
# =========================================================================

def _normalize_types(types: str) -> list[str]:
    """'AI, founder breakfasts, VC drinks' -> ['ai', 'founder breakfasts', 'vc drinks']"""
    return [t.strip().lower() for t in re.split(r"[;,]", types) if t.strip()]


def keyword_match(meta: dict, types: list[str]) -> tuple[bool, str]:
    """Cheap default: any type substring appears in title/description/hosts."""
    blob = " ".join([
        meta.get("title", ""),
        meta.get("description", ""),
        " ".join(meta.get("hosts") or []),
    ]).lower()
    for t in types:
        # split phrase into words; all words must appear (substring) for phrase match
        words = t.split()
        if all(w in blob for w in words):
            return True, t
    return False, ""


def llm_match(meta: dict, user_types: str) -> tuple[bool, str]:
    """Smart mode: Claude Haiku judges whether the event matches the user's
    plain-English description. Skipped if ANTHROPIC_API_KEY isn't set."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return False, "no ANTHROPIC_API_KEY"
    try:
        import anthropic
    except ImportError:
        return False, "anthropic package not installed"
    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"You decide whether an event matches what a user is looking for.\n\n"
        f"User wants: {user_types}\n\n"
        f"Event title: {meta.get('title','')!r}\n"
        f"Event description: {meta.get('description','')[:600]!r}\n"
        f"Event hosts: {', '.join(meta.get('hosts') or [])!r}\n\n"
        f"Reply with exactly one JSON object: "
        f'{{"match": true|false, "reason": "<5 words why>"}}'
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        return False, f"llm error: {type(exc).__name__}"
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{[^}]+\}", text)
    if not m:
        return False, "llm bad output"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, "llm bad json"
    return bool(data.get("match")), (data.get("reason") or "")[:60]


# =========================================================================
# read name + phone from the logged-in session
# =========================================================================

async def _extract_user_info(page: Page) -> tuple[Optional[str], Optional[str]]:
    """After login, Partiful stores the user's name + phone in browser
    localStorage under Firebase Auth keys. Pull them out so the user
    doesn't have to type --name/--phone for every run."""
    try:
        await page.goto("https://partiful.com/", wait_until="domcontentloaded", timeout=20_000)
    except Exception:
        return (None, None)
    # Wait briefly for client JS to hydrate localStorage.
    await page.wait_for_timeout(1500)
    try:
        data = await page.evaluate("""
          () => {
            const out = {name: null, phone: null};
            for (let i = 0; i < localStorage.length; i++) {
              const k = localStorage.key(i);
              if (!k) continue;
              if (k.startsWith('firebase:authUser:')) {
                try {
                  const v = JSON.parse(localStorage.getItem(k));
                  if (v.phoneNumber) out.phone = v.phoneNumber;
                  if (v.displayName) out.name = v.displayName;
                } catch (e) {}
              }
              if (k.startsWith('CT_user_') || k.includes('partiful')) {
                try {
                  const v = JSON.parse(localStorage.getItem(k));
                  if (v && typeof v === 'object') {
                    if (v.phoneNumber && !out.phone) out.phone = v.phoneNumber;
                    if (v.displayName && !out.name) out.name = v.displayName;
                    if (v.name && !out.name) out.name = v.name;
                  }
                } catch (e) {}
              }
            }
            return out;
          }
        """)
    except Exception:
        return (None, None)
    if isinstance(data, dict):
        return (data.get("name"), data.get("phone"))
    return (None, None)


# =========================================================================
# rsvp on a single page
# =========================================================================

async def _rsvp_one(page: Page, url: str, *, dry_run: bool,
                     name: Optional[str] = None, phone: Optional[str] = None,
                     debug: bool = False, timeout_ms: int = 60_000) -> tuple[str, str]:
    """RSVP to one event. If debug=True, screenshots at each stage and
    keeps the browser open longer so a human can watch."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PWTimeout:
        return ("navigate", "timeout")
    # React/Next hydration window — without this we look for buttons before
    # the app finishes rendering and almost always miss them.
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PWTimeout:
        pass  # some events keep long-poll connections open; carry on
    await page.wait_for_timeout(800)
    title = ""
    try:
        title = (await page.title()) or ""
    except Exception:
        pass
    page_text = (await page.inner_text("body")).lower()
    # Past-event detection — Partiful removes the RSVP button after an
    # event ends. Surface a clearer skip reason than "no button found."
    if any(t in page_text for t in ("event ended", "event has ended",
                                     "this event has passed", "no longer accepting")):
        return ("skip", f"event_ended | {title[:80]}")
    if any(t in page_text for t in ("you're going", "you're attending", "you're in",
                                     "you applied", "you've applied",
                                     "application pending",
                                     "you're on the waitlist", "you're waitlisted")):
        return ("skip", f"already_rsvpd | {title[:80]}")

    candidates = [
        # invite-only / approval events
        "button:has-text('Apply')",
        "button:has-text('Request to join')",
        "button:has-text('Request invite')",
        "button:has-text('Request to attend')",
        "[role=button]:has-text('Apply')",
        "[role=button]:has-text('Request')",
        # open RSVP
        "button:has-text('Going')",
        "button:has-text('RSVP')",
        "button:has-text(\"I'm going\")",
        "button:has-text('Yes')",
        "button:has-text('Join')",
        "button:has-text('Count me in')",
        "[role=button]:has-text('Going')",
        "[role=button]:has-text('RSVP')",
        # waitlist
        "button:has-text('Join waitlist')",
        "button:has-text('Waitlist')",
    ]
    btn = None
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                btn = loc
                break
        except Exception:
            continue
    if btn is None:
        return ("skip", f"no_rsvp_button_found | {title[:80]}")
    if dry_run:
        try:
            btn_text = (await btn.inner_text(timeout=2000)).strip()
        except Exception:
            btn_text = "?"
        return ("dry-run", f"would_click [{btn_text!r}] | {title[:80]}")
    try:
        await btn.click(timeout=5_000)
    except Exception as exc:
        return ("click", f"click_failed:{type(exc).__name__} | {title[:80]}")

    # Partiful opens a confirm modal with Going/Maybe/Can't Go preselected on
    # Going, plus Name + Phone fields and a Continue button. Fill what we have.
    await page.wait_for_timeout(1200)
    if debug:
        await page.screenshot(path="debug_01_modal_opened.png", full_page=True)

    name_filled, phone_filled = False, False
    if name:
        # Partiful's name field looks like a styled div, not a real <input>.
        # Try the obvious selectors first; if none match, fall back to typing
        # into whichever visible input isn't the phone field.
        attempted = [
            "input[placeholder*='Name' i]",
            "input[placeholder*='your name' i]",
            "input[name='name' i]",
            "input[aria-label*='Name' i]",
            "[contenteditable='true']",
        ]
        for sel in attempted:
            try:
                inp = page.locator(sel).first
                if await inp.is_visible(timeout=1200):
                    try:
                        cur = (await inp.input_value()) or ""
                    except Exception:
                        cur = (await inp.inner_text()) or ""
                    if not cur.strip():
                        try:
                            await inp.fill(name, timeout=1500)
                        except Exception:
                            # contenteditable / styled-div fields ignore fill;
                            # focus + keyboard.type is the reliable fallback.
                            await inp.click(timeout=1500)
                            await page.keyboard.type(name, delay=30)
                    name_filled = True
                    break
            except Exception:
                continue
        # Last-resort fallback: first visible text-ish input that isn't the
        # phone one. Partiful's modal layout is consistent — name is first,
        # phone is second.
        if not name_filled:
            try:
                inputs = page.locator(
                    "input:visible:not([type='tel']):not([inputmode='tel']):not([type='hidden'])"
                )
                first = inputs.first
                if await first.is_visible(timeout=1500):
                    try:
                        await first.fill(name, timeout=1500)
                    except Exception:
                        await first.click(timeout=1500)
                        await page.keyboard.type(name, delay=30)
                    name_filled = True
            except Exception:
                pass
    if phone:
        for sel in ("input[type='tel']",
                    "input[placeholder*='Phone' i]",
                    "input[placeholder*='phone number' i]",
                    "input[name='phone' i]",
                    "input[aria-label*='Phone' i]",
                    "input[inputmode='tel']"):
            try:
                inp = page.locator(sel).first
                if await inp.is_visible(timeout=1500):
                    cur = (await inp.input_value()) or ""
                    if not cur.strip():
                        await inp.fill(phone, timeout=2000)
                    phone_filled = True
                    break
            except Exception:
                continue
    if debug:
        log.info("    debug: name_filled=%s phone_filled=%s", name_filled, phone_filled)
        await page.screenshot(path="debug_02_after_fill.png", full_page=True)

    # Click confirm. 'Continue' is Partiful's modal-submit. Try both <button>
    # and [role=button] / div variants since Partiful uses styled divs.
    clicked_confirm = False
    for sel in ("button:has-text('Continue')",
                "[role=button]:has-text('Continue')",
                "div:has-text('Continue'):not(:has(*))",
                "button:has-text('Confirm')",
                "button:has-text('RSVP')",
                "button:has-text('Submit')",
                "button:has-text('Done')",
                "button:has-text('Save')",
                "button:has-text('Apply')"):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2_500):
                await loc.click(timeout=3_000)
                clicked_confirm = True
                break
        except Exception:
            continue
    if debug:
        log.info("    debug: clicked_confirm=%s", clicked_confirm)
    await page.wait_for_timeout(3_000)
    if debug:
        await page.screenshot(path="debug_03_after_confirm.png", full_page=True)
        log.info("    debug: holding browser open for 20s for inspection")
        await page.wait_for_timeout(20_000)
    try:
        post_text = (await page.inner_text("body")).lower()
        if any(t in post_text for t in ("you're going", "you're attending", "you're in",
                                        "you applied", "you've applied",
                                        "pending approval", "request submitted", "request sent",
                                        "you're on the waitlist")):
            return ("rsvp", f"success | {title[:80]}")
    except Exception:
        pass
    return ("rsvp", f"unknown_state | {title[:80]}")


# =========================================================================
# top-level rsvp command
# =========================================================================

async def cmd_rsvp(
    *,
    calendar_url: Optional[str] = None,
    urls_file: Optional[Path] = None,
    types: Optional[str] = None,
    use_llm: bool = False,
    delay: float = 30.0,
    jitter: float = 30.0,
    max_events: Optional[int] = None,
    dry_run: bool = False,
    headed: bool = False,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    debug: bool = False,
) -> None:
    if not async_playwright:
        log.error("playwright not installed")
        sys.exit(1)
    if not STATE_FILE.exists():
        log.error(f"no login state at {STATE_FILE} — run `python3 rsvp_bot.py login` first")
        sys.exit(1)

    # Source URLs
    if calendar_url:
        urls = await scroll_calendar(calendar_url)
    elif urls_file:
        urls = [u.strip() for u in urls_file.read_text().splitlines()
                if u.strip() and not u.startswith("#")]
    else:
        log.error("need --calendar URL or --urls FILE")
        sys.exit(1)

    # Optional filter
    if types:
        type_list = _normalize_types(types)
        log.info("filtering %d events against types: %s", len(urls), type_list)
        kept: list[tuple[str, str]] = []  # (url, why_matched)
        for i, u in enumerate(urls, 1):
            if i % 25 == 0:
                log.info("  filter %d/%d — keeping %d so far", i, len(urls), len(kept))
            meta = _fetch_event_meta(u)
            if not meta:
                continue
            if use_llm:
                hit, why = llm_match(meta, types)
            else:
                hit, why = keyword_match(meta, type_list)
            if hit:
                kept.append((u, why))
        log.info("filter kept %d / %d events", len(kept), len(urls))
        urls = [u for u, _ in kept]
        # Persist filter result so re-runs are cheap
        Path("rsvp_filtered.csv").open("w", newline="").write(
            "url,reason\n" + "\n".join(f"{u},{w}" for u, w in kept))
        log.info("saved filtered set to rsvp_filtered.csv")

    if max_events is not None:
        urls = urls[:max_events]
    log.info("queueing %d events (delay=%.0fs±%.0f, dry_run=%s)",
             len(urls), delay, jitter, dry_run)

    log_existed = LOG_FILE.exists()
    f = LOG_FILE.open("a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if not log_existed:
        w.writerow(["timestamp", "url", "action", "result"])
    f.flush()

    # Resolve name + phone in priority order:
    #   1. --name/--phone CLI flags
    #   2. saved partiful_profile.json from a previous run
    #   3. auto-extract from Firebase Auth in localStorage
    # Whatever we end up with gets written back to the profile so the next
    # run picks it up without any flags.
    if PROFILE_FILE.exists():
        try:
            saved = json.loads(PROFILE_FILE.read_text())
            if not name and saved.get("name"):
                name = saved["name"]
                log.info("loaded name from %s", PROFILE_FILE)
            if not phone and saved.get("phone"):
                phone = saved["phone"]
                log.info("loaded phone from %s", PROFILE_FILE)
        except Exception:
            pass

    consecutive_fail = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        context = await browser.new_context(storage_state=str(STATE_FILE))
        page = await context.new_page()
        # Last-resort extraction from the live session.
        if not name or not phone:
            ext_name, ext_phone = await _extract_user_info(page)
            if not name and ext_name:
                name = ext_name
                log.info("auto-extracted name from login: %s", name)
            if not phone and ext_phone:
                phone = ext_phone
                log.info("auto-extracted phone from login: %s", phone)
        # Persist whatever we ended up with so the next run is zero-config.
        if name or phone:
            try:
                PROFILE_FILE.write_text(json.dumps({"name": name, "phone": phone}))
            except Exception:
                pass
        if not phone:
            log.warning("no phone resolved — Partiful's modal may not accept the RSVP. "
                        "Pass --phone explicitly once and it'll be saved for next time.")
        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] %s", i, len(urls), url)
            action, result = await _rsvp_one(page, url, dry_run=dry_run,
                                              name=name, phone=phone,
                                              debug=debug)
            w.writerow([datetime.now(timezone.utc).isoformat(), url, action, result])
            f.flush()
            log.info("    %s | %s", action, result)
            if action in ("click", "navigate") and "fail" in result.lower():
                consecutive_fail += 1
            else:
                consecutive_fail = 0
            if consecutive_fail >= 3:
                log.error("3 consecutive failures — bailing. account may be flagged.")
                break
            if i < len(urls):
                wait = delay + random.uniform(0, jitter)
                log.info("    sleeping %.1fs", wait)
                await asyncio.sleep(wait)
        try:
            await context.storage_state(path=str(STATE_FILE))
        except Exception:
            pass
        await browser.close()
    f.close()
    log.info("done. log at %s", LOG_FILE)


# =========================================================================
# cli
# =========================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="one-time browser login; saves partiful_state.json")

    rp = sub.add_parser("rsvp", help="run RSVPs")
    src = rp.add_mutually_exclusive_group(required=True)
    src.add_argument("--calendar", type=str,
                     help="Tech-week-style calendar URL (e.g. https://www.tech-week.com/calendar/nyc)")
    src.add_argument("--urls", type=Path, help="text file with one Partiful URL per line")
    rp.add_argument("--types", type=str, default=None,
                    help='comma-separated event types you want, e.g. "AI, founder breakfasts, VC drinks". '
                         'When set, the bot fetches each event and only RSVPs to matching ones.')
    rp.add_argument("--llm", action="store_true",
                    help="use Claude Haiku to judge matches semantically. "
                         "Requires ANTHROPIC_API_KEY env var (~$0.001 per event).")
    rp.add_argument("--delay", type=float, default=30.0, help="base seconds between RSVPs")
    rp.add_argument("--jitter", type=float, default=30.0, help="random extra seconds")
    rp.add_argument("--max", type=int, default=None, help="optional cap on events this run")
    rp.add_argument("--name", type=str, default=None,
                    help="name to fill in Partiful's RSVP modal if it asks. "
                         "Use the same name on your Partiful account.")
    rp.add_argument("--phone", type=str, default=None,
                    help="phone number for the RSVP modal (e.g. '+1 555 123 4567'). "
                         "Partiful uses this for event reminders.")
    rp.add_argument("--dry-run", action="store_true", help="navigate + report, don't click")
    rp.add_argument("--headed", action="store_true", help="visible browser (debugging)")
    rp.add_argument("--debug", action="store_true",
                    help="save screenshots (debug_01..03_*.png), log every modal step, "
                         "and hold the browser open 20s after Continue so you can inspect "
                         "what landed. Use to diagnose unknown_state results.")
    rp.add_argument("--quiet", action="store_true")

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.WARNING if getattr(args, "quiet", False) else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.cmd == "login":
        asyncio.run(cmd_login())
    elif args.cmd == "rsvp":
        asyncio.run(cmd_rsvp(
            calendar_url=args.calendar,
            urls_file=args.urls,
            types=args.types,
            use_llm=args.llm,
            delay=args.delay,
            jitter=args.jitter,
            max_events=args.max,
            dry_run=args.dry_run,
            headed=args.headed,
            name=args.name,
            phone=args.phone,
            debug=args.debug,
        ))


if __name__ == "__main__":
    main()
