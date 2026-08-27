"""ADP fetch (Fantasy Football Calculator) + crosswalk matching to sleeper_id.

Produces a normalized ranking table: sleeper_id -> {rank, position, name, source}.
Downstream grading code never needs to know whether a given player's rank came
from the live ADP pull or the manual CSV fallback.

Matching pipeline:
  1. Pull ADP from FFC (player names, not Sleeper IDs).
  2. Resolve each name to a sleeper_id via the DynastyProcess player-id crosswalk,
     matched on the crosswalk's pre-normalized `merge_name` column.
  3. Team defenses skip the crosswalk entirely and join on team abbreviation,
     since the crosswalk contains no DEF rows.
  4. Anything still unmatched falls back to rapidfuzz against the crosswalk's
     merge_name column, accepted only above a high confidence threshold.
  5. Unmatched entries are logged and excluded, not silently dropped without a trace.
  6. data/rankings.csv (optional) overrides/fills in on top of all of the above.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache"
PLAYERIDS_CACHE_PATH = CACHE_DIR / "playerids.csv"
PLAYERIDS_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
MANUAL_RANKINGS_PATH = ROOT_DIR / "data" / "rankings.csv"

FFC_ADP_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}"
DYNASTYPROCESS_CROSSWALK_URL = (
    "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
)

FUZZY_MATCH_THRESHOLD = 90  # 0-100, rapidfuzz token_sort_ratio; only accept high-confidence matches

_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv)\b\.?", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_NON_ALNUM_KEEP_HYPHEN_RE = re.compile(r"[^a-z0-9\s-]")


@dataclass
class RankedPlayer:
    sleeper_id: str
    position: str
    rank: float
    name: str
    source: str  # "adp" | "manual"
    stdev: float | None = None  # ADP standard deviation, used as an upside/volatility proxy


def normalize_name(name: str, keep_hyphens: bool = True) -> str:
    """Lowercase, strip periods/apostrophes and Jr/Sr/II/III suffixes.

    Matches DynastyProcess's merge_name convention: most hyphenated surnames
    (e.g. "Smith-Njigba", "Croskey-Merritt") keep the hyphen in merge_name,
    but a handful of entries (e.g. "Amon-Ra St. Brown" -> "amon ra st brown")
    have the hyphen collapsed to a space instead -- there's no reliable rule
    to predict which, so callers should try both via `normalize_name_variants`.
    """
    name = name.lower().strip()
    name = name.replace(".", "").replace("'", "")
    name = _SUFFIX_RE.sub("", name)
    if keep_hyphens:
        name = _NON_ALNUM_KEEP_HYPHEN_RE.sub("", name)
    else:
        name = _NON_ALNUM_RE.sub("", name.replace("-", " "))
    name = re.sub(r"\s+", " ", name).strip()
    return name


def normalize_name_variants(name: str) -> list[str]:
    """All normalized forms worth trying against merge_name, in priority order."""
    hyphenated = normalize_name(name, keep_hyphens=True)
    despaced = normalize_name(name, keep_hyphens=False)
    return [hyphenated] if hyphenated == despaced else [hyphenated, despaced]


def fetch_ffc_adp(team_count: int, year: int, fmt: str = "half-ppr") -> list[dict]:
    """Pull ADP from Fantasy Football Calculator. Returns the raw `players` list.

    Called at most once per run -- FFC data only refreshes daily anyway.
    """
    url = FFC_ADP_URL.format(fmt=fmt)
    resp = requests.get(url, params={"teams": team_count, "year": year}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "Success":
        raise RuntimeError(f"FFC ADP API returned non-success status: {payload.get('status')}")
    return payload.get("players", [])


def fetch_dynastyprocess_crosswalk(force_refresh: bool = False) -> list[dict]:
    """Fetch + cache the DynastyProcess player-id crosswalk CSV (24h cache)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not force_refresh and PLAYERIDS_CACHE_PATH.exists():
        age = time.time() - PLAYERIDS_CACHE_PATH.stat().st_mtime
        if age < PLAYERIDS_CACHE_MAX_AGE_SECONDS:
            with open(PLAYERIDS_CACHE_PATH, "r", encoding="utf-8") as f:
                return list(csv.DictReader(f))

    resp = requests.get(DYNASTYPROCESS_CROSSWALK_URL, timeout=60)
    resp.raise_for_status()
    text = resp.text
    with open(PLAYERIDS_CACHE_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return list(csv.DictReader(io.StringIO(text)))


def build_merge_name_index(crosswalk_rows: list[dict]) -> dict[str, dict]:
    """Index crosswalk rows by merge_name, resolving collisions.

    Multiple rows can share an identical merge_name (e.g. an active player and a
    retired/historical player of the same name). Prefer the row with a non-null
    sleeper_id; if still ambiguous, the first non-null-sleeper_id row wins (callers
    that need team-based disambiguation should do it themselves via
    `find_crosswalk_candidates`).
    """
    index: dict[str, dict] = {}
    for row in crosswalk_rows:
        merge_name = (row.get("merge_name") or "").strip()
        if not merge_name:
            continue
        existing = index.get(merge_name)
        if existing is None:
            index[merge_name] = row
            continue
        existing_has_id = bool(existing.get("sleeper_id")) and existing.get("sleeper_id") != "NA"
        new_has_id = bool(row.get("sleeper_id")) and row.get("sleeper_id") != "NA"
        if new_has_id and not existing_has_id:
            index[merge_name] = row
    return index


def find_crosswalk_candidates(crosswalk_rows: list[dict], merge_name: str) -> list[dict]:
    return [r for r in crosswalk_rows if (r.get("merge_name") or "").strip() == merge_name]


def _valid_sleeper_id(value: str | None) -> str | None:
    if not value or value == "NA":
        return None
    return value


def _lookup_merge_name(
    merge_index: dict[str, dict],
    all_merge_names: list[str],
    name: str,
) -> tuple[dict | None, str | None]:
    """Try each normalized variant of `name` against the merge_name index, then
    fall back to a high-confidence rapidfuzz match. Returns (row, merge_name_used)."""
    for variant in normalize_name_variants(name):
        row = merge_index.get(variant)
        if row is not None:
            return row, variant

    primary = normalize_name(name)
    candidates = process.extract(primary, all_merge_names, scorer=fuzz.token_sort_ratio, limit=3)
    best = candidates[0] if candidates else None
    if best and best[1] >= FUZZY_MATCH_THRESHOLD:
        logger.info("Fuzzy-matched name %r -> crosswalk %r (score=%s)", name, best[0], best[1])
        return merge_index.get(best[0]), best[0]

    return None, None


def match_adp_to_sleeper_ids(
    adp_players: list[dict],
    crosswalk_rows: list[dict],
    team_abbrev_to_def_id: dict[str, str],
) -> tuple[list[RankedPlayer], list[dict]]:
    """Resolve each FFC ADP entry to a sleeper_id.

    Returns (matched, unmatched) where unmatched entries are the raw FFC dicts
    that couldn't be confidently resolved (for logging).
    """
    merge_index = build_merge_name_index(crosswalk_rows)
    all_merge_names = list(merge_index.keys())

    matched: list[RankedPlayer] = []
    unmatched: list[dict] = []

    for entry in adp_players:
        name = entry.get("name", "")
        position = entry.get("position", "")
        team = (entry.get("team") or "").upper()
        adp = entry.get("adp")
        stdev = entry.get("stdev")

        if position == "DEF":
            def_id = team_abbrev_to_def_id.get(team)
            if def_id:
                matched.append(
                    RankedPlayer(sleeper_id=def_id, position="DEF", rank=adp, name=name, source="adp", stdev=stdev)
                )
            else:
                unmatched.append(entry)
            continue

        row, matched_merge_name = _lookup_merge_name(merge_index, all_merge_names, name)

        sleeper_id = _valid_sleeper_id(row.get("sleeper_id")) if row else None
        if sleeper_id is None:
            # Disambiguate by team if merge_name matched multiple rows, none carrying sleeper_id.
            if row is not None:
                candidates = find_crosswalk_candidates(crosswalk_rows, matched_merge_name)
                team_matches = [
                    c for c in candidates
                    if _valid_sleeper_id(c.get("sleeper_id")) and (c.get("team") or "").upper() == team
                ]
                if team_matches:
                    sleeper_id = _valid_sleeper_id(team_matches[0].get("sleeper_id"))

        if sleeper_id is None:
            unmatched.append(entry)
            continue

        matched.append(
            RankedPlayer(sleeper_id=sleeper_id, position=position, rank=adp, name=name, source="adp", stdev=stdev)
        )

    return matched, unmatched


def load_manual_rankings(path: Path = MANUAL_RANKINGS_PATH) -> list[dict]:
    """Load data/rankings.csv (columns: player_name, position, rank). Optional."""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows


def match_manual_rankings_to_sleeper_ids(
    manual_rows: list[dict],
    crosswalk_rows: list[dict],
    team_abbrev_to_def_id: dict[str, str],
) -> tuple[list[RankedPlayer], list[dict]]:
    merge_index = build_merge_name_index(crosswalk_rows)
    all_merge_names = list(merge_index.keys())
    matched: list[RankedPlayer] = []
    unmatched: list[dict] = []

    for row in manual_rows:
        name = row.get("player_name", "")
        position = (row.get("position") or "").upper()
        rank_raw = row.get("rank")
        try:
            rank = float(rank_raw)
        except (TypeError, ValueError):
            unmatched.append(row)
            continue

        if position == "DEF":
            team = normalize_name(name).upper()
            def_id = team_abbrev_to_def_id.get(team) or team_abbrev_to_def_id.get(name.upper())
            if def_id:
                matched.append(
                    RankedPlayer(sleeper_id=def_id, position="DEF", rank=rank, name=name, source="manual")
                )
            else:
                unmatched.append(row)
            continue

        crosswalk_row, _ = _lookup_merge_name(merge_index, all_merge_names, name)
        sleeper_id = _valid_sleeper_id(crosswalk_row.get("sleeper_id")) if crosswalk_row else None
        if sleeper_id is None:
            unmatched.append(row)
            continue

        matched.append(
            RankedPlayer(sleeper_id=sleeper_id, position=position, rank=rank, name=name, source="manual")
        )

    return matched, unmatched


def build_team_abbrev_to_def_id(all_players: dict) -> dict[str, str]:
    """Sleeper represents each team defense as a player whose player_id is the
    team abbreviation itself (e.g. player_id "SF" for the 49ers defense)."""
    result: dict[str, str] = {}
    for player_id, player in all_players.items():
        if player.get("position") == "DEF":
            team = player.get("team") or player_id
            if team:
                result[team.upper()] = player_id
    return result


def build_rankings(
    league_id: str,
    team_count: int,
    year: int,
    all_players: dict,
    adp_format: str = "half-ppr",
) -> tuple[dict[str, RankedPlayer], bool]:
    """Build the final sleeper_id -> RankedPlayer table.

    Returns (rankings_by_sleeper_id, live_adp_succeeded).
    Manual CSV entries take priority over live ADP for the same player, and fill
    in anyone the live pull couldn't confidently resolve. If both are unavailable
    for a player, that player is simply excluded from value/rank grading.
    """
    team_abbrev_to_def_id = build_team_abbrev_to_def_id(all_players)

    live_matched: list[RankedPlayer] = []
    live_unmatched: list[dict] = []
    live_adp_succeeded = True
    crosswalk_rows: list[dict] = []

    try:
        adp_players = fetch_ffc_adp(team_count=team_count, year=year, fmt=adp_format)
        crosswalk_rows = fetch_dynastyprocess_crosswalk()
        live_matched, live_unmatched = match_adp_to_sleeper_ids(
            adp_players, crosswalk_rows, team_abbrev_to_def_id
        )
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash the run
        logger.warning("Live ADP pull failed (%s); falling back to manual rankings.csv only.", exc)
        live_adp_succeeded = False
        try:
            crosswalk_rows = fetch_dynastyprocess_crosswalk()
        except Exception:  # noqa: BLE001
            crosswalk_rows = []

    if live_unmatched:
        logger.warning("%d ADP entries could not be confidently matched to a sleeper_id:", len(live_unmatched))
        for entry in live_unmatched:
            logger.warning("  unmatched: %s (%s, %s)", entry.get("name"), entry.get("position"), entry.get("team"))

    manual_rows = load_manual_rankings()
    manual_matched, manual_unmatched = match_manual_rankings_to_sleeper_ids(
        manual_rows, crosswalk_rows, team_abbrev_to_def_id
    )
    if manual_unmatched:
        logger.warning("%d manual rankings.csv entries could not be matched:", len(manual_unmatched))
        for row in manual_unmatched:
            logger.warning("  unmatched: %s", row.get("player_name"))

    rankings: dict[str, RankedPlayer] = {}
    for rp in live_matched:
        rankings[rp.sleeper_id] = rp
    # Manual entries override live ADP for the same player.
    for rp in manual_matched:
        rankings[rp.sleeper_id] = rp

    return rankings, live_adp_succeeded
