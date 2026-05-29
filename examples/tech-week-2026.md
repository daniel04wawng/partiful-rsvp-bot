# Example: Tech Week 2026 NYC

Tech Week is an a16z-organized week of ~1500 events across NYC each June.
This is how you'd use the bot to RSVP to just the ones you actually want.

## 1. Discover what's on the calendar

```bash
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --dry-run --headed --max 10
```

`--dry-run --headed --max 10` opens a browser so you can watch, scrolls the
calendar, then navigates to 10 events without RSVPing. Look at `rsvp_log.csv`
to see what types of events the calendar has.

## 2. Pick what you want

Run the actual filter. A few examples:

### Founder breakfasts only

```bash
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --types "founders breakfast, founder coffee, founder lunch"
```

### AI / agent topics

```bash
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --types "AI, agents, LLM, agentic, foundation models"
```

### Semantic match with Claude

```bash
export ANTHROPIC_API_KEY=sk-ant-...
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --types "Events for early-stage founders raising seed or series A. Prefer demos and pitch nights. Skip yoga, runs, wellness, or non-tech parties." \
  --llm
```

The LLM mode handles negations ("skip yoga") and abstract concepts
("raising seed or series A" matches events about fundraising even if the
keywords don't appear literally).

## 3. Run overnight

The full Tech Week list is ~1500 events. After filtering you'll typically
have 50-150 matches. At 30-60s delays that's ~1-3 hours wall time, so just
let it run while you sleep.

```bash
partiful-rsvp rsvp \
  --calendar https://www.tech-week.com/calendar/nyc \
  --types "..." \
  --delay 30 --jitter 30
```

Wake up to `rsvp_log.csv` with success/pending/skipped per event.

## 4. Check the results

```bash
# count by result
awk -F, 'NR>1 {gsub(/[\\| ].*/,"",$4); print $4}' rsvp_log.csv | sort | uniq -c | sort -rn
```

Typical output:

```
  82 success
  31 pending approval
  18 already_rsvpd
   6 no_rsvp_button_found
   2 timeout
```

`pending approval` events need the host to manually approve you — bot can't
bypass that, but it submitted the request correctly. Check your Partiful
inbox over the next day or two for approvals.
