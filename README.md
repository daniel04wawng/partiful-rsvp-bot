# partiful-rsvp-bot

Auto-RSVPs to Partiful events that match the kinds of events you actually want
to go to. Originally built to handle Tech Week's ~1500-event calendars without
hand-clicking every one.

```bash
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --types "AI, founder breakfasts, VC drinks"
```

That scrolls the calendar, fetches each event, keeps the ones that match
"AI", "founder breakfasts", or "VC drinks", and RSVPs to them with human-paced
delays.

---

## Why

Multi-day conference weeks (Tech Week, SXSW satellite events, etc.) publish
calendars of hundreds-to-thousands of events on Partiful. You actually want
to attend 20-50 of them. Manually filtering and RSVPing eats half a day.

This bot:

1. **Scrolls the calendar** for you (Playwright, headless Chrome)
2. **Filters events** by keyword (free) or semantic LLM match (opt-in)
3. **RSVPs** to each match in a real browser session, with human-pacing

Works on any Partiful event page, not just Tech Week — point it at any
infinite-scroll calendar of Partiful URLs and it'll do the rest.

---

## Setup (~5 min, one-time)

### 1. Install

```bash
pipx install partiful-rsvp
playwright install chromium
```

(or `pip install partiful-rsvp` if you don't have pipx)

For the optional semantic-matching mode:

```bash
pipx install "partiful-rsvp[llm]"
```

### 2. Log in to Partiful

```bash
partiful-rsvp login
```

Opens Chrome. Sign in with your phone + SMS as normal. When you see your home
feed, return to the terminal and hit ENTER. Your session is saved to
`partiful_state.json` (don't commit it; it's your auth).

---

## Usage

### Simple — RSVP to everything on a calendar

```bash
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc
```

### Filter by event type (keyword mode, free)

```bash
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --types "AI, founders, VC, drinks, breakfast"
```

Each comma-separated phrase is matched as a substring against the event's
title, description, and host names. Useful for unambiguous keywords like
brand names, tech topics, event formats.

### Filter by event type (semantic, smarter)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --types "founder breakfasts, intimate VC dinners, AI agent workshops. Skip yoga or wellness." \
  --llm
```

Claude Haiku judges each event against the natural-language description.
Catches things keyword matching misses ("drinks party" → matches a wine
tasting; "yoga or wellness" → excludes wellness brunches).

Cost: ~$0.001 per event judged, so $1-2 for a 1500-event calendar.

### Dry-run first

Always test on a small batch before letting it loose:

```bash
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --types "AI, founders" \
  --dry-run --headed --max 5
```

`--headed` shows the browser, `--dry-run` reports what it WOULD click without
clicking, `--max 5` stops after 5 events. The log will show:

```
[1/5] https://partiful.com/e/...
    dry-run | would_click ['Apply'] | Private AI Founders Breakfast
```

### Pace it

Defaults to 30-60s between events with random jitter. Tune with `--delay`
(base seconds) and `--jitter` (max extra random seconds):

```bash
--delay 45 --jitter 30    # 45-75s gaps, slower & safer
--delay 15 --jitter 15    # 15-30s gaps, faster & riskier
```

---

## Outputs

Each run appends to `rsvp_log.csv`:

| timestamp | url | action | result |
|---|---|---|---|
| 2026-05-28T...Z | https://partiful.com/e/abc | rsvp | success \| AI Founders Breakfast |
| 2026-05-28T...Z | https://partiful.com/e/def | skip | already_rsvpd \| ... |
| 2026-05-28T...Z | https://partiful.com/e/ghi | rsvp | unknown_state \| ... |

When filtering, the bot also writes `rsvp_filtered.csv` with the kept URLs +
why they matched, so you can spot-check before re-running.

### Result codes

| Result | What it means |
|---|---|
| `success` | RSVP submitted, you should be on the list |
| `pending approval` / `request submitted` | Invite-only event; host needs to approve, can't bypass |
| `already_rsvpd` | You're already in; nothing to do |
| `no_rsvp_button_found` | Event is closed, requires a code, or uses a button label we don't know about |
| `unknown_state` | Click sent but we couldn't confirm success — manually verify these |
| `timeout` | Page didn't load (often a deleted event) |

---

## Caveats

- **Partiful's ToS prohibits automated activity.** Account ban risk scales with
  volume. We've found 100-200 RSVPs/day with 30-60s delays usually goes
  undetected; 500+/day or sub-10s delays often gets the account flagged.
  Use a phone number you can afford to lose.
- **Hosts plan capacity based on RSVPs.** RSVPing yes to events you won't
  attend wastes their food/space and burns trust. Use the filter aggressively.
- **Invite-only events still need host approval** — bot submits the request,
  host approves manually. Look for `pending approval` in the log.
- **Login session expires** after ~weeks. If RSVPs start failing with weird
  redirects, re-run `python3 rsvp_bot.py login`.
- **No re-login needed when updating the script.** `partiful_state.json` is
  independent of bot version; just download a new `rsvp_bot.py` and run.

---

## How it works

1. **Calendar scroll** (Playwright in headless mode): keeps scrolling until
   no new `partiful.com/e/...` URLs appear for 3 rounds. Caps at 80 scrolls.

2. **Event metadata fetch** (plain HTTP): pulls `__NEXT_DATA__` from each event
   page to get title, description, and host names. No login required — events
   are SSR'd for previews.

3. **Match** (regex or LLM): keyword mode does substring match against the
   blob; LLM mode asks Claude Haiku to return `{"match": true|false, ...}`.

4. **RSVP** (Playwright in headed-or-headless mode, with login state):
   navigates to each event, finds the RSVP/Apply/Join/Waitlist button by
   text match across ~15 selector variants, clicks it, handles confirm modals,
   verifies post-click success text.

Single file (`rsvp_bot.py`), one dependency (`playwright`), no database.

---

## Roadmap

- [ ] Better invite-only handling (auto-fill the host's questionnaire)
- [ ] Per-event RSVP intent (going / maybe / interested) instead of always Going
- [ ] Calendar-source adapters beyond tech-week.com (Luma, Eventbrite calendars
      that link to Partiful)
- [ ] Slack/Discord webhook on each RSVP success for tracking
- [ ] CI tests against a saved Partiful event HTML fixture

PRs welcome.

---

## License

MIT — see `LICENSE`.

Not affiliated with Partiful. Built for personal use.
