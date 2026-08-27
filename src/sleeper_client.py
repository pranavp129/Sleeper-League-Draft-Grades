"""All Sleeper API calls live here.

Sleeper's API (https://api.sleeper.app/v1) is public, read-only, and requires
no API key or auth of any kind.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://api.sleeper.app/v1"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
PLAYERS_CACHE_PATH = CACHE_DIR / "players.json"
PLAYERS_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60

_session = requests.Session()


def _get(path: str) -> Any:
    url = f"{BASE_URL}{path}"
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_league(league_id: str) -> dict:
    return _get(f"/league/{league_id}")


def get_league_users(league_id: str) -> list[dict]:
    return _get(f"/league/{league_id}/users")


def get_league_rosters(league_id: str) -> list[dict]:
    return _get(f"/league/{league_id}/rosters")


def get_league_drafts(league_id: str) -> list[dict]:
    return _get(f"/league/{league_id}/drafts")


def get_draft_picks(draft_id: str) -> list[dict]:
    return _get(f"/draft/{draft_id}/picks")


def get_league_matchups(league_id: str, week: int) -> list[dict]:
    return _get(f"/league/{league_id}/matchups/{week}")


def get_all_players(force_refresh: bool = False) -> dict:
    """Fetch the full Sleeper player dump (~5MB), cached to disk for 24h.

    Sleeper's own guidance is to call this endpoint at most once a day.
    Never call this inside a per-pick loop.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not force_refresh and PLAYERS_CACHE_PATH.exists():
        age = time.time() - PLAYERS_CACHE_PATH.stat().st_mtime
        if age < PLAYERS_CACHE_MAX_AGE_SECONDS:
            with open(PLAYERS_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)

    data = _get("/players/nfl")
    with open(PLAYERS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def get_current_draft(league_id: str) -> dict | None:
    """Return the most recent draft for a league, or None if none exists."""
    drafts = get_league_drafts(league_id)
    if not drafts:
        return None
    drafts_sorted = sorted(drafts, key=lambda d: d.get("start_time") or 0, reverse=True)
    return drafts_sorted[0]
