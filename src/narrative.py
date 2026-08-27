"""Claude API calls for per-team draft recap paragraphs.

Fed structured output from grading.py/simulation.py -- never the raw pick
list alone -- so the paragraph references specific reasoning (grade, best
value pick, a reach, a positional gap, projected record) instead of filler.
"""

from __future__ import annotations

import logging

from anthropic import Anthropic

from .grading import TeamGradeResult
from .simulation import ProjectedRecord

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
CHEAP_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 500

SYSTEM_PROMPT = """You are a confident sports-analyst columnist writing post-draft \
recap paragraphs for a fantasy football league's report-card website. Write in the \
voice of a beat writer grading a real draft class: opinionated, specific, no hedging \
language ("might", "could potentially", "it's possible that"). Reference concrete \
picks by name and round. Do not restate the stat line verbatim (e.g. don't just say \
"they got a B+ and are projected to go 9-5") -- explain the reasoning behind it. \
Write 120-180 words, one paragraph, no headers or bullet points."""


def _build_user_prompt(
    team_name: str,
    grade: TeamGradeResult,
    projected_record: ProjectedRecord | str,
) -> str:
    pick_lines = []
    for p in grade.picks:
        rank_note = f", ranked ~{p.rank:.0f}" if p.rank is not None else ""
        pick_lines.append(f"  Round {p.round}, Pick {p.pick_no}: {p.name} ({p.position}){rank_note}")

    value_lines = "\n".join(
        f"  {p.name} (Round {p.round}, Pick {p.pick_no}, ranked ~{p.rank:.0f})" for p in grade.best_value_picks
    ) or "  None standout"
    reach_lines = "\n".join(
        f"  {p.name} (Round {p.round}, Pick {p.pick_no}, ranked ~{p.rank:.0f})" for p in grade.reaches
    ) or "  None standout"
    gap_lines = ", ".join(grade.positional_gaps) or "None"

    record_str = str(projected_record)

    return f"""Team: {team_name}
Letter grade: {grade.letter_grade}

Grade components (0-100 scale, higher is better):
  Value captured vs. ADP: {grade.normalized_components.get('value', 0):.0f}
  Positional need coverage: {grade.normalized_components.get('need', 0):.0f}
  Roster balance / bench depth: {grade.normalized_components.get('balance', 0):.0f}
  Upside vs. floor mix: {grade.normalized_components.get('upside', 0):.0f}

Positional gaps: {gap_lines}

Full pick list:
{chr(10).join(pick_lines)}

Standout value picks:
{value_lines}

Reaches:
{reach_lines}

Projected regular-season record: {record_str}

Write the recap paragraph for this team now."""


def _fallback_summary(team_name: str, grade: TeamGradeResult, projected_record) -> str:
    value_bit = (
        f"Their best value pick was {grade.best_value_picks[0].name} in round {grade.best_value_picks[0].round}."
        if grade.best_value_picks
        else ""
    )
    reach_bit = (
        f"Their biggest reach was {grade.reaches[0].name} in round {grade.reaches[0].round}."
        if grade.reaches
        else ""
    )
    gap_bit = f"Notable gaps: {', '.join(grade.positional_gaps)}." if grade.positional_gaps else ""
    return (
        f"{team_name} drafted to a {grade.letter_grade} grade, projected to finish "
        f"{projected_record}. {value_bit} {reach_bit} {gap_bit}"
    ).strip()


def generate_team_paragraph(
    client: Anthropic,
    team_name: str,
    grade: TeamGradeResult,
    projected_record,
    model: str = DEFAULT_MODEL,
) -> str:
    prompt = _build_user_prompt(team_name, grade, projected_record)

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            if text:
                return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("Claude API call failed for %s (attempt %d/2): %s", team_name, attempt + 1, exc)

    logger.warning("Falling back to plain-text summary for %s after repeated API failures.", team_name)
    return _fallback_summary(team_name, grade, projected_record)


def generate_all_paragraphs(
    api_key: str,
    grades: dict[int, TeamGradeResult],
    projected_records: dict[int, ProjectedRecord | str],
    model: str = DEFAULT_MODEL,
) -> dict[int, str]:
    client = Anthropic(api_key=api_key)
    paragraphs = {}
    for roster_id, grade in grades.items():
        record = projected_records.get(roster_id, "N/A")
        paragraphs[roster_id] = generate_team_paragraph(
            client, grade.team_name, grade, record, model=model
        )
    return paragraphs
