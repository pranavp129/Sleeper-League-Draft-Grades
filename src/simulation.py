"""Monte Carlo projected-record simulation.

Each team's weekly score is modeled as its power score plus random variance,
so a good-not-great team can still lose to a bad team some weeks. The whole
season is simulated N times and results are averaged into a projected record.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from . import sleeper_client

logger = logging.getLogger(__name__)

DEFAULT_N_SIMULATIONS = 10_000
SCORE_STDEV = 12.0  # variance applied to each team's weekly power score


@dataclass
class ProjectedRecord:
    avg_wins: float
    avg_losses: float

    def __str__(self) -> str:
        return f"{self.avg_wins:.1f}-{self.avg_losses:.1f}"


def get_regular_season_weeks(league: dict) -> list[int]:
    settings = league.get("settings", {}) or {}
    playoff_week_start = settings.get("playoff_week_start")
    if not playoff_week_start or playoff_week_start < 2:
        return []
    return list(range(1, playoff_week_start))


def fetch_schedule(league_id: str, weeks: list[int]) -> dict[int, list[tuple[int, int]]] | None:
    """Build {week: [(roster_id_a, roster_id_b), ...]} from Sleeper matchups.

    Returns None if Sleeper hasn't populated the schedule yet (e.g. run
    immediately post-draft, before the commissioner has generated it).
    """
    if not weeks:
        return None

    schedule: dict[int, list[tuple[int, int]]] = {}
    any_matchups_found = False

    for week in weeks:
        try:
            entries = sleeper_client.get_league_matchups(league_id, week)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully, don't crash the run
            logger.warning("Failed to fetch matchups for week %d: %s", week, exc)
            schedule[week] = []
            continue

        if not entries:
            schedule[week] = []
            continue

        by_matchup_id: dict[int, list[int]] = {}
        for entry in entries:
            matchup_id = entry.get("matchup_id")
            roster_id = entry.get("roster_id")
            if matchup_id is None or roster_id is None:
                continue
            by_matchup_id.setdefault(matchup_id, []).append(roster_id)

        pairs = [tuple(ids) for ids in by_matchup_id.values() if len(ids) == 2]
        if pairs:
            any_matchups_found = True
        schedule[week] = pairs

    if not any_matchups_found:
        return None

    return schedule


def simulate_season(
    power_scores: dict[int, float],
    schedule: dict[int, list[tuple[int, int]]],
    n_simulations: int = DEFAULT_N_SIMULATIONS,
    score_stdev: float = SCORE_STDEV,
    rng: random.Random | None = None,
) -> dict[int, ProjectedRecord]:
    rng = rng or random.Random()
    total_wins: dict[int, int] = {rid: 0 for rid in power_scores}
    total_games: dict[int, int] = {rid: 0 for rid in power_scores}

    weeks_with_games = [w for w, pairs in schedule.items() if pairs]

    for _ in range(n_simulations):
        for week in weeks_with_games:
            for roster_a, roster_b in schedule[week]:
                if roster_a not in power_scores or roster_b not in power_scores:
                    continue
                score_a = power_scores[roster_a] + rng.gauss(0, score_stdev)
                score_b = power_scores[roster_b] + rng.gauss(0, score_stdev)
                total_games[roster_a] += 1
                total_games[roster_b] += 1
                if score_a >= score_b:
                    total_wins[roster_a] += 1
                else:
                    total_wins[roster_b] += 1

    records = {}
    for roster_id in power_scores:
        games = total_games[roster_id] / n_simulations if n_simulations else 0
        wins = total_wins[roster_id] / n_simulations if n_simulations else 0
        records[roster_id] = ProjectedRecord(avg_wins=wins, avg_losses=max(games - wins, 0.0))
    return records
