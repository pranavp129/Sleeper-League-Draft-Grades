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

BASE_SYSTEM_PROMPT = """You are a confident sports-analyst columnist writing post-draft \
recap paragraphs for a fantasy football league's report-card website. Write in the \
voice of a beat writer grading a real draft class: opinionated, specific, no hedging \
language ("might", "could potentially", "it's possible that"). Reference concrete \
picks by name and round. Do not restate the stat line verbatim (e.g. don't just say \
"they got a B+ and are projected to go 9-5") -- explain the reasoning behind it. \
If a team punted kicker and/or defense entirely, treat that as a normal, common \
strategic choice (streaming those positions off waivers all season) -- do not \
call it a mistake, a hole, or a wound. It's fine to note in passing at most. \
Write 120-180 words, one paragraph, no headers or bullet points."""

REDRAFT_SYSTEM_PROMPT = BASE_SYSTEM_PROMPT + """ This is a REDRAFT league -- rosters \
reset next season, so grade purely on this year: value vs. this year's ADP, \
lineup fit, and weekly floor/ceiling. A player's age or long-term outlook is \
irrelevant here."""

DYNASTY_SYSTEM_PROMPT = BASE_SYSTEM_PROMPT + """ This is a DYNASTY league -- rosters \
carry over indefinitely, so grade for long-term asset value, not just this \
season. Rank/value figures are already dynasty ADP (the market's age-adjusted \
consensus), so a rookie or ascending 23-year-old taken above ADP is still a \
reach, and a declining veteran taken at "good value" relative to ADP can still \
be a bad long-term asset -- weigh both. Praise teams that accumulated youth and \
draft capital even if it costs immediate production; flag teams that paid up \
for aging veterans even if those picks look fine by this year's output alone. \
Reference player ages where given -- they're central to a dynasty grade."""

SYSTEM_PROMPTS = {"redraft": REDRAFT_SYSTEM_PROMPT, "dynasty": DYNASTY_SYSTEM_PROMPT}


def _pick_descriptor(p, include_age: bool) -> str:
    rank_note = f", ranked ~{p.rank:.0f}" if p.rank is not None else ""
    age_note = f", age {p.age:.0f}" if include_age and p.age is not None else ""
    return f"{p.name} (Round {p.round}, Pick {p.pick_no}, {p.position}{rank_note}{age_note})"


def _build_user_prompt(
    team_name: str,
    grade: TeamGradeResult,
    projected_record: ProjectedRecord | str,
    league_format: str = "redraft",
) -> str:
    is_dynasty = league_format == "dynasty"
    pick_lines = [f"  {_pick_descriptor(p, is_dynasty)}" for p in grade.picks]

    value_lines = "\n".join(f"  {_pick_descriptor(p, is_dynasty)}" for p in grade.best_value_picks) or "  None standout"
    reach_lines = "\n".join(f"  {_pick_descriptor(p, is_dynasty)}" for p in grade.reaches) or "  None standout"
    gap_lines = ", ".join(grade.positional_gaps) or "None"
    punted_lines = ", ".join(grade.punted_positions) or "None"

    adp_label = "dynasty ADP" if is_dynasty else "this year's ADP"
    upside_label = "Roster youth / long-term upside" if is_dynasty else "Upside vs. floor mix"

    record_str = str(projected_record)

    return f"""Team: {team_name}
League format: {"Dynasty" if is_dynasty else "Redraft"}
Letter grade: {grade.letter_grade}

Grade components (0-100 scale, higher is better):
  Value captured vs. {adp_label}: {grade.normalized_components.get('value', 0):.0f}
  Positional need coverage: {grade.normalized_components.get('need', 0):.0f}
  Roster balance / bench depth: {grade.normalized_components.get('balance', 0):.0f}
  {upside_label}: {grade.normalized_components.get('upside', 0):.0f}

Positional gaps (real weaknesses): {gap_lines}
Punted positions (skipped by choice, not a weakness -- do not criticize): {punted_lines}

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
    punted_bit = (
        f"Punted {', '.join(grade.punted_positions)} entirely (a fine strategic choice)."
        if grade.punted_positions
        else ""
    )
    return (
        f"{team_name} drafted to a {grade.letter_grade} grade, projected to finish "
        f"{projected_record}. {value_bit} {reach_bit} {gap_bit} {punted_bit}"
    ).strip()


def generate_team_paragraph(
    client: Anthropic,
    team_name: str,
    grade: TeamGradeResult,
    projected_record,
    model: str = DEFAULT_MODEL,
    league_format: str = "redraft",
) -> str:
    prompt = _build_user_prompt(team_name, grade, projected_record, league_format=league_format)
    system_prompt = SYSTEM_PROMPTS.get(league_format, REDRAFT_SYSTEM_PROMPT)

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
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
    league_format: str = "redraft",
) -> dict[int, str]:
    client = Anthropic(api_key=api_key)
    paragraphs = {}
    for roster_id, grade in grades.items():
        record = projected_records.get(roster_id, "N/A")
        paragraphs[roster_id] = generate_team_paragraph(
            client, grade.team_name, grade, record, model=model, league_format=league_format
        )
    return paragraphs
