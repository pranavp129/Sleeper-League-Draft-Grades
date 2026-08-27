# Build Prompt: Sleeper Draft Recap Generator

Paste this into a coding agent (e.g. Claude Code) to build the project.

---

## 1. Goal

Build a Python CLI tool that, after a Sleeper fantasy football draft completes, generates
a shareable static HTML report recreating Yahoo's old post-draft recap: a letter grade per
team, a projected regular-season record, and an AI-written paragraph analyzing each team's
draft. Output is a single static site pushed to GitHub Pages, so the league can be sent one
link in GroupMe.

Scope for v1: **post-draft recap only.** Do not build weekly power rankings, trade grades,
or in-season features yet — structure the code so those can be added later without a rewrite
(see "Design for future extension" below), but don't build them now.

## 2. Tech stack

- Python 3.11+
- `requests` for Sleeper API calls
- `anthropic` official SDK for the paragraph generation
- `python-dotenv` for loading `.env`
- `jinja2` for HTML templating
- `rapidfuzz` for fuzzy name-matching fallback against the DynastyProcess ID crosswalk (section 5)
- `pandas` (optional, only if it meaningfully simplifies the grading/simulation math — don't
  add it just for style)
- No web framework needed — this is a script that outputs static files, not a running server

## 3. Project structure

```
sleeper-draft-recap/
├── .env                     # not committed
├── .env.example             # committed, documents required vars
├── .gitignore                # must ignore .env, cache/, __pycache__
├── requirements.txt
├── data/
│   └── rankings.csv          # optional manual override/fallback, see section 5
├── cache/
│   ├── players.json          # cached Sleeper players dump, see section 4
│   └── playerids.csv         # cached DynastyProcess ID crosswalk, see section 5
├── src/
│   ├── sleeper_client.py     # all Sleeper API calls
│   ├── rankings.py           # ADP fetch + crosswalk matching to sleeper_id
│   ├── grading.py            # deterministic draft grade logic
│   ├── simulation.py         # Monte Carlo projected-record logic
│   ├── narrative.py          # Claude API calls for per-team paragraphs
│   ├── report.py             # renders the Jinja2 template to HTML
│   └── main.py                # orchestrates the whole run
├── templates/
│   └── report.html.j2
└── docs/                      # GitHub Pages source — generated output lands here
    └── index.html
```

## 4. Environment variables (`.env`)

Sleeper's API is public and read-only — **it does not require an API key or auth of any
kind.** Do not build any Sleeper credential handling. The `.env` file only needs:

```
ANTHROPIC_API_KEY=
SLEEPER_LEAGUE_ID=
```

`SLEEPER_LEAGUE_ID` is just a convenience default so the league doesn't have to be passed
as a CLI arg every run; still support `--league-id` as an override.

## 5. Data sources — read this before building the grading logic

Sleeper's public API (base URL `https://api.sleeper.app/v1`, confirmed via its docs) gives
you: league info, users, rosters, drafts, draft picks, matchups, and a full players dump.
**It does not provide ADP or expert rankings.** Any draft grade that claims to measure
"value vs. ADP" needs a rankings source from elsewhere.

This league is a **half-PPR redraft league** — default the ADP format to `half-ppr`, but
still read the league's actual `scoring_settings` from `GET /league/{league_id}` and warn
(don't crash) if it doesn't look like half-PPR, in case this tool ever gets pointed at a
different league later.

### ADP source: Fantasy Football Calculator's ADP REST API

Free, no API key, no auth, explicitly free for personal/commercial use (attribution
requested — put a small "ADP data via Fantasy Football Calculator" credit + link in the
report footer). Data is pulled from real human mock drafts, updates daily.

```
GET https://fantasyfootballcalculator.com/api/v1/adp/half-ppr?teams={n}&year={year}
```

`{n}` is the league's team count, from the league settings call. Don't call this more than
once per run — the data only refreshes daily anyway. This returns **player names, not
Sleeper IDs** — see the matching step below.

Note: I confirmed this endpoint is real and returns current 2026 half-PPR data (verified via
the site's own HTML rendering of it), but the API path itself is robots.txt-disallowed for
automated fetching tools like mine, so I could not personally inspect the raw JSON's exact
key names — only infer them from the displayed columns (name, position, team, bye, ADP).
Python's `requests` doesn't honor robots.txt and FFC's own docs describe this endpoint as
built for exactly this kind of external use, so this shouldn't block anything — but make the
first implementation step a real `requests.get()` call with the raw JSON printed, so the
actual key names are confirmed before the rest of `rankings.py` is built against assumed ones.

### ID matching: DynastyProcess player ID crosswalk, not raw fuzzy matching

Don't fuzzy-match FFC's player names directly against Sleeper's raw player dump — that's
solving a well-worn cross-platform problem from scratch, badly. Instead use
DynastyProcess's free, daily-updated player ID crosswalk, maintained by the ffverse
project specifically to link players across fantasy platforms:

```
https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv
```

This CSV includes a `sleeper_id` column plus player name/position/team and IDs for MFL,
ESPN, Yahoo, FantasyPros, PFF, and others. Build this in `src/rankings.py`:

1. Fetch and cache this file the same way as the Sleeper players dump — refresh at most
   once every 24 hours, don't refetch per run.
2. For each FFC ADP entry, match its name (normalized: lowercase, strip `Jr.`/`Sr.`/`II`/
   `III`/periods) against the crosswalk's `merge_name` column (already normalized this way
   by DynastyProcess — use it directly rather than re-deriving your own) to resolve
   `sleeper_id`.
3. **Handle name collisions explicitly.** Verified against the real file: multiple rows can
   share an identical `merge_name` (e.g. an active player and a retired/historical player
   with the same name — "Marvin Harrison" appears 4 times in the crosswalk, only one of
   them the active WR you actually want). A plain dict lookup will silently pick whichever
   row loaded last, which can resolve to the wrong player with no error. When a `merge_name`
   has multiple rows, prefer the one with a non-null `sleeper_id` (retired/inactive entries
   are usually missing it); if still ambiguous, disambiguate by team.
4. **Team defenses don't need the crosswalk at all.** Verified: this crosswalk contains zero
   `DEF` rows — it's a skill-position player database. Sleeper represents each team defense
   as its own player whose `player_id` is the team abbreviation itself (confirm this against
   the real `/players/nfl` response during implementation). FFC's ADP data already returns
   the team abbreviation for defenses — join defenses on team code directly, skip name
   matching for this position entirely.
5. Use `rapidfuzz` as a fallback for anything that still doesn't match exactly — but only
   accept fuzzy matches above a high confidence threshold, and only against this crosswalk,
   not against Sleeper's raw dump. A wrong match silently corrupts a team's grade.
6. Log every ADP entry that still couldn't be confidently matched. Don't fail the run.

Once ADP is resolved to `sleeper_id`, joining it to a team's actual draft picks is a direct
ID lookup against the `player_id` already present on each pick from Sleeper's draft-picks
endpoint — no further name matching needed at that stage.

### Fallback / override: manual CSV

`data/rankings.csv` — columns: `player_name, position, rank`. Optional. If present, entries
here take priority over the live-pulled ADP for the same player (lets the user hand-correct
a bad match or add someone the live pull missed) and are also used to fill in anyone the
matching step above couldn't confidently resolve. If both the live pull and the CSV are
unavailable for a given player, that player is simply excluded from the "value vs. rank"
component for that team — degrade gracefully, don't crash, and note in the report if the
live ADP pull failed entirely (network error, endpoint down) so the grade is understood as
CSV-only that run.

Both sources should normalize into the same in-memory shape (`sleeper_id, position, rank`)
before reaching `grading.py`, so the grading logic never needs to know which source a given
player's rank came from.

### Sleeper endpoints to use

| Purpose | Endpoint |
|---|---|
| League settings | `GET /league/{league_id}` |
| Teams/owners | `GET /league/{league_id}/users` |
| Rosters | `GET /league/{league_id}/rosters` |
| Drafts in league | `GET /league/{league_id}/drafts` |
| Draft picks | `GET /draft/{draft_id}/picks` |
| Full player dump | `GET /players/nfl` |
| Weekly matchups (for simulation, once schedule exists) | `GET /league/{league_id}/matchups/{week}` |

**Cache the player dump.** It's close to 5MB and Sleeper's own guidance is to call it at
most once a day — write it to `cache/players.json` and only refetch if the cache is missing
or older than 24 hours. Never fetch it inside a per-pick loop. Cache the DynastyProcess
crosswalk CSV (section 5) to `cache/playerids.csv` under the same 24-hour rule.

Stay well under Sleeper's documented rate limit (~1000 calls/min) — this is a low-volume
script, so this shouldn't be a real constraint, just don't do anything pathological like
refetching users or rosters per player.

## 6. Draft grade — deterministic, not AI-generated

The letter grade must be computed by code from actual numbers, not invented by the LLM.
This keeps it reproducible (rerun the tool, get the same grade) and auditable (you can show
your work if someone in the league disputes their C+). The LLM's only job (section 8) is to
explain a grade that already exists.

Build a composite score per team from these components (weights are a starting point —
expose them as constants near the top of `grading.py` so they're easy to tune):

1. **Value captured** (40%) — for each pick, compare the pick number to the player's rank
   in `rankings.csv`. A player taken later than their rank is value gained; earlier is a
   reach. Sum/average across the roster.
2. **Positional need coverage** (25%) — did the team fill starting-lineup requirements
   (per the league's roster settings from `/league/{league_id}`) without glaring gaps
   (e.g., zero RBs through round 8)?
3. **Roster balance / bench depth** (20%) — distribution across positions relative to
   typical roster construction, not just "best player available" every round.
4. **Upside vs. floor mix** (15%) — this one's inherently softer; use it as a minor
   modifier, not a primary driver, since you don't have projections data to make it rigorous.

Convert the composite score to a letter grade using percentile buckets **within that
specific draft** (i.e., grade teams relative to each other, not against some absolute
scale) — a 12-team draft should produce a real spread of grades, not 11 B+ and one B-.

## 7. Projected record — Monte Carlo simulation, not a random number

Do this properly or don't do it — a projected record with no defensible logic will get
torn apart by your league faster than just not having the feature.

1. Compute a **power score** per team from the draft grade components above plus roster
   construction (this can reuse most of the grading logic).
2. Pull the season's actual weekly schedule via `/league/{league_id}/matchups/{week}`
   (confirm during implementation whether Sleeper populates the full-season schedule
   immediately after the draft or only as weeks are played — if it's not available yet,
   fall back to `/league/{league_id}` schedule settings if present, or clearly label
   projected records as unavailable until the schedule exists).
3. For each matchup, model each team's weekly score as its power score plus random
   variance (e.g., normal distribution, tuned so a good-not-great team can still lose to
   a bad team some weeks — that's realistic and part of what made these fun).
4. Run the whole season N times (start with N=10,000), tally wins/losses each run, and
   average into a final projected record per team (e.g., "9.2–4.8").

## 8. AI-generated paragraph (Claude API)

One call per team. Feed Claude **structured output from steps 6–7**, not the raw pick
list — the paragraph should reference specific reasoning (their grade, their best value
pick, a reach, a positional gap, their projected record) rather than generic filler.

- Model: use `claude-sonnet-5` for quality, or `claude-haiku-4-5-20251001` if the user
  wants a cheaper run — make this a config option, don't hardcode one.
- System/user prompt should include: team name, grade + why (the computed components),
  full pick list with round/pick number, 1-2 standout value picks, 1-2 reaches, the
  projected record, and instructions to write ~120-180 words, confident sports-analyst
  tone, no hedging language, no restating the stat line verbatim.
- Handle API failures per team gracefully (retry once, then fall back to a plain-text
  summary of the grade components) so one failed call doesn't kill the whole report.

## 9. Report generation & output

- Render one static `docs/index.html` via Jinja2: every team as a card with team name,
  grade, projected record, and their paragraph. Simple, readable, mobile-friendly CSS —
  this is what gets opened from a GroupMe link on someone's phone, so no desktop-only
  layouts.
- Also write a plain Markdown version to the repo (not published) as an archival record.
- Deployment: `docs/` folder on the `main` branch, GitHub Pages configured to serve from
  `docs/`. One-time manual setup in the GitHub repo settings — don't try to automate Pages
  configuration itself. After that, every run is just: run the script, commit, push, share
  the same stable URL.

### Visual design — Metea Valley Mustangs theme (gold / black / grey)

This is a report card, so lean into that: the signature element is a stamped letter-grade
badge per team, styled like an ink stamp on a report card, rendered in gold on black rather
than the red-pen cliché. Don't default to a generic dark-mode dashboard look — commit to
the school-colors identity throughout, not just as an accent.

Color tokens (adjust exact values during build, but keep this structure — a near-black
base, one warm gold accent, and a spread of greys for hierarchy, not a flat two-tone):

```css
--mv-black:      #0B0B0C;   /* page background — not pure #000 */
--mv-grey-900:   #1D1D20;   /* card background */
--mv-grey-600:   #6B6B70;   /* secondary text, borders */
--mv-grey-300:   #C7C7CC;   /* dividers, muted labels */
--mv-white:      #F5F5F2;   /* primary body text, warm off-white */
--mv-gold:       #D4A72C;   /* primary accent — grade badges, headers, links */
--mv-gold-dim:   #8C7222;   /* gold at lower emphasis, e.g. hover states */
```

Typography — pair a bold condensed display face for team names/headers/grade badges
(scoreboard/varsity-roster energy — e.g. Oswald or Anton) with a clean readable body face
for the paragraphs (e.g. Inter or Source Sans Pro), and a monospace/tabular face for the
projected record and pick numbers (e.g. IBM Plex Mono) so stats align like a stat sheet.
All are free on Google Fonts — pull them via `<link>`, no local font files needed.

Layout: mobile-first single column of team cards (this is opened from a GroupMe link on a
phone) — team name in the display face, the stamped grade badge, projected record in the
mono face, then the paragraph. Keep the stamp as the one bold/rotated element on the page;
everything else stays disciplined (flat cards, restrained borders, no gradients or
drop-shadow piles) so the stamp actually reads as a signature rather than one flourish
among many. Respect `prefers-reduced-motion` if you animate the stamp on load.

## 10. Design for future extension (don't build yet, just don't block it)

- Keep Sleeper data-fetching, grading, simulation, narrative, and rendering as separate
  modules (already reflected in the file layout above) so a future "weekly power rankings"
  feature can reuse `sleeper_client.py` and `narrative.py` without touching draft-specific code.
- If multi-year history matters later: Sleeper leagues chain year to year via
  `previous_league_id` on the league object — worth knowing now even though v1 only
  handles the current season.

## 11. Acceptance criteria

- [ ] Running `python src/main.py` with a valid `.env` and `data/rankings.csv` produces
      `docs/index.html` with one card per team: grade, projected record, AI paragraph.
- [ ] If the live ADP API is unreachable and no `rankings.csv` is present, the tool still
      runs and clearly says in the report that the value/rank grading component was skipped.
- [ ] Unmatched ADP names (live pull didn't confidently match a Sleeper player) are logged,
      not silently dropped or silently mismatched.
- [ ] Player data is cached and not refetched more than once per 24 hours.
- [ ] Grades are computed deterministically — rerunning without new data produces the
      same grades.
- [ ] A failed Claude API call for one team doesn't stop the other teams' reports from
      generating.
- [ ] `.env` is gitignored; `.env.example` documents both required variables.
- [ ] Report is readable on a phone screen (this is how the league will actually view it).
- [ ] Report uses the gold/black/grey theme with the stamped-grade signature element, not
      a generic dark dashboard template.
- [ ] ADP is pulled from the half-PPR FFC endpoint and resolved to `sleeper_id` via the
      DynastyProcess crosswalk's `merge_name` column, with `rapidfuzz` only as a fallback —
      not raw name matching against Sleeper's player dump as the primary path.
- [ ] Name collisions in the crosswalk (multiple rows sharing a `merge_name`) are resolved
      by preferring the row with a populated `sleeper_id`, not by silently taking whichever
      row loaded last.
- [ ] Team defenses are matched by team abbreviation directly against Sleeper's own defense
      player IDs, not run through the player-name crosswalk (which doesn't contain them).
