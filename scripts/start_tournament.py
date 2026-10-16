from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
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
    if not roster_sizes.issubset({10, 11}):
        raise ValueError(f"CSV: допустимы составы из 10 или 11 игроков: {sorted(roster_sizes)}")
    meta["roster_size"] = max(roster_sizes)
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
