"""Renders the Jinja2 template to static HTML, plus an archival Markdown copy.

Each league gets its own subdirectory under docs/ (docs/<slug>/index.html) so
multiple leagues can coexist on the same GitHub Pages site with distinct,
bookmarkable URLs. docs/index.html itself is a small hub page listing every
league that's been generated, rebuilt from docs/leagues.json each run.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT_DIR / "templates"
DOCS_DIR = ROOT_DIR / "docs"
REPORTS_DIR = ROOT_DIR / "reports"
LEAGUES_MANIFEST_PATH = DOCS_DIR / "leagues.json"

_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = _SLUG_NON_ALNUM_RE.sub("-", text)
    return text.strip("-") or "league"


def _get_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )


def render_html(context: dict, output_path: Path = DOCS_DIR / "index.html") -> Path:
    template = _get_env().get_template("report.html.j2")
    html = template.render(**context)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def update_leagues_manifest(slug: str, league_name: str, year: int, generated_at: str) -> list[dict]:
    """Upsert this league's entry (by slug) into docs/leagues.json and return the full list."""
    entries: list[dict] = []
    if LEAGUES_MANIFEST_PATH.exists():
        with open(LEAGUES_MANIFEST_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)

    entries = [e for e in entries if e.get("slug") != slug]
    entries.append({"slug": slug, "league_name": league_name, "year": year, "generated_at": generated_at})
    entries.sort(key=lambda e: e["league_name"].lower())

    LEAGUES_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEAGUES_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    return entries


def render_hub(leagues: list[dict], output_path: Path = DOCS_DIR / "index.html") -> Path:
    """Render the multi-league landing page linking to each docs/<slug>/index.html."""
    template = _get_env().get_template("hub.html.j2")
    html = template.render(leagues=leagues)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def render_markdown(context: dict, output_path: Path | None = None, slug: str | None = None) -> Path:
    if output_path is None:
        year = context.get("year", datetime.now().year)
        slug = slug or slugify(context.get("league_name", "league"))
        output_path = REPORTS_DIR / slug / f"{year}-draft-recap.md"

    lines = [
        f"# {context.get('league_name', 'League')} — {context.get('year')} Draft Recap",
        "",
        f"_Generated {context.get('generated_at')}_",
        "",
    ]

    if context.get("value_grading_skipped"):
        lines.append(
            "> **Note:** the live ADP pull failed this run and no `data/rankings.csv` was "
            "available — the value/rank grading component was skipped entirely."
        )
        lines.append("")
    elif context.get("live_adp_failed"):
        lines.append(
            "> **Note:** the live ADP pull failed this run. Value/rank grading is based on "
            "`data/rankings.csv` only."
        )
        lines.append("")

    if context.get("scoring_warning"):
        lines.append(f"> **Note:** {context['scoring_warning']}")
        lines.append("")

    if context.get("schedule_unavailable"):
        lines.append(
            "> **Note:** the season schedule wasn't available yet, so projected records "
            "are not shown this run."
        )
        lines.append("")

    for team in context.get("teams", []):
        lines.append(f"## {team['team_name']} — {team['letter_grade']}")
        lines.append("")
        if team.get("projected_record"):
            lines.append(f"**Projected record:** {team['projected_record']}")
            lines.append("")
        lines.append(team["paragraph"])
        lines.append("")
        lines.append("**Picks:**")
        for pick in team["picks"]:
            rank_note = f" (ranked ~{pick['rank']:.0f})" if pick.get("rank") is not None else ""
            lines.append(f"- Round {pick['round']}, Pick {pick['pick_no']}: {pick['name']} ({pick['position']}){rank_note}")
        lines.append("")

    lines.append("---")
    lines.append("_ADP data via [Fantasy Football Calculator](https://fantasyfootballcalculator.com/)._")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
