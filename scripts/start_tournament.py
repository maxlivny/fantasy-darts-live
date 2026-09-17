from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "config.json"
PROJECT_PATH = ROOT / "data" / "project.json"
SITE_DATA_PATH = ROOT / "site" / "data.json"
ARCHIVE_ROOT = ROOT / "site" / "archive"
ARCHIVE_INDEX = ARCHIVE_ROOT / "index.json"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def detect_encoding(path: Path) -> str:
    for enc in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            path.read_text(encoding=enc)
            return enc
        except UnicodeDecodeError:
            pass
    return "cp1251"


def to_float(value: str) -> float:
    return float(value.strip().replace(",", "."))


def parse_fantasy_csv(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    enc = detect_encoding(path)
    with path.open("r", encoding=enc, newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))

    participants: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    i = 0
    while i < len(rows):
        row = rows[i]
        first = row[0].strip() if row else ""

        # Metadata exported by darts.lapki.su.
        if first == "Ссылка на турнир" and len(row) > 1:
            meta["fantasy_url"] = row[1].strip()
            i += 1
            continue
        if first == "Окончание турнира":
            if len(row) > 1:
                meta["deadline"] = row[1].strip()
            if len(row) > 2:
                meta["tournament_name"] = row[2].strip()
            if len(row) > 3:
                try:
                    meta["budget_limit"] = to_float(row[3])
                except ValueError:
                    pass
            i += 1
            continue
        if first == "Время выгрузки" and len(row) > 1:
            meta["exported_at"] = row[1].strip()
            i += 1
            continue

        if not row or len(row) < 3 or first.casefold() in {"фио", "имя", "участник"}:
            i += 1
            continue
        if i + 1 >= len(rows):
            break

        price_row = rows[i + 1]
        if len(price_row) < 3:
            i += 1
            continue

        player_cells = [x.strip() for x in row[2:] if x.strip()]
        prices: list[float] = []
        for cell in price_row[2:]:
            cell = cell.strip()
            if not cell or cell.startswith("="):
                continue
            try:
                prices.append(to_float(cell))
            except ValueError:
                pass

        count = min(len(player_cells), len(prices))
        if count:
            participants.append({"name": first, "players": player_cells[:count], "prices": prices[:count]})
            i += 2
        else:
            i += 1

    if not participants:
        raise ValueError("CSV: не удалось найти пары строк «состав + цены».")

    roster_sizes = {len(p["players"]) for p in participants}
    if len(roster_sizes) != 1:
        raise ValueError(f"CSV: у участников разный размер состава: {sorted(roster_sizes)}")
    meta["roster_size"] = next(iter(roster_sizes))
    return participants, meta


def slugify(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"^fantasy\s+", "", value, flags=re.I)
    value = re.sub(r"[^a-z0-9а-я]+", "-", value, flags=re.I).strip("-")
    return value or datetime.now(timezone.utc).strftime("tournament-%Y%m%d-%H%M%S")


def archive_current() -> str | None:
    site_data = read_json(SITE_DATA_PATH, {})
    if not site_data or not site_data.get("standings"):
        return None
    project = read_json(PROJECT_PATH, {})
    config = read_json(CONFIG_PATH, {})
    tournament = site_data.get("tournament") or project.get("settings", {}).get("tournament_name") or config.get("site_title")
    if not tournament:
        return None

    archive_id = slugify(str(tournament))
    archive_dir = ARCHIVE_ROOT / archive_id
    write_json(archive_dir / "project.json", project)
    write_json(archive_dir / "data.json", site_data)
    write_json(archive_dir / "config.json", config)

    standings = site_data.get("standings", [])
    index = read_json(ARCHIVE_INDEX, [])
    if not isinstance(index, list):
        index = []
    item = {
        "id": archive_id,
        "title": f"Fantasy {tournament}" if not str(tournament).casefold().startswith("fantasy ") else str(tournament),
        "winner": standings[0].get("name") if standings else None,
        "participants": len(standings),
        "matches": int(site_data.get("matches_count", 0)),
        "archived_at": site_data.get("updated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": f"archive/{archive_id}/data.json",
    }
    index = [x for x in index if x.get("id") != archive_id]
    index.append(item)
    write_json(ARCHIVE_INDEX, index)
    return archive_id


def parse_ints(text: str, expected: int, label: str) -> list[int]:
    values = [int(x.strip()) for x in text.split(",") if x.strip()]
    if len(values) != expected:
        raise ValueError(f"{label}: ожидалось {expected} значений, получено {len(values)}")
    return values


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--url", required=True)
    ap.add_argument("--source", choices=["wikipedia", "dartconnect"], default="wikipedia")
    ap.add_argument("--budget", type=float)
    ap.add_argument("--start-stage", default="1/32")
    ap.add_argument("--stage-scores", default="0,1,3,5,8,12,16")
    ap.add_argument("--winning-legs", default="6,6,6,6,7,8")
    ap.add_argument("--seeded", default="")
    ap.add_argument("--seeded-start-round", type=int, default=2)
    ap.add_argument("--first-round-matches", type=int, default=16)
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()

    participants, meta = parse_fantasy_csv(Path(args.csv))
    name = args.name.strip() or str(meta.get("tournament_name", "")).strip()
    if not name:
        raise ValueError("Не удалось определить название турнира. Укажи --name.")
    budget = args.budget if args.budget is not None else meta.get("budget_limit")
    if budget is None:
        raise ValueError("Не удалось определить бюджет. Укажи --budget.")

    scores_raw = [x.strip() for x in args.stage_scores.split(",") if x.strip()]
    rounds = len(scores_raw) - 1
    scores = parse_ints(args.stage_scores, rounds + 1, "stage_scores")
    legs = parse_ints(args.winning_legs, rounds, "winning_legs")

    players = sorted({player for p in participants for player in p["players"]})
    seeded = [x.strip() for x in re.split(r"[\n|]+", args.seeded) if x.strip()]
    unknown_seeds = [x for x in seeded if x not in players]
    if unknown_seeds:
        print("WARNING: сеяные отсутствуют в фэнтези-составах (это допустимо): " + ", ".join(unknown_seeds))
    tournament_players = sorted(set(players) | set(seeded))

    archived = None if args.no_archive else archive_current()
    project = {
        "participants": participants,
        "player_scores": {p: (scores[args.seeded_start_round - 1] if p in seeded else 0) for p in tournament_players},
        "manual_score_overrides": {},
        "seeded_players": seeded,
        "completed_matches": [],
        "tournament_players": tournament_players,
        "eliminated_players": [],
        "manual_status_overrides": {},
        "settings": {
            "tournament_name": name,
            "start_stage": args.start_stage,
            "budget_limit": float(budget),
            "roster_size": int(meta["roster_size"]),
            "stage_scores": scores,
            "seeded_start_round": args.seeded_start_round,
            "winning_legs_by_round": {str(i): v for i, v in enumerate(legs, 1)},
            "rating_participants": [],
            "first_round_matches": args.first_round_matches,
        },
    }
    write_json(PROJECT_PATH, project)

    config = read_json(CONFIG_PATH, {})
    config.update({
        "site_title": f"Fantasy {name}" if not name.casefold().startswith("fantasy ") else name,
        "result_source": args.source,
        "tournament_url": args.url,
        "source_label": "DartConnect" if args.source == "dartconnect" else "Wikipedia",
        "refresh_seconds": int(config.get("refresh_seconds", 60)),
    })
    write_json(CONFIG_PATH, config)
    write_json(SITE_DATA_PATH, {
        "tournament": name,
        "site_title": config["site_title"],
        "updated_at": None,
        "standings": [],
        "matches": [],
        "matches_count": 0,
        "refresh_seconds": config["refresh_seconds"],
        "source_url": args.url,
        "source_label": config["source_label"],
    })

    print(f"OK: {name}")
    print(f"Participants: {len(participants)}; roster: {meta['roster_size']}; players: {len(tournament_players)}")
    print(f"Budget: {budget}; seeds: {len(seeded)}; archived: {archived or '-'}")

if __name__ == "__main__":
    main()
