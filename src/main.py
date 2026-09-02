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
        "--format",
        choices=["redraft", "dynasty"],
        required=True,
        help=(
            "Redraft or dynasty league. Changes the ADP source (this year's vs. dynasty "
            "consensus), the upside grade component (boom/bust volatility vs. roster youth), "
            "and how the recap paragraphs reason about picks. No default -- always specify."
        ),
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="URL slug for this league's page (docs/<slug>/). Defaults to a slugified league name.",
    )
    parser.add_argument(
        "--model",
        default=narrative.DEFAULT_MODEL,
        help=f"Claude model for recap paragraphs (default: {narrative.DEFAULT_MODEL})",
    )
    parser.add_argument("--n-sims", type=int, default=simulation.DEFAULT_N_SIMULATIONS, help="Monte Carlo iterations")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass 24h caches for players + crosswalk")
    parser.add_argument("--skip-narrative", action="store_true", help="Skip Claude calls, use fallback summaries only")
    return parser.parse_args()


def check_scoring_settings(league: dict, league_format: str) -> str | None:
    if league_format == "dynasty":
        # FFC's dynasty ADP is a single blended format, not split by scoring type,
        # so there's nothing to sanity-check it against here.
        return None
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


def count_qb_slots(league: dict) -> int:
    """QB + Superflex slot count, for FantasyCalc's numQbs param (1QB vs. Superflex value sets differ a lot)."""
    roster_positions = league.get("roster_positions", []) or []
    return sum(1 for slot in roster_positions if slot.upper() in {"QB", "SUPER_FLEX"}) or 1


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

    scoring_warning = check_scoring_settings(league, args.format)
    if scoring_warning:
        logger.warning(scoring_warning)

    draft = sleeper_client.get_current_draft(league_id)
    if draft is None:
        logger.error("No draft found for this league.")
        sys.exit(1)
    draft_picks = sleeper_client.get_draft_picks(draft["draft_id"])
    logger.info("Loaded draft %s with %d picks.", draft["draft_id"], len(draft_picks))

    # Sleeper's own draft.settings.player_type: 0 = veteran/full pool, 1 = rookie-only.
    # A rookie-only draft in an ongoing dynasty league only touches a handful of picks
    # per team -- need/balance and the season simulation don't mean the same thing here.
    # Only relevant in dynasty mode; redraft drafts are always the full player pool.
    is_rookie_only_draft = args.format == "dynasty" and draft.get("settings", {}).get("player_type") == 1
    if is_rookie_only_draft:
        logger.info(
            "Detected a rookie-only supplemental draft (%d rounds) -- grading on value + "
            "youth only, and skipping the season simulation (it isn't measuring the "
            "team's actual roster).",
            draft.get("settings", {}).get("rounds", 0),
        )

    logger.info("Loading Sleeper player dump (cached up to 24h)...")
    all_players = sleeper_client.get_all_players(force_refresh=args.force_refresh)

    team_count = league.get("total_rosters") or len(rosters)

    if args.format == "dynasty":
        num_qbs = count_qb_slots(league)
        ppr = (league.get("scoring_settings", {}) or {}).get("rec", 0.5)
        logger.info("Building rankings (FantasyCalc dynasty values, numQbs=%d, ppr=%s)...", num_qbs, ppr)
        ranking_table, live_adp_succeeded = rankings.build_dynasty_rankings(
            team_count=team_count, all_players=all_players, num_qbs=num_qbs, ppr=ppr,
            rookie_only=is_rookie_only_draft,
        )
    else:
        logger.info("Building rankings (FFC half-ppr ADP + DynastyProcess crosswalk)...")
        ranking_table, live_adp_succeeded = rankings.build_rankings(
            league_id=league_id, team_count=team_count, year=args.year, all_players=all_players,
            adp_format="half-ppr",
        )
    logger.info("Resolved %d ranked players.", len(ranking_table))

    if args.format == "dynasty":
        # Younger = higher score once normalized (higher raw value = better, same convention as stdev below).
        upside_metric_by_sleeper_id = {
            sleeper_id: -rp.age for sleeper_id, rp in ranking_table.items() if rp.age is not None
        }
    else:
        upside_metric_by_sleeper_id = {
            sleeper_id: rp.stdev for sleeper_id, rp in ranking_table.items() if rp.stdev is not None
        }

    logger.info("Grading teams...")
    grades = grading.grade_all_teams(
        draft_picks=draft_picks,
        rosters=rosters,
        users=users,
        league=league,
        rankings=ranking_table,
        upside_metric_by_sleeper_id=upside_metric_by_sleeper_id,
        league_format=args.format,
    )

    schedule_unavailable = False
    projected_records: dict[int, simulation.ProjectedRecord | str] = {}
    if is_rookie_only_draft:
        # composite_score here reflects only this rookie class, not the team's actual
        # roster strength -- simulating a season on it would be measuring the wrong thing.
        for roster_id in grades:
            projected_records[roster_id] = ""
    else:
        logger.info("Simulating season...")
        weeks = simulation.get_regular_season_weeks(league)
        schedule = simulation.fetch_schedule(league_id, weeks)
        schedule_unavailable = schedule is None

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
            api_key=api_key, grades=grades, projected_records=projected_records, model=args.model,
            league_format=args.format, is_rookie_only_draft=is_rookie_only_draft,
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
        "league_format": args.format,
        "is_rookie_only_draft": is_rookie_only_draft,
        "year": args.year,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "teams": teams_context,
        "live_adp_failed": not live_adp_succeeded,
        "value_grading_skipped": value_grading_skipped,
        "scoring_warning": scoring_warning,
        "schedule_unavailable": schedule_unavailable,
    }

    slug = args.slug or report.slugify(context["league_name"])
    html_path = report.render_html(context, output_path=report.DOCS_DIR / slug / "index.html")
    md_path = report.render_markdown(context, slug=slug)
    logger.info("Wrote %s", html_path)
    logger.info("Wrote %s", md_path)

    leagues = report.update_leagues_manifest(
        slug=slug, league_name=context["league_name"], year=args.year, generated_at=context["generated_at"]
    )
    hub_path = report.render_hub(leagues)
    logger.info("Wrote %s (%d league%s)", hub_path, len(leagues), "" if len(leagues) == 1 else "s")


if __name__ == "__main__":
    main()
