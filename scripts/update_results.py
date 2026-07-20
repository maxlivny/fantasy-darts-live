from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "config.json"
PROJECT_PATH = ROOT / "data" / "project.json"
OUTPUT_PATH = ROOT / "site" / "data.json"

SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[–—−-]\s*(\d{1,2})(?!\d)")


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"\[[^\]]*]", "", value)
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return value.replace("ё", "е")


def clean_name(value: str) -> str:
    value = re.sub(r"\[[^\]]*]", "", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n–—-:")
    return value


def known_players(project: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for participant in project.get("participants", []):
        for player in participant.get("players", []):
            cleaned = clean_name(player)
            if cleaned:
                result[normalize_name(cleaned)] = cleaned
    for key in (
        "player_scores",
        "manual_score_overrides",
        "manual_status_overrides",
    ):
        for player in project.get(key, {}):
            cleaned = clean_name(player)
            if cleaned:
                result.setdefault(normalize_name(cleaned), cleaned)
    return result


def resolve_player(text: str, players: dict[str, str]) -> str | None:
    cleaned = clean_name(text)
    norm = normalize_name(cleaned)
    if norm in players:
        return players[norm]

    # Иногда ячейка содержит посев, флаг или дополнительный текст.
    candidates = []
    for key, canonical in players.items():
        if len(key) >= 5 and (key in norm or norm in key):
            candidates.append((len(key), canonical))
    if candidates:
        return max(candidates)[1]
    return None


def infer_round(text: str) -> int | None:
    t = normalize_name(text)
    patterns = [
        (r"\bfinal\b", 5),
        (r"semi", 4),
        (r"quarter|last 8", 3),
        (r"second round|last 16|round of 16", 2),
        (r"first round|last 32|round of 32", 1),
    ]
    for pattern, number in patterns:
        if re.search(pattern, t):
            return number
    match = re.search(r"\b(?:round|r)\s*(\d+)\b", t)
    return int(match.group(1)) if match else None


def parse_tables(html: str, players: dict[str, str]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    matches: list[dict[str, Any]] = []
    seen = set()

    for table in soup.select("table"):
        rows = table.select("tr")
        current_round = infer_round(table.get_text(" ", strip=True))

        for row in rows:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue

            texts = [clean_name(cell.get_text(" ", strip=True)) for cell in cells]
            row_text = " | ".join(texts)
            round_no = infer_round(row_text) or current_round

            score_index = None
            score_pair = None
            for i, text in enumerate(texts):
                found = SCORE_RE.search(text)
                if found:
                    score_index = i
                    score_pair = (int(found.group(1)), int(found.group(2)))
                    break
            if score_index is None or score_pair is None:
                continue

            left_candidates = [
                resolve_player(text, players) for text in texts[:score_index]
            ]
            right_candidates = [
                resolve_player(text, players) for text in texts[score_index + 1 :]
            ]
            left = next((x for x in reversed(left_candidates) if x), None)
            right = next((x for x in right_candidates if x), None)
            if not left or not right or left == right:
                continue

            s1, s2 = score_pair
            if s1 == s2:
                continue
            if s1 > s2:
                winner, loser = left, right
                score = f"{s1}-{s2}"
            else:
                winner, loser = right, left
                score = f"{s2}-{s1}"

            key = (round_no, normalize_name(winner), normalize_name(loser))
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                {
                    "winner": winner,
                    "loser": loser,
                    "score": score,
                    "round": round_no,
                }
            )
    return matches


def fetch_matches(url: str, players: dict[str, str]) -> list[dict[str, Any]]:
    response = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "FantasyDartsLive/1.0 (public standings updater)"},
    )
    response.raise_for_status()
    return parse_tables(response.text, players)


def normalize_stage_scores(stage_scores: Any) -> dict[int, int]:
    """
    Поддерживает оба формата из Fantasy Darts Counter:

    1) Словарь:
       {"1": 0, "2": 3, "3": 6}

    2) Список:
       [0, 3, 6, 10, 14, 18]

    Для списка первый элемент считается очками за 1-й раунд.
    """
    converted: dict[int, int] = {}

    if isinstance(stage_scores, dict):
        for key, value in stage_scores.items():
            try:
                converted[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return converted

    if isinstance(stage_scores, list):
        for index, value in enumerate(stage_scores, start=1):
            try:
                converted[index] = int(value)
            except (TypeError, ValueError):
                continue
        return converted

    return converted


def round_score(stage_scores: Any, wins: int) -> int:
    converted = normalize_stage_scores(stage_scores)
    if not converted:
        return 0

    # После одной победы игрок достигает второго раунда.
    achieved_round = wins + 1
    eligible = [key for key in converted if key <= achieved_round]
    return converted[max(eligible)] if eligible else 0


def calculate(project: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    settings = project.get("settings", {})
    stage_scores = settings.get("stage_scores", {})
    rating_names = {
        normalize_name(x) for x in settings.get("rating_participants", [])
    }

    wins: dict[str, int] = {}
    eliminated: set[str] = set()
    tournament_players: set[str] = set(project.get("tournament_players", []))

    for match in matches:
        winner = match["winner"]
        loser = match["loser"]
        wins[winner] = wins.get(winner, 0) + 1
        wins.setdefault(loser, wins.get(loser, 0))
        eliminated.add(loser)
        tournament_players.update((winner, loser))

    scores: dict[str, int] = {}
    all_players = {
        player
        for participant in project.get("participants", [])
        for player in participant.get("players", [])
    }
    for player in all_players:
        scores[player] = round_score(stage_scores, wins.get(player, 0))

    # Ручные значения всегда главнее автоматических.
    for player, score in project.get("manual_score_overrides", {}).items():
        scores[player] = int(score)

    manual_status = {
        normalize_name(player): bool(value)
        for player, value in project.get("manual_status_overrides", {}).items()
    }

    standings = []
    for participant in project.get("participants", []):
        name = participant.get("name", "")
        roster = participant.get("players", [])
        prices = participant.get("prices", [])
        roster_rows = []
        total = 0
        alive = 0

        for index, player in enumerate(roster):
            points = int(scores.get(player, 0))
            total += points
            norm = normalize_name(player)

            if norm in manual_status:
                is_alive = not manual_status[norm]
                status = "В ИГРЕ" if is_alive else "ВЫБЫЛ"
            elif player in eliminated:
                is_alive = False
                status = "ВЫБЫЛ"
            else:
                is_alive = True
                status = "В ИГРЕ"

            alive += int(is_alive)
            roster_rows.append(
                {
                    "name": player,
                    "points": points,
                    "status": status,
                    "alive": is_alive,
                    "price": float(prices[index]) if index < len(prices) else 0,
                }
            )

        cost = round(sum(float(x) for x in prices), 2)
        standings.append(
            {
                "name": name,
                "points": total,
                "alive": alive,
                "cost": cost,
                "rating": normalize_name(name) in rating_names,
                "players": roster_rows,
            }
        )

    standings.sort(
        key=lambda item: (-item["points"], item["cost"], normalize_name(item["name"]))
    )
    for place, item in enumerate(standings, start=1):
        item["place"] = place

    return {
        "tournament": settings.get("tournament_name", "Fantasy Darts"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "standings": standings,
        "matches": list(reversed(matches[-12:])),
        "matches_count": len(matches),
    }


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    project = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))

    if not project.get("participants"):
        print(
            "В data/project.json пока нет участников. "
            "Сохраните проект в Fantasy Darts Counter и замените файл.",
            file=sys.stderr,
        )

    players = known_players(project)
    matches = fetch_matches(config["tournament_url"], players) if players else []
    output = calculate(project, matches)
    output["site_title"] = config.get("site_title", output["tournament"])
    output["refresh_seconds"] = int(config.get("refresh_seconds", 60))
    output["source_url"] = config.get("tournament_url", "")
    output["source_label"] = config.get("source_label", "Источник")

    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"Обновлено: участников {len(output['standings'])}, "
        f"матчей {len(matches)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
