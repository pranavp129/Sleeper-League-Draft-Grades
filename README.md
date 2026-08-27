# Sleeper Draft Recap Generator

Generates a shareable static site recapping a Sleeper fantasy draft: a letter grade per team,
a Monte Carlo-projected regular-season record, and an AI-written paragraph per team.

Live site: https://pranavp129.github.io/Sleeper-League-Draft-Grades/

## One-time setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:
- `ANTHROPIC_API_KEY` — for the recap paragraphs
- `SLEEPER_LEAGUE_ID` — convenience default; can be overridden per run with `--league-id`

## Generate a recap for a league

```bash
python src/main.py --league-id <sleeper_league_id>
```

This writes `docs/<slug>/index.html` (slug is auto-derived from the league's name on Sleeper)
and an archival copy at `reports/<slug>/<year>-draft-recap.md`. It also updates
`docs/index.html`, the hub page listing every league that's been generated.

Useful flags:
- `--slug my-league` — set the URL slug explicitly (needed if two leagues would otherwise
  slugify to the same name, e.g. two leagues both just called "Dynasty League")
- `--year 2027` — season year, for ADP lookup (defaults to the current year)
- `--skip-narrative` — skip the Claude calls and use plain-text summaries instead (free,
  useful for checking grades/records without spending API credits)
- `--model claude-haiku-4-5-20251001` — use a cheaper model for the recap paragraphs

## Adding another league

Same command, just point it at the new league's ID:

```bash
python src/main.py --league-id <new_sleeper_league_id>
```

It'll land at its own `docs/<new-slug>/` page and appear as a new card on the hub — no other
setup needed. Then publish:

```bash
git add -A
git commit -m "Add <league name> draft recap"
git push
```

(Or use GitHub Desktop: **Add Local Repository** if not already added, review the changes,
commit, and push.)

GitHub Pages is already configured to serve from `main` / `docs`, so nothing needs to change
in the repo's GitHub settings when adding a league — anything written under `docs/` just
shows up once it's pushed. Give it about a minute after pushing for the new page to go live.

## Re-running for the same league

Running the command again for a league you've already generated overwrites its page in place
(same slug, unless you pass a different `--slug`). Useful for correcting a bad name match in
`data/rankings.csv` or re-simulating with a fresh schedule — see that file's comments for how
manual ranking overrides work.
