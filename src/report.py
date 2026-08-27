"""Renders the Jinja2 template to static HTML, plus an archival Markdown copy."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT_DIR / "templates"
DOCS_DIR = ROOT_DIR / "docs"
REPORTS_DIR = ROOT_DIR / "reports"


def render_html(context: dict, output_path: Path = DOCS_DIR / "index.html") -> Path:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    template = env.get_template("report.html.j2")
    html = template.render(**context)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def render_markdown(context: dict, output_path: Path | None = None) -> Path:
    if output_path is None:
        year = context.get("year", datetime.now().year)
        output_path = REPORTS_DIR / f"{year}-draft-recap.md"

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
