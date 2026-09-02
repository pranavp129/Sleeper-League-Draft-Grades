"""Deterministic draft grade logic.

The letter grade is computed from actual numbers, never invented by the LLM,
so a rerun with the same inputs produces the same grade. Weights are exposed
as constants here so they're easy to tune without touching the logic.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

# --- Composite weights (must sum to 1.0) -----------------------------------
VALUE_WEIGHT = 0.40       # value captured vs. ADP/rank
NEED_WEIGHT = 0.25        # positional need coverage
BALANCE_WEIGHT = 0.20     # roster balance / bench depth
UPSIDE_WEIGHT = 0.15      # upside vs. floor mix (soft modifier)

# Typical positional distribution for a redraft half-PPR roster, used as the
# baseline for the balance/bench-depth component. K/DEF are deliberately left
# out: streaming them off waivers all year instead of drafting them is a
# legitimate strategy, not a deviation to penalize (shares renormalize to 1.0
# across the remaining positions).
TYPICAL_POSITION_SHARE = {
    "QB": 0.115,
    "RB": 0.345,
    "WR": 0.402,
    "TE": 0.138,
}

GLARING_GAP_ROUND_THRESHOLD = 8  # a required position with no pick by this round is a gap
LATE_ROUND_POSITIONS = {"K", "DEF"}  # conventionally drafted last, or skipped entirely -- see below
GAP_PENALTY = 0.15  # per missing/late skill-position (QB/RB/WR/TE) gap
LATE_ROUND_GAP_PENALTY = 0.02  # per skipped K/DEF -- a token ding, not a real penalty

# Ordered worst-to-best is reversed below; index 0 = top grade.
GRADE_SCALE = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]

FLEX_KEYWORDS = {"FLEX", "W/R/T", "W/T", "W/R", "REC_FLEX", "SUPER_FLEX"}
CORE_POSITIONS = {"QB", "RB", "WR", "TE", "DEF", "K"}


@dataclass
class PickRecord:
    pick_no: int
    round: int
    sleeper_id: str
    name: str
    position: str
    rank: float | None
    value: float | None  # pick_no - rank; positive = value gained, negative = reach
    age: float | None = None  # from the crosswalk, most relevant in dynasty mode


@dataclass
class TeamGradeResult:
    roster_id: int
    owner_name: str
    team_name: str
    picks: list[PickRecord]
    value_raw: float | None
    need_raw: float
    balance_raw: float
    upside_raw: float | None
    positional_gaps: list[str]  # missing/late skill positions (QB/RB/WR/TE) -- real weaknesses
    punted_positions: list[str]  # K/DEF skipped entirely -- a neutral strategic choice, not a weakness
    best_value_picks: list[PickRecord]
    reaches: list[PickRecord]
    composite_score: float = 0.0
    letter_grade: str = ""
    normalized_components: dict = field(default_factory=dict)


def _expand_required_positions(roster_positions: list[str]) -> dict[str, float]:
    """Turn league roster_positions into required starter counts per core position.

    FLEX slots are distributed evenly across RB/WR/TE since any of those three
    can fill one, which is the standard convention for this kind of heuristic.
    """
    required: dict[str, float] = defaultdict(float)
    flex_count = 0
    for slot in roster_positions:
        slot = slot.upper()
        if slot in CORE_POSITIONS:
            required[slot] += 1
        elif slot in FLEX_KEYWORDS:
            flex_count += 1
        # BN, IDP slots, etc. are ignored for starter-need purposes

    if flex_count:
        for pos in ("RB", "WR", "TE"):
            required[pos] += flex_count / 3

    return dict(required)


def _team_picks(
    draft_picks: list[dict],
    roster_id: int,
    rankings: dict,
) -> list[PickRecord]:
    records = []
    for pick in draft_picks:
        if pick.get("roster_id") != roster_id:
            continue
        sleeper_id = pick.get("player_id")
        metadata = pick.get("metadata", {}) or {}
        position = (metadata.get("position") or "").upper()
        name = f"{metadata.get('first_name', '')} {metadata.get('last_name', '')}".strip() or sleeper_id
        pick_no = pick.get("pick_no")
        round_no = pick.get("round")

        ranked = rankings.get(sleeper_id)
        rank = ranked.rank if ranked else None
        value = (pick_no - rank) if (rank is not None and pick_no is not None) else None

        records.append(
            PickRecord(
                pick_no=pick_no,
                round=round_no,
                sleeper_id=sleeper_id,
                name=name,
                position=position or (ranked.position if ranked else ""),
                rank=rank,
                value=value,
                age=ranked.age if ranked else None,
            )
        )
    records.sort(key=lambda r: (r.pick_no is None, r.pick_no))
    return records


def _value_component(picks: list[PickRecord]) -> float | None:
    values = [p.value for p in picks if p.value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _need_component(picks: list[PickRecord], required: dict[str, float]) -> tuple[float, list[str]]:
    drafted_counts = Counter(p.position for p in picks if p.position)
    first_round_by_position: dict[str, int] = {}
    for p in picks:
        if p.position and p.round is not None:
            if p.position not in first_round_by_position or p.round < first_round_by_position[p.position]:
                first_round_by_position[p.position] = p.round

    ratios = []
    gaps = []
    late_round_gaps = []
    for pos, need in required.items():
        if need <= 0:
            continue
        have = drafted_counts.get(pos, 0)

        if pos in LATE_ROUND_POSITIONS:
            # Skipping K/DEF on draft day is a legitimate strategy (stream off
            # waivers all year) -- it doesn't factor into the coverage ratio at
            # all, and only earns a token ding, not a real positional-gap penalty.
            if have == 0:
                late_round_gaps.append(pos)
            continue

        ratios.append(min(have / need, 1.0))
        first_round = first_round_by_position.get(pos)
        if have == 0 or (first_round is not None and first_round > GLARING_GAP_ROUND_THRESHOLD):
            gaps.append(pos)

    coverage = sum(ratios) / len(ratios) if ratios else 1.0
    gap_penalty = GAP_PENALTY * len(gaps) + LATE_ROUND_GAP_PENALTY * len(late_round_gaps)
    score = max(coverage - gap_penalty, 0.0)
    return score, gaps, late_round_gaps


def _balance_component(picks: list[PickRecord]) -> float:
    total = len([p for p in picks if p.position])
    if total == 0:
        return 0.0
    drafted_counts = Counter(p.position for p in picks if p.position in TYPICAL_POSITION_SHARE)
    deviation = 0.0
    for pos, typical_share in TYPICAL_POSITION_SHARE.items():
        actual_share = drafted_counts.get(pos, 0) / total
        deviation += (actual_share - typical_share) ** 2
    # Lower deviation is better; invert to a "higher is better" raw score.
    return -deviation


def _upside_component(picks: list[PickRecord], upside_metric_by_sleeper_id: dict[str, float]) -> float | None:
    """Average a per-player "higher is better" upside metric across the roster.

    What the metric actually represents is decided by the caller: redraft mode
    passes ADP standard deviation (boom/bust volatility), dynasty mode passes
    negative age (younger roster = higher score) -- see main.py.
    """
    values = [upside_metric_by_sleeper_id[p.sleeper_id] for p in picks if p.sleeper_id in upside_metric_by_sleeper_id]
    if not values:
        return None
    return sum(values) / len(values)


def _normalize(raw_by_roster: dict[int, float | None]) -> dict[int, float]:
    """Min-max normalize a raw metric across teams to a 0-100 scale.

    Teams with no data for this metric get the mean of the others (neutral,
    doesn't drag the team down or up for a component that simply had no signal).
    """
    values = [v for v in raw_by_roster.values() if v is not None]
    if not values:
        return {rid: 50.0 for rid in raw_by_roster}

    lo, hi = min(values), max(values)
    mean = sum(values) / len(values)

    normalized = {}
    for rid, v in raw_by_roster.items():
        if v is None:
            normalized[rid] = 50.0
            continue
        if hi == lo:
            normalized[rid] = 50.0
        else:
            normalized[rid] = 100.0 * (v - lo) / (hi - lo)
    return normalized


def grade_all_teams(
    draft_picks: list[dict],
    rosters: list[dict],
    users: list[dict],
    league: dict,
    rankings: dict,
    upside_metric_by_sleeper_id: dict[str, float] | None = None,
) -> dict[int, TeamGradeResult]:
    upside_metric_by_sleeper_id = upside_metric_by_sleeper_id or {}
    roster_positions = league.get("roster_positions", [])
    required = _expand_required_positions(roster_positions)

    user_by_id = {u["user_id"]: u for u in users}

    results: dict[int, TeamGradeResult] = {}
    value_raw: dict[int, float | None] = {}
    need_raw: dict[int, float] = {}
    balance_raw: dict[int, float] = {}
    upside_raw: dict[int, float | None] = {}

    for roster in rosters:
        roster_id = roster["roster_id"]
        owner_id = roster.get("owner_id")
        user = user_by_id.get(owner_id, {})
        display_name = user.get("display_name") or user.get("username") or f"Team {roster_id}"
        team_name = (user.get("metadata") or {}).get("team_name") or display_name

        picks = _team_picks(draft_picks, roster_id, rankings)
        v = _value_component(picks)
        n, gaps, punted = _need_component(picks, required)
        b = _balance_component(picks)
        u = _upside_component(picks, upside_metric_by_sleeper_id)

        value_raw[roster_id] = v
        need_raw[roster_id] = n
        balance_raw[roster_id] = b
        upside_raw[roster_id] = u

        picks_with_value = [p for p in picks if p.value is not None]
        best_value = sorted(picks_with_value, key=lambda p: p.value, reverse=True)[:2]
        reaches = sorted(picks_with_value, key=lambda p: p.value)[:2]
        reaches = [p for p in reaches if p.value < 0]
        best_value = [p for p in best_value if p.value > 0]

        results[roster_id] = TeamGradeResult(
            roster_id=roster_id,
            owner_name=display_name,
            team_name=team_name,
            picks=picks,
            value_raw=v,
            need_raw=n,
            balance_raw=b,
            upside_raw=u,
            positional_gaps=gaps,
            punted_positions=punted,
            best_value_picks=best_value,
            reaches=reaches,
        )

    value_norm = _normalize(value_raw)
    need_norm = _normalize(need_raw)
    balance_norm = _normalize(balance_raw)
    upside_norm = _normalize(upside_raw)

    composite_by_roster: dict[int, float] = {}
    for roster_id, result in results.items():
        composite = (
            VALUE_WEIGHT * value_norm[roster_id]
            + NEED_WEIGHT * need_norm[roster_id]
            + BALANCE_WEIGHT * balance_norm[roster_id]
            + UPSIDE_WEIGHT * upside_norm[roster_id]
        )
        result.composite_score = composite
        result.normalized_components = {
            "value": value_norm[roster_id],
            "need": need_norm[roster_id],
            "balance": balance_norm[roster_id],
            "upside": upside_norm[roster_id],
        }
        composite_by_roster[roster_id] = composite

    _assign_letter_grades(results, composite_by_roster)
    return results


def _assign_letter_grades(results: dict[int, TeamGradeResult], composite_by_roster: dict[int, float]) -> None:
    n_teams = len(composite_by_roster)
    if n_teams == 0:
        return
    ranked = sorted(composite_by_roster.items(), key=lambda kv: kv[1], reverse=True)

    for rank, (roster_id, _score) in enumerate(ranked):
        percentile = 1.0 if n_teams == 1 else 1.0 - (rank / (n_teams - 1))
        grade_index = round((1.0 - percentile) * (len(GRADE_SCALE) - 1))
        grade_index = max(0, min(grade_index, len(GRADE_SCALE) - 1))
        results[roster_id].letter_grade = GRADE_SCALE[grade_index]
