from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "config.json"
PROJECT_PATH = ROOT / "data" / "project.json"
OUTPUT_PATH = ROOT / "site" / "data.json"

SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[–—−-]\s*(\d{1,2})(?!\d)")
WALKOVER_RE = re.compile(r"(?:\bw\s*/?\s*o\b|\bwalkover\b|\bwithdrawn\b|\bwithdrew\b|\bretired\b|\babandoned\b)", re.I)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"\[[^\]]*]", "", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
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



def infer_round_from_match_number(
    match_number: int,
    rounds_count: int,
    first_round_matches: int | None = None,
) -> int | None:
    if match_number < 1 or rounds_count < 1:
        return None

    if first_round_matches is None or first_round_matches < 1:
        first_round_matches = 2 ** (rounds_count - 1)
    start = 1

    for round_no in range(1, rounds_count + 1):
        matches_in_round = max(1, first_round_matches // (2 ** (round_no - 1)))
        end = start + matches_in_round - 1
        if start <= match_number <= end:
            return round_no
        start = end + 1

    return None


def clean_wikitext_cell(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"<ref\b[^>]*>.*?</ref>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<ref\b[^>]*/\s*>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)

    def pdc_flag_repl(match: re.Match[str]) -> str:
        parts = match.group(1).split("|")
        return parts[0].strip() if parts else ""

    value = re.sub(
        r"\{\{PDCFlag\|([^{}]+)\}\}",
        pdc_flag_repl,
        value,
        flags=re.I,
    )

    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"\{\{[^{}]*\}\}", " ", value)

    value = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = value.replace("'''", "").replace("''", "")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def is_completed_numeric_score(
    score1: int,
    score2: int,
    required_legs: int | None,
) -> bool:
    if score1 == score2:
        return False

    winner_score = max(score1, score2)
    loser_score = min(score1, score2)

    if required_legs is None:
        return True

    # В дартсе матч заканчивается сразу после достижения нужного числа легов.
    # Разница в два лега не требуется: 6-5, 7-6 и 8-7 являются корректными
    # завершёнными счетами. Live-матчи отбрасываются, пока лидер не достиг
    # лимита текущего раунда.
    return winner_score >= required_legs




def parse_schedule_wikitext(
    wikitext: str,
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
    rounds_count: int,
    first_round_matches: int | None = None,
) -> list[dict[str, Any]]:
    """
    Читает строки Schedule из исходной разметки Wikipedia.

    Ключевое правило: сначала находится ячейка итогового счёта, затем берутся
    ближайший известный игрок слева и ближайший известный игрок справа.
    Это исключает случай, когда в той же строке встречается имя из сноски,
    шаблона или дополнительной статистики.
    """
    matches: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()

    row_pattern = re.compile(
        r"^\|\s*(\d{1,3})\s*\|\|(.*)$",
        flags=re.M,
    )

    for row_match in row_pattern.finditer(wikitext):
        match_number = int(row_match.group(1))
        raw_cells = [cell.strip() for cell in row_match.group(2).split("||")]
        cells = [clean_wikitext_cell(cell) for cell in raw_cells]

        round_no = infer_round_from_match_number(match_number, rounds_count, first_round_matches)
        if round_no is None:
            continue

        # Сначала ищем настоящий итоговый Score.
        score_index: int | None = None
        score_text = ""

        for index, candidate in enumerate(cells):
            if WALKOVER_RE.search(candidate):
                score_index = index
                score_text = candidate
                break

            score_pair = SCORE_RE.search(candidate)
            if score_pair:
                score1 = int(score_pair.group(1))
                score2 = int(score_pair.group(2))

                # Итоговый счёт должен удовлетворять формату данного раунда.
                if is_completed_numeric_score(
                    score1,
                    score2,
                    winning_legs_by_round.get(round_no),
                ):
                    score_index = index
                    score_text = candidate
                    break

        if score_index is None:
            continue

        # Берём ближайшего игрока слева от итогового счёта.
        player1: str | None = None
        for index in range(score_index - 1, -1, -1):
            resolved = resolve_player(cells[index], players)
            if resolved:
                player1 = resolved
                break

        # И ближайшего игрока справа.
        player2: str | None = None
        for index in range(score_index + 1, len(cells)):
            resolved = resolve_player(cells[index], players)
            if resolved:
                player2 = resolved
                break

        if not player1 or not player2 or player1 == player2:
            continue

        is_walkover = bool(WALKOVER_RE.search(score_text))
        score_pair = SCORE_RE.search(score_text)

        if is_walkover:
            winner, loser = player2, player1
            display_score = "w/o"
        elif score_pair:
            score1 = int(score_pair.group(1))
            score2 = int(score_pair.group(2))

            if score1 > score2:
                winner, loser = player1, player2
                display_score = f"{score1}-{score2}"
            else:
                winner, loser = player2, player1
                display_score = f"{score2}-{score1}"
        else:
            continue

        key = (
            round_no,
            normalize_name(winner),
            normalize_name(loser),
        )
        if key in seen:
            continue

        seen.add(key)
        matches.append(
            {
                "winner": winner,
                "loser": loser,
                "score": display_score,
                "round": round_no,
            }
        )

    return matches


def parse_bracket_wikitext(
    wikitext: str,
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
    rounds_count: int,
) -> list[dict[str, Any]]:
    """
    Читает стандартные bracket-шаблоны Wikipedia вида:

        |RD1-team01=Player A
        |RD1-score01=6
        |RD1-team02=Player B
        |RD1-score02=3

    Поддерживает RD1..RDn, номера слотов как с ведущим нулём, так и без него.
    """
    params: dict[tuple[int, str, int], str] = {}

    param_re = re.compile(
        r"(?im)^\s*\|\s*RD(\d+)-(team|score)\s*0*(\d+)\s*=\s*(.*?)\s*$"
    )

    for match in param_re.finditer(str(wikitext or "")):
        round_no = int(match.group(1))
        kind = match.group(2).casefold()
        slot = int(match.group(3))
        if not (1 <= round_no <= rounds_count) or slot < 1:
            continue
        params[(round_no, kind, slot)] = match.group(4).strip()

    if not params:
        return []

    matches: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()

    for round_no in range(1, rounds_count + 1):
        slots = sorted(
            slot
            for (rnd, kind, slot) in params
            if rnd == round_no and kind == "team"
        )
        if not slots:
            continue

        max_slot = max(slots)
        for first_slot in range(1, max_slot + 1, 2):
            second_slot = first_slot + 1

            raw_team1 = params.get((round_no, "team", first_slot), "")
            raw_team2 = params.get((round_no, "team", second_slot), "")
            raw_score1 = params.get((round_no, "score", first_slot), "")
            raw_score2 = params.get((round_no, "score", second_slot), "")

            team1_text = clean_wikitext_cell(raw_team1)
            team2_text = clean_wikitext_cell(raw_team2)
            player1 = resolve_player(team1_text, players)
            player2 = resolve_player(team2_text, players)

            if not player1 or not player2 or player1 == player2:
                continue

            score1_text = clean_wikitext_cell(raw_score1)
            score2_text = clean_wikitext_cell(raw_score2)
            combined_score = f"{score1_text} {score2_text}"

            s1_match = re.search(r"(?<!\d)(\d{1,2})(?!\d)", score1_text)
            s2_match = re.search(r"(?<!\d)(\d{1,2})(?!\d)", score2_text)

            if s1_match and s2_match:
                score1 = int(s1_match.group(1))
                score2 = int(s2_match.group(1))
                required = winning_legs_by_round.get(round_no)

                if not is_completed_numeric_score(score1, score2, required):
                    continue

                if score1 > score2:
                    winner, loser = player1, player2
                    ws, ls = score1, score2
                else:
                    winner, loser = player2, player1
                    ws, ls = score2, score1

                display_score = f"{ws}-{ls}"

            elif WALKOVER_RE.search(combined_score):
                first_walkover = bool(WALKOVER_RE.search(score1_text))
                second_walkover = bool(WALKOVER_RE.search(score2_text))

                if first_walkover and not second_walkover:
                    winner, loser = player1, player2
                elif second_walkover and not first_walkover:
                    winner, loser = player2, player1
                else:
                    continue
                display_score = "w/o"
            else:
                continue

            key = (
                round_no,
                normalize_name(winner),
                normalize_name(loser),
            )
            if key in seen:
                continue

            seen.add(key)
            matches.append(
                {
                    "winner": winner,
                    "loser": loser,
                    "score": display_score,
                    "round": round_no,
                }
            )

    return matches


def wikipedia_page_title(url: str) -> str:
    match = re.search(r"/wiki/([^?#]+)", str(url))
    if not match:
        raise ValueError("Не удалось определить название страницы Wikipedia.")
    return unquote(match.group(1)).replace("_", " ")


def fetch_wikipedia_matches(
    url: str,
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
    rounds_count: int,
    first_round_matches: int | None = None,
) -> list[dict[str, Any]]:
    title = wikipedia_page_title(url)

    response = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "parse",
            "page": title,
            "prop": "wikitext",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        },
        timeout=30,
        headers={"User-Agent": "FantasyDartsLive/3.0 (public standings updater)"},
    )
    response.raise_for_status()

    payload = response.json().get("parse", {})
    wikitext = payload.get("wikitext", "")

    matches = parse_schedule_wikitext(
        wikitext,
        players,
        winning_legs_by_round,
        rounds_count,
        first_round_matches,
    )

    if matches:
        print(f"Wikipedia: распознана таблица Schedule, матчей {len(matches)}.")
        return matches

    matches = parse_bracket_wikitext(
        wikitext,
        players,
        winning_legs_by_round,
        rounds_count,
    )

    if matches:
        round_counts: dict[int, int] = {}
        for item in matches:
            rnd = int(item.get("round", 0))
            round_counts[rnd] = round_counts.get(rnd, 0) + 1
        print(
            f"Wikipedia: распознан bracket, матчей {len(matches)}; "
            f"по раундам {round_counts}."
        )
        return matches

    raise RuntimeError(
        "Wikipedia загрузилась, но не удалось распознать ни таблицу Schedule, "
        "ни bracket-разметку RD1-team/RD1-score."
    )




DARTN_SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:–—−-]\s*(\d{1,2})(?!\d)")


def dartn_round_from_heading(value: str) -> int | None:
    t = normalize_name(value)
    if "halbfinale" in t:
        return 5
    if "viertelfinale" in t:
        return 4
    if "achtelfinale" in t:
        return 3
    if re.search(r"\b2\.\s*runde\b", t):
        return 2
    if re.search(r"\b1\.\s*runde\b", t):
        return 1
    if re.search(r"\bfinale\b", t):
        return 6
    return None


def parse_dartn_text(
    page_text: str,
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    current_round: int | None = None

    for raw_line in str(page_text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue

        heading_round = dartn_round_from_heading(line)
        if heading_round is not None and not DARTN_SCORE_RE.search(line):
            current_round = heading_round
            continue

        score_match = DARTN_SCORE_RE.search(line)
        if not score_match or current_round is None:
            continue

        score1, score2 = int(score_match.group(1)), int(score_match.group(2))
        required = winning_legs_by_round.get(current_round)
        if not is_completed_numeric_score(score1, score2, required):
            continue

        normalized_line = normalize_name(line)
        occurrences: list[tuple[int, int, str]] = []
        for canonical in players.values():
            key = normalize_name(canonical)
            pos = normalized_line.find(key)
            if pos >= 0:
                occurrences.append((pos, pos + len(key), canonical))

        occurrences.sort(key=lambda x: x[0])
        deduped: list[tuple[int, int, str]] = []
        used = set()
        for item in occurrences:
            nk = normalize_name(item[2])
            if nk not in used:
                deduped.append(item)
                used.add(nk)

        if len(deduped) < 2:
            continue

        score_pos = score_match.start()
        left = [x for x in deduped if x[0] < score_pos]
        right = [x for x in deduped if x[0] > score_pos]
        if left and right:
            player1 = left[-1][2]
            player2 = right[0][2]
        else:
            player1, player2 = deduped[0][2], deduped[1][2]

        if normalize_name(player1) == normalize_name(player2):
            continue

        if score1 > score2:
            winner, loser = player1, player2
            ws, ls = score1, score2
        else:
            winner, loser = player2, player1
            ws, ls = score2, score1

        key = (current_round, normalize_name(winner), normalize_name(loser))
        if key in seen:
            continue
        seen.add(key)
        matches.append({
            "winner": winner,
            "loser": loser,
            "score": f"{ws}-{ls}",
            "round": current_round,
        })

    return matches


def fetch_dartn_matches(
    url: str,
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
) -> list[dict[str, Any]]:
    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
            )
        },
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text("\n", strip=True)
    matches = parse_dartn_text(page_text, players, winning_legs_by_round)

    round_counts: dict[int, int] = {}
    for item in matches:
        rnd = int(item.get("round", 0))
        round_counts[rnd] = round_counts.get(rnd, 0) + 1
    print(f"dartn.de: завершённых матчей {len(matches)}; по раундам {round_counts}.")
    return matches

def dartconnect_event_slug(url: str) -> str:
    match = re.search(r"/(?:api/)?event/([^/?#]+)", str(url), flags=re.I)
    if not match:
        raise ValueError(
            "Не удалось определить код турнира DartConnect. "
            "Нужна ссылка вида https://tv.dartconnect.com/event/.../matches"
        )
    return match.group(1)


def dartconnect_score(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if re.fullmatch(r"\d{1,2}", text) else None


def clean_dartconnect_name(value: Any, players: dict[str, str]) -> str | None:
    if isinstance(value, dict):
        for key in ("fullName", "displayName", "name", "playerName", "hcf", "acf", "ch", "ac"):
            if value.get(key):
                value = value[key]
                break
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    if "," in text:
        surname, given = text.split(",", 1)
        text = f"{given.strip()} {surname.strip()}"
    return resolve_player(text, players)


def dartconnect_round(value: Any, rounds_count: int) -> int | None:
    text = normalize_name(str(value or ""))
    if not text:
        return None
    if "final" in text and "semi" not in text:
        return rounds_count
    if "semi" in text:
        return max(1, rounds_count - 1)
    if "quarter" in text or "last 8" in text:
        return max(1, rounds_count - 2)
    m = re.search(r"(?:last|round of|r)\s*(128|64|32|16|8|4|2|1)\b", text)
    if m:
        field = int(m.group(1))
        import math
        return max(1, min(rounds_count, rounds_count - int(math.log2(field)) + 1))
    m = re.fullmatch(r"\D*(\d+)\D*", text)
    if m:
        number = int(m.group(1))
        if 1 <= number <= rounds_count:
            return number
        if number in (128, 64, 32, 16, 8, 4, 2):
            import math
            return max(1, min(rounds_count, rounds_count - int(math.log2(number)) + 1))
    return None


def parse_dartconnect_api(
    data: Any,
    players: dict[str, str],
    rounds_count: int,
) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    payload = data.get("payload", data)
    if not isinstance(payload, dict):
        return []
    completed = payload.get("completed", [])
    if not isinstance(completed, list):
        return []

    def first_value(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = obj.get(key)
            if value not in (None, "", []):
                return value
        return None

    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in completed:
        if not isinstance(item, dict):
            continue
        home = clean_dartconnect_name(first_value(item, (
            "hcf", "ch", "homeFullName", "home_name", "homePlayer", "home", "player1"
        )), players)
        away = clean_dartconnect_name(first_value(item, (
            "acf", "ac", "awayFullName", "away_name", "awayPlayer", "away", "player2"
        )), players)
        if not home or not away or home == away:
            continue
        hs = dartconnect_score(first_value(item, ("hs", "homeScore", "score1", "homelegs", "homeLegs")))
        ass = dartconnect_score(first_value(item, ("as", "awayScore", "score2", "awaylegs", "awayLegs")))
        if hs is None or ass is None:
            m = SCORE_RE.search(str(item.get("ms", "")))
            if m:
                hs, ass = int(m.group(1)), int(m.group(2))
        if hs is None or ass is None or hs == ass:
            continue
        winner, loser = (home, away) if hs > ass else (away, home)
        ws, ls = max(hs, ass), min(hs, ass)
        round_no = dartconnect_round(first_value(item, ("r", "round", "roundCode", "stage")), rounds_count)
        row = {"winner": winner, "loser": loser, "score": f"{ws}-{ls}"}
        if round_no is not None:
            row["round"] = round_no
        key = (winner, loser, row["score"])
        if key not in seen:
            seen.add(key)
            matches.append(row)
    return matches


def dartconnect_round_from_heading(value: str, rounds_count: int) -> int | None:
    text = normalize_name(value)
    labels = {
        128: 1,
        64: 2,
        32: 3,
        16: 4,
        8: 5,
        4: 6,
        2: 7,
    }
    if "final" in text and "semi" not in text:
        return rounds_count
    if "semi" in text:
        return max(1, rounds_count - 1)
    if "quarter" in text:
        return max(1, rounds_count - 2)
    match = re.search(r"(?:top|last|round of)\s*(128|64|32|16|8|4|2)\b", text)
    if match:
        field = int(match.group(1))
        return min(rounds_count, labels.get(field, 1))
    return None


def dartconnect_player_occurrences(line: str, players: dict[str, str]) -> list[tuple[int, int, str]]:
    normalized_line = normalize_name(line)
    found: list[tuple[int, int, str]] = []
    for canonical in players.values():
        aliases = [canonical]
        parts = canonical.split()
        if len(parts) >= 2:
            aliases.append(f"{' '.join(parts[1:])}, {parts[0]}")
        best = None
        for alias in aliases:
            key = normalize_name(alias)
            pos = normalized_line.find(key)
            if pos >= 0 and (best is None or len(key) > best[1] - best[0]):
                best = (pos, pos + len(key), canonical)
        if best:
            found.append(best)
    # Удаляем вложенные/дублирующиеся совпадения, предпочитая более длинные.
    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    result: list[tuple[int, int, str]] = []
    for item in found:
        if any(item[0] >= x[0] and item[1] <= x[1] for x in result):
            continue
        result.append(item)
    return sorted(result)


def parse_dartconnect_rendered_text(
    text: str,
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
    rounds_count: int,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    current_round: int | None = None

    for raw_line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue

        heading_round = dartconnect_round_from_heading(line, rounds_count)
        if heading_round is not None and not SCORE_RE.search(line):
            current_round = heading_round
            continue

        score_match = SCORE_RE.search(line)
        if not score_match:
            continue

        score1, score2 = int(score_match.group(1)), int(score_match.group(2))
        occurrences = dartconnect_player_occurrences(line, players)
        if len(occurrences) < 2:
            continue

        score_pos = score_match.start()
        left = [item for item in occurrences if item[1] <= score_pos]
        right = [item for item in occurrences if item[0] >= score_match.end()]
        if not left or not right:
            continue

        player1 = max(left, key=lambda item: item[1])[2]
        player2 = min(right, key=lambda item: item[0])[2]
        if player1 == player2:
            continue

        round_no = current_round
        if round_no is None:
            # Для ранних строк без заголовка допускаем только первый раунд.
            round_no = 1

        required = winning_legs_by_round.get(round_no)
        if not is_completed_numeric_score(score1, score2, required):
            continue

        if score1 > score2:
            winner, loser = player1, player2
            display_score = f"{score1}-{score2}"
        else:
            winner, loser = player2, player1
            display_score = f"{score2}-{score1}"

        key = (round_no, normalize_name(winner), normalize_name(loser))
        if key in seen:
            continue
        seen.add(key)
        matches.append({
            "winner": winner,
            "loser": loser,
            "score": display_score,
            "round": round_no,
        })

    return matches


def canonicalize_dartconnect_display_name(value: str, players: dict[str, str]) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^\(\d+\)\s*", "", text)
    text = re.sub(r"\s*\(\d+\)$", "", text)
    if not text or re.fullmatch(r"B\d+", text, flags=re.I):
        return None
    if "," in text:
        surname, given = text.split(",", 1)
        text = f"{given.strip()} {surname.strip()}"
    return resolve_player(text, players) or clean_name(text)


def clean_dartconnect_player_segment(value: str) -> str:
    """Удаляет номер доски, среднее и номер посева из половины строки матча."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^B\d+\b\s*", "", text, flags=re.I)
    text = re.sub(r"\(\s*\d+\s*\)", " ", text)
    # Среднее DartConnect всегда выводится как десятичное число (например 94.55).
    text = re.sub(r"(?<![\w.])\d{1,3}[.,]\d{1,2}(?![\w.])", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n–—-:")
    return text


def extract_dartconnect_players_around_score(
    line: str,
    score_match: re.Match[str],
    players: dict[str, str],
) -> list[str]:
    """Берёт игроков слева и справа от счёта независимо от посева и средних."""
    left_raw = clean_dartconnect_player_segment(line[: score_match.start()])
    right_raw = clean_dartconnect_player_segment(line[score_match.end() :])

    left = canonicalize_dartconnect_display_name(left_raw, players)
    right = canonicalize_dartconnect_display_name(right_raw, players)
    if not left or not right or normalize_name(left) == normalize_name(right):
        return []
    return [left, right]


def extract_known_players_from_line(line: str, players: dict[str, str]) -> list[str]:
    """Извлекает двух игроков из полного текста строки DartConnect.

    Используется как резервный путь, если CSS-классы DartConnect изменились
    или один из span с именем отсутствует. Поддерживает оба порядка:
    ``Имя Фамилия`` и ``Фамилия, Имя``.
    """
    normalized_line = normalize_name(line)
    found: list[tuple[int, int, str]] = []

    for canonical in players.values():
        variants = [canonical]
        parts = canonical.split()
        if len(parts) >= 2:
            variants.append(f"{' '.join(parts[1:])}, {parts[0]}")

        best_pos = None
        best_len = 0
        for variant in variants:
            key = normalize_name(variant)
            pos = normalized_line.find(key)
            if pos >= 0 and (best_pos is None or pos < best_pos):
                best_pos = pos
                best_len = len(key)
        if best_pos is not None:
            found.append((best_pos, best_len, canonical))

    found.sort(key=lambda item: item[0])
    result: list[str] = []
    seen: set[str] = set()
    for _, _, canonical in found:
        key = normalize_name(canonical)
        if key not in seen:
            result.append(canonical)
            seen.add(key)
    return result[:2]


def parse_dartconnect_dom_rows(
    rows: list[dict[str, Any]],
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
    rounds_count: int,
) -> list[dict[str, Any]]:
    """Разбирает отрисованные строки DartConnect.

    Ключевой принцип: имена, которые DartConnect уже отдал отдельными DOM-
    элементами, принимаются независимо от того, присутствует ли игрок хотя бы
    в одном фэнтези-составе. Это важно: соперник выбранного дартсмена может
    отсутствовать во всех составах, но его матч всё равно должен учитываться.
    """
    matches: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    skipped: list[str] = []

    for row_index, row in enumerate(rows, start=1):
        line = re.sub(r"\s+", " ", str(row.get("text", ""))).strip()

        # 1. Счёт. Сначала используем два отдельных DOM-значения, затем текст.
        score1: int | None = None
        score2: int | None = None
        raw_score = row.get("score")
        if isinstance(raw_score, list) and len(raw_score) >= 2:
            try:
                score1, score2 = int(raw_score[0]), int(raw_score[1])
            except (TypeError, ValueError):
                score1 = score2 = None

        score_match = SCORE_RE.search(line)
        if score1 is None or score2 is None:
            if not score_match:
                skipped.append(f"#{row_index}: нет счёта | {line}")
                continue
            score1, score2 = int(score_match.group(1)), int(score_match.group(2))

        # 2. Имена. Не отбрасываем неизвестного фэнтези-системе соперника.
        names: list[str] = []
        raw_names = row.get("players", [])
        if isinstance(raw_names, list):
            for value in raw_names:
                display = re.sub(r"\s+", " ", str(value or "")).strip()
                if not display:
                    continue
                name = canonicalize_dartconnect_display_name(display, players)
                if not name:
                    continue
                if normalize_name(name) not in {normalize_name(x) for x in names}:
                    names.append(name)
                if len(names) == 2:
                    break

        # Резерв: делим полную строку строго вокруг счёта.
        if len(names) != 2:
            if score_match is None:
                score_match = SCORE_RE.search(line)
            if score_match:
                names = extract_dartconnect_players_around_score(
                    line, score_match, players
                )

        # Последний резерв — поиск известных игроков в полном тексте.
        if len(names) != 2:
            known = extract_known_players_from_line(line, players)
            if len(known) == 2:
                names = known

        if len(names) != 2:
            skipped.append(
                f"#{row_index}: не извлечены два игрока; DOM={raw_names!r} | {line}"
            )
            continue

        player1, player2 = names[0], names[1]
        if normalize_name(player1) == normalize_name(player2):
            skipped.append(f"#{row_index}: одинаковые игроки | {line}")
            continue

        # 3. Раунд берём из заголовка секции, сохранённого браузером.
        heading = str(row.get("heading", ""))
        round_no = dartconnect_round_from_heading(heading, rounds_count)

        # Поздние стадии имеют уникальные лимиты в настройках PC25:
        # полуфинал до 7, финал до 8. Используем счёт как резерв, если
        # заголовок позднего блока DartConnect отличается от обычного.
        winner_score = max(score1, score2)
        final_target = winning_legs_by_round.get(rounds_count)
        semifinal_target = winning_legs_by_round.get(rounds_count - 1)
        if final_target is not None and winner_score >= final_target:
            round_no = rounds_count
        elif semifinal_target is not None and winner_score >= semifinal_target:
            round_no = rounds_count - 1
        elif round_no is None:
            round_no = 1

        required = winning_legs_by_round.get(round_no)
        if not is_completed_numeric_score(score1, score2, required):
            # Live-матчи в диагностике не считаем ошибкой.
            continue

        if score1 > score2:
            winner, loser = player1, player2
            ws, ls = score1, score2
        else:
            winner, loser = player2, player1
            ws, ls = score2, score1

        key = (round_no, normalize_name(winner), normalize_name(loser))
        if key in seen:
            continue
        seen.add(key)
        matches.append({
            "winner": winner,
            "loser": loser,
            "score": f"{ws}-{ls}",
            "round": round_no,
        })

    if skipped:
        print("DartConnect: пропущенные строки:", file=sys.stderr)
        for item in skipped:
            print("  " + item, file=sys.stderr)

    return matches

def fetch_dartconnect_matches(
    url: str,
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
    rounds_count: int,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Для DartConnect нужен пакет playwright.") from exc

    slug = dartconnect_event_slug(url)
    event_url = f"https://tv.dartconnect.com/event/{slug}/matches"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1800},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto(event_url, wait_until="domcontentloaded", timeout=90000)

        # Ждём именно строки DartConnect, а не любой случайный счёт в рекламе.
        try:
            page.wait_for_selector('[recap-url]', timeout=60000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        dom_rows = page.evaluate(
            r"""() => {
                const compact = value => (value || '').replace(/\s+/g, ' ').trim();

                // Извлекаем только короткую подпись конкретного раунда.
                // Раньше в список попадали большие родительские блоки, внутри
                // которых одновременно встречались Quarter Finals, Semi Finals
                // и Final. Из-за этого четвертьфиналы могли ошибочно считаться
                // финалом и отбрасываться по лимиту в 8 легов.
                const headingRe = /\b(?:Top|Last|Round\s+of)\s*(?:128|64|32|16|8|4|2)\b|\bQuarter[\s-]*finals?\b|\bSemi[\s-]*finals?\b|\b(?:Grand\s+)?Finals?\b/i;

                const headings = [];
                for (const el of document.querySelectorAll('body *')) {
                    const fullText = compact(el.innerText);
                    if (!fullText || fullText.length > 100) continue;

                    const match = fullText.match(headingRe);
                    if (!match) continue;

                    // Не берём контейнер, если внутри него есть более маленький
                    // элемент с той же подписью раунда.
                    const hasSmallerHeading = Array.from(el.children || []).some(child => {
                        const childText = compact(child.innerText);
                        return childText.length <= 100 && headingRe.test(childText);
                    });
                    if (hasSmallerHeading) continue;

                    headings.push({
                        element: el,
                        label: match[0],
                    });
                }

                const rows = [];
                for (const el of document.querySelectorAll('[recap-url]')) {
                    const rowText = compact(el.innerText);
                    const playerNames = Array.from(
                        el.querySelectorAll('span.truncate.leading-tight')
                    )
                        .map(sp => compact(sp.innerText))
                        .filter(Boolean);

                    // Центральный desktop-блок счёта: два отдельных span.
                    let score = Array.from(
                        el.querySelectorAll('div.mx-2.hidden.w-14 span')
                    )
                        .map(sp => compact(sp.innerText))
                        .filter(t => /^\d{1,2}$/.test(t))
                        .slice(0, 2)
                        .map(Number);

                    if (score.length !== 2) {
                        const m = rowText.match(
                            /(?:^|\s)(\d{1,2})\s*[–—−-]\s*(\d{1,2})(?:\s|$)/
                        );
                        score = m ? [Number(m[1]), Number(m[2])] : [];
                    }

                    // Последний короткий заголовок, расположенный перед строкой.
                    let heading = '';
                    for (const item of headings) {
                        const relation = item.element.compareDocumentPosition(el);
                        if (relation & Node.DOCUMENT_POSITION_FOLLOWING) {
                            heading = item.label;
                        }
                    }

                    rows.push({
                        text: rowText,
                        heading,
                        players: playerNames,
                        score,
                    });
                }
                return rows;
            }"""
        )
        browser.close()

    # Подробная диагностика остаётся в Actions-логе.
    print(f"DartConnect: DOM-строк найдено {len(dom_rows)}.")
    heading_counts: dict[str, int] = {}
    for dom_row in dom_rows:
        heading_label = str(dom_row.get("heading", "") or "(без заголовка)")
        heading_counts[heading_label] = heading_counts.get(heading_label, 0) + 1
    print(f"DartConnect: DOM-строки по заголовкам {heading_counts}.")
    matches = parse_dartconnect_dom_rows(
        dom_rows, players, winning_legs_by_round, rounds_count
    )
    print(f"DartConnect: завершённых матчей принято {len(matches)}.")
    parsed_round_counts: dict[int, int] = {}
    for parsed_match in matches:
        parsed_round = int(parsed_match.get("round", 0))
        parsed_round_counts[parsed_round] = parsed_round_counts.get(parsed_round, 0) + 1
    print(f"DartConnect: по раундам {parsed_round_counts}.")

    if not matches and dom_rows:
        raise RuntimeError(
            "DartConnect открылся, но завершённые матчи не удалось сопоставить "
            "с игроками проекта. Смотрите строки диагностики выше."
        )
    return matches


def fetch_matches(
    config: dict[str, Any],
    players: dict[str, str],
    winning_legs_by_round: dict[int, int],
    rounds_count: int,
    first_round_matches: int | None = None,
) -> list[dict[str, Any]]:
    source = str(config.get("result_source", "wikipedia")).strip().casefold()
    url = str(config.get("tournament_url", "")).strip()
    if source == "dartconnect":
        return fetch_dartconnect_matches(url, players, winning_legs_by_round, rounds_count)
    if source == "dartn":
        return fetch_dartn_matches(url, players, winning_legs_by_round)
    if source == "wikipedia":
        return fetch_wikipedia_matches(url, players, winning_legs_by_round, rounds_count, first_round_matches)
    raise ValueError(f"Неизвестный источник результатов: {source}")

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

    # Очки считаем по самой дальней достигнутой стадии, а не по числу
    # найденных побед. Это устойчиво к единично пропущенной строке на
    # DartConnect: если игрок проиграл в 3-м раунде, сам этот матч уже
    # доказывает, что он дошёл до 3-го раунда и должен получить очки за
    # эту стадию. Победитель раунда N достигает стадии N+1.
    reached_stage: dict[str, int] = {}
    eliminated: set[str] = set()
    tournament_players: set[str] = set(project.get("tournament_players", []))

    # European Tour: seeded players enter directly in round 2.
    # Their starting stage must count even before their first match is played.
    try:
        seeded_start_round = max(
            1, int(settings.get("seeded_start_round", 1))
        )
    except (TypeError, ValueError):
        seeded_start_round = 1

    for seeded_player in project.get("seeded_players", []):
        seeded_player = clean_name(seeded_player)
        if seeded_player:
            reached_stage[seeded_player] = max(
                reached_stage.get(seeded_player, 1),
                seeded_start_round,
            )
            tournament_players.add(seeded_player)

    for match in matches:
        winner = match["winner"]
        loser = match["loser"]
        try:
            round_no = max(1, int(match.get("round", 1)))
        except (TypeError, ValueError):
            round_no = 1

        reached_stage[winner] = max(reached_stage.get(winner, 1), round_no + 1)
        reached_stage[loser] = max(reached_stage.get(loser, 1), round_no)
        eliminated.add(loser)
        tournament_players.update((winner, loser))

    converted_scores = normalize_stage_scores(stage_scores)

    # Если уже зафиксированы более поздние стадии, игрок, который дошёл
    # только до более ранней стадии, не может оставаться «В ИГРЕ», даже если
    # его конкретный матч отсутствует в выдаче DartConnect.
    #
    # Пример: финал уже сыгран (latest_round == 7), а игрок достиг только
    # стадии 5 — значит он выбыл в четвертьфинале.
    latest_round = max(
        (int(match.get("round", 1)) for match in matches),
        default=0,
    )
    if latest_round > 0:
        for player, stage in reached_stage.items():
            if stage <= latest_round:
                eliminated.add(player)

    def score_for_stage(stage: int) -> int:
        if not converted_scores:
            return 0
        eligible = [key for key in converted_scores if key <= stage]
        return converted_scores[max(eligible)] if eligible else 0

    scores: dict[str, int] = {}
    all_players = {
        player
        for participant in project.get("participants", [])
        for player in participant.get("players", [])
    }
    for player in all_players:
        scores[player] = score_for_stage(reached_stage.get(player, 1))

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

            if manual_status.get(norm, False):
                is_alive = False
                status = "ВЫБЫЛ"
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

    raw_legs = project.get("settings", {}).get("winning_legs_by_round", {})
    winning_legs_by_round: dict[int, int] = {}
    if isinstance(raw_legs, dict):
        for key, value in raw_legs.items():
            try:
                winning_legs_by_round[int(key)] = int(value)
            except (TypeError, ValueError):
                continue

    stage_scores = project.get("settings", {}).get("stage_scores", [])

    raw_first_round_matches = project.get("settings", {}).get("first_round_matches")
    try:
        first_round_matches = (
            int(raw_first_round_matches)
            if raw_first_round_matches not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        first_round_matches = None

    if isinstance(stage_scores, list):
        rounds_count = max(1, len(stage_scores) - 1)
    elif isinstance(stage_scores, dict):
        rounds_count = max(
            [int(key) for key in stage_scores if str(key).isdigit()],
            default=len(winning_legs_by_round),
        )
    else:
        rounds_count = len(winning_legs_by_round)

    matches = (
        fetch_matches(
            config,
            players,
            winning_legs_by_round,
            rounds_count,
            first_round_matches,
        )
        if players
        else []
    )
    output = calculate(project, matches)
    output["site_title"] = config.get("site_title", output["tournament"])
    output["refresh_seconds"] = int(config.get("refresh_seconds", 60))
    output["source_url"] = config.get("tournament_url", "")
    output["source_label"] = config.get("source_label", "Источник")

    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    round_counts: dict[int, int] = {}
    for match in matches:
        round_no = int(match.get("round", 0))
        round_counts[round_no] = round_counts.get(round_no, 0) + 1

    print(
        f"Обновлено: участников {len(output['standings'])}, "
        f"матчей {len(matches)}; по раундам {round_counts}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
