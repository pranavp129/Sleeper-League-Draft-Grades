"""Orchestrates a full draft-recap run: fetch -> rank -> grade -> simulate -> narrate -> render."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import grading, narrative, report, rankings, simulation, sleeper_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Sleeper post-draft recap report.")
    parser.add_argument("--league-id", default=None, help="Sleeper league ID (overrides SLEEPER_LEAGUE_ID)")
    parser.add_argument("--year", type=int, default=datetime.now().year, help="Season year, for FFC ADP lookup")
    parser.add_argument(
        "--model",
        default=narrative.DEFAULT_MODEL,
        help=f"Claude model for recap paragraphs (default: {narrative.DEFAULT_MODEL})",
    )
    parser.add_argument("--n-sims", type=int, default=simulation.DEFAULT_N_SIMULATIONS, help="Monte Carlo iterations")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass 24h caches for players + crosswalk")
    parser.add_argument("--skip-narrative", action="store_true", help="Skip Claude calls, use fallback summaries only")
    return parser.parse_args()


def check_scoring_settings(league: dict) -> str | None:
    scoring = league.get("scoring_settings", {}) or {}
    rec = scoring.get("rec")
    if rec is None:
        return "Could not read this league's reception scoring — skipping the half-PPR sanity check."
    if abs(rec - 0.5) > 0.01:
        return (
            f"This league's reception scoring is {rec} points, not half-PPR (0.5). "
            "ADP is pulled in half-PPR format, so value/rank grading may be skewed."
        )
    return None


def build_power_scores(grades: dict[int, grading.TeamGradeResult]) -> dict[int, float]:
    return {roster_id: g.composite_score for roster_id, g in grades.items()}


def main() -> None:
    load_dotenv()
    args = parse_args()

    league_id = args.league_id or os.environ.get("SLEEPER_LEAGUE_ID")
    if not league_id:
        logger.error("No league ID provided. Set SLEEPER_LEAGUE_ID in .env or pass --league-id.")
        sys.exit(1)

    logger.info("Fetching league data for league %s...", league_id)
    league = sleeper_client.get_league(league_id)
    users = sleeper_client.get_league_users(league_id)
    rosters = sleeper_client.get_league_rosters(league_id)

    scoring_warning = check_scoring_settings(league)
    if scoring_warning:
        logger.warning(scoring_warning)

    draft = sleeper_client.get_current_draft(league_id)
    if draft is None:
        logger.error("No draft found for this league.")
        sys.exit(1)
    draft_picks = sleeper_client.get_draft_picks(draft["draft_id"])
    logger.info("Loaded draft %s with %d picks.", draft["draft_id"], len(draft_picks))

    logger.info("Loading Sleeper player dump (cached up to 24h)...")
    all_players = sleeper_client.get_all_players(force_refresh=args.force_refresh)

    team_count = league.get("total_rosters") or len(rosters)
    logger.info("Building rankings (FFC ADP + DynastyProcess crosswalk)...")
    ranking_table, live_adp_succeeded = rankings.build_rankings(
        league_id=league_id,
        team_count=team_count,
        year=args.year,
        all_players=all_players,
    )
    logger.info("Resolved %d ranked players.", len(ranking_table))

    adp_stdev_by_sleeper_id = {
        sleeper_id: rp.stdev
        for sleeper_id, rp in ranking_table.items()
        if rp.stdev is not None
    }

    logger.info("Grading teams...")
    grades = grading.grade_all_teams(
        draft_picks=draft_picks,
        rosters=rosters,
        users=users,
        league=league,
        rankings=ranking_table,
        adp_stdev_by_sleeper_id=adp_stdev_by_sleeper_id,
    )

    logger.info("Simulating season...")
    weeks = simulation.get_regular_season_weeks(league)
    schedule = simulation.fetch_schedule(league_id, weeks)
    schedule_unavailable = schedule is None

    projected_records: dict[int, simulation.ProjectedRecord | str] = {}
    if schedule_unavailable:
        logger.warning("Season schedule not available yet -- projected records will be omitted.")
        for roster_id in grades:
            projected_records[roster_id] = ""
    else:
        power_scores = build_power_scores(grades)
        projected_records = simulation.simulate_season(
            power_scores=power_scores, schedule=schedule, n_simulations=args.n_sims
        )

    logger.info("Generating recap paragraphs...")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if args.skip_narrative or not api_key:
        if not args.skip_narrative:
            logger.warning("No ANTHROPIC_API_KEY set -- using fallback plain-text summaries for all teams.")
        paragraphs = {
            roster_id: narrative._fallback_summary(g.team_name, g, projected_records.get(roster_id, "N/A"))
            for roster_id, g in grades.items()
        }
    else:
        paragraphs = narrative.generate_all_paragraphs(
            api_key=api_key, grades=grades, projected_records=projected_records, model=args.model
        )

    teams_context = []
    for roster_id, g in sorted(grades.items(), key=lambda kv: kv[1].composite_score, reverse=True):
        record = projected_records.get(roster_id, "")
        teams_context.append(
            {
                "team_name": g.team_name,
                "owner_name": g.owner_name,
                "letter_grade": g.letter_grade,
                "projected_record": str(record) if record else "",
                "paragraph": paragraphs.get(roster_id, ""),
                "picks": [
                    {
                        "round": p.round,
                        "pick_no": p.pick_no,
                        "name": p.name,
                        "position": p.position,
                        "rank": p.rank,
                        "value": p.value,
                    }
                    for p in g.picks
                ],
            }
        )

    value_grading_skipped = not live_adp_succeeded and len(ranking_table) == 0

    context = {
        "league_name": league.get("name", "Fantasy League"),
        "year": args.year,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "teams": teams_context,
        "live_adp_failed": not live_adp_succeeded,
        "value_grading_skipped": value_grading_skipped,
        "scoring_warning": scoring_warning,
        "schedule_unavailable": schedule_unavailable,
    }

    html_path = report.render_html(context)
    md_path = report.render_markdown(context)
    logger.info("Wrote %s", html_path)
    logger.info("Wrote %s", md_path)


if __name__ == "__main__":
    main()
