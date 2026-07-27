from __future__ import annotations

import json
import re
import shutil
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
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
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def slugify(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"[^a-z0-9а-я]+", "-", value, flags=re.I)
    value = value.strip("-")
    return value or datetime.now().strftime("tournament-%Y%m%d-%H%M%S")


def parse_int_list(value: str, expected: int, label: str) -> list[int]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) != expected:
        raise ValueError(f"{label}: нужно указать {expected} значений через запятую.")
    try:
        return [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"{label}: допустимы только целые числа.") from exc


def reset_project(project: dict[str, Any]) -> dict[str, Any]:
    players = {
        player
        for participant in project.get("participants", [])
        for player in participant.get("players", [])
    }
    project["player_scores"] = {player: 0 for player in sorted(players)}
    project["manual_score_overrides"] = {}
    project["manual_status_overrides"] = {}
    project["completed_matches"] = []
    project["eliminated_players"] = []
    project["tournament_players"] = sorted(players)
    project["seeded_players"] = []
    return project


def archive_current() -> str | None:
    project = read_json(PROJECT_PATH, {})
    site_data = read_json(SITE_DATA_PATH, {})
    config = read_json(CONFIG_PATH, {})

    tournament = (
        site_data.get("site_title")
        or site_data.get("tournament")
        or config.get("site_title")
        or project.get("settings", {}).get("tournament_name")
    )
    if not tournament:
        return None

    # Один турнир хранится под постоянным адресом. Повторное архивирование
    # обновляет итоговый снимок, а не создаёт дубликат с датой в имени.
    archive_id = slugify(str(tournament))
    archive_dir = ARCHIVE_ROOT / archive_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    write_json(archive_dir / "project.json", project)
    write_json(archive_dir / "data.json", site_data)
    write_json(archive_dir / "config.json", config)

    index = read_json(ARCHIVE_INDEX, [])
    if not isinstance(index, list):
        index = []
    winner = None
    standings = site_data.get("standings", [])
    if standings:
        winner = standings[0].get("name")
    index = [item for item in index if item.get("id") != archive_id]
    index.insert(
        0,
        {
            "id": archive_id,
            "title": tournament,
            "winner": winner,
            "participants": len(standings),
            "matches": site_data.get("matches_count", 0),
            "archived_at": datetime.now().isoformat(timespec="seconds"),
            "data": f"archive/{archive_id}/data.json",
        },
    )
    write_json(ARCHIVE_INDEX, index)
    return archive_id


class TournamentWizard(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Fantasy Darts — мастер нового турнира")
        self.geometry("760x690")
        self.minsize(720, 640)

        config = read_json(CONFIG_PATH, {})
        project = read_json(PROJECT_PATH, {})
        settings = project.get("settings", {})

        self.project_file = tk.StringVar()
        self.name = tk.StringVar(value=settings.get("tournament_name", ""))
        self.site_title = tk.StringVar(value=config.get("site_title", ""))
        self.source = tk.StringVar(value=config.get("result_source", "wikipedia"))
        self.url = tk.StringVar(value=config.get("tournament_url", ""))
        self.start_stage = tk.StringVar(value=settings.get("start_stage", "1/32"))
        self.budget = tk.StringVar(value=str(settings.get("budget_limit", 110)))
        self.roster_size = tk.StringVar(value=str(settings.get("roster_size", 8)))
        self.rounds = tk.StringVar(value="5")
        self.stage_scores = tk.StringVar(value="0, 3, 6, 10, 14, 18")
        self.winning_legs = tk.StringVar(value="10, 11, 16, 17, 18")
        self.archive_enabled = tk.BooleanVar(value=True)

        existing_scores = settings.get("stage_scores")
        if isinstance(existing_scores, list) and existing_scores:
            self.stage_scores.set(", ".join(str(x) for x in existing_scores))
            self.rounds.set(str(max(1, len(existing_scores) - 1)))
        existing_legs = settings.get("winning_legs_by_round")
        if isinstance(existing_legs, dict) and existing_legs:
            ordered = [existing_legs.get(str(i), existing_legs.get(i, "")) for i in range(1, len(existing_legs) + 1)]
            self.winning_legs.set(", ".join(str(x) for x in ordered))

        self._build()

    def _row(self, parent: ttk.Frame, row: int, label: str, variable: tk.Variable, width: int = 54) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=7)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="ew", pady=7)

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=22)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text="Создание нового фэнтези-турнира", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text="Мастер архивирует текущий турнир, заменит проект участников и сбросит старые результаты.",
            wraplength=690,
        ).grid(row=1, column=0, sticky="w", pady=(4, 18))

        project_box = ttk.LabelFrame(outer, text="1. Новый проект участников", padding=14)
        project_box.grid(row=2, column=0, sticky="ew")
        project_box.columnconfigure(0, weight=1)
        ttk.Entry(project_box, textvariable=self.project_file).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(project_box, text="Выбрать project.json", command=self.choose_project).grid(row=0, column=1)
        ttk.Label(project_box, text="Выбери project.json, экспортированный из Fantasy Darts Counter.").grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))

        details = ttk.LabelFrame(outer, text="2. Настройки турнира", padding=14)
        details.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        details.columnconfigure(1, weight=1)
        self._row(details, 0, "Название турнира", self.name)
        self._row(details, 1, "Заголовок сайта", self.site_title)
        ttk.Label(details, text="Источник результатов").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=7)
        source_box = ttk.Combobox(
            details, textvariable=self.source, state="readonly",
            values=("wikipedia", "dartconnect"), width=51,
        )
        source_box.grid(row=2, column=1, sticky="ew", pady=7)
        source_box.bind("<<ComboboxSelected>>", lambda _event: self._update_source_hint())
        self.source_url_label = ttk.Label(details, text="Ссылка на источник")
        self.source_url_label.grid(row=3, column=0, sticky="w", padx=(0, 12), pady=7)
        ttk.Entry(details, textvariable=self.url, width=54).grid(row=3, column=1, sticky="ew", pady=7)
        self._row(details, 4, "Стартовая стадия", self.start_stage)
        self._row(details, 5, "Бюджет", self.budget)
        self._row(details, 6, "Игроков в составе", self.roster_size)
        self._row(details, 7, "Количество раундов", self.rounds)
        self._row(details, 8, "Очки по стадиям", self.stage_scores)
        self._row(details, 9, "Победные леги по раундам", self.winning_legs)

        ttk.Label(
            details,
            text=(
                "Пример для 5 раундов: очки — 0, 3, 6, 10, 14, 18; "
                "леги — 10, 11, 16, 17, 18. Первое значение очков — до первой победы."
            ),
            wraplength=650,
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(7, 0))
        self.source_hint = ttk.Label(details, wraplength=650)
        self.source_hint.grid(row=11, column=0, columnspan=2, sticky="w", pady=(7, 0))
        self._update_source_hint()

        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        ttk.Checkbutton(
            actions,
            text="Сохранить текущий турнир в site/archive перед заменой",
            variable=self.archive_enabled,
        ).pack(anchor="w")
        ttk.Button(actions, text="Создать новый турнир", command=self.create_tournament).pack(anchor="e", pady=(14, 0))


    def _update_source_hint(self) -> None:
        if self.source.get() == "dartconnect":
            self.source_url_label.configure(text="Ссылка DartConnect")
            self.source_hint.configure(
                text="Пример: https://tv.dartconnect.com/event/pdcpc26e25/matches. "
                     "GitHub Actions будет запускать Chromium и читать официальный список завершённых матчей."
            )
        else:
            self.source_url_label.configure(text="Ссылка Wikipedia")
            self.source_hint.configure(
                text="Укажи полную ссылку на страницу турнира Wikipedia."
            )

    def choose_project(self) -> None:
        filename = filedialog.askopenfilename(
            title="Выберите project.json",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")],
        )
        if filename:
            self.project_file.set(filename)

    def create_tournament(self) -> None:
        try:
            source = Path(self.project_file.get().strip())
            if not source.is_file():
                raise ValueError("Сначала выбери новый project.json.")

            name = self.name.get().strip()
            title = self.site_title.get().strip() or name
            url = self.url.get().strip()
            if not name:
                raise ValueError("Укажи название турнира.")
            if not re.match(r"https?://", url):
                raise ValueError("Укажи полную ссылку на источник, начиная с https://.")
            source_type = self.source.get().strip().casefold()
            host = url.casefold()
            if source_type == "wikipedia" and "wikipedia.org" not in host:
                raise ValueError("Для источника Wikipedia нужна ссылка wikipedia.org.")
            if source_type == "dartconnect" and "dartconnect.com/event/" not in host:
                raise ValueError("Для DartConnect нужна ссылка вида https://tv.dartconnect.com/event/.../matches")

            rounds = int(self.rounds.get())
            if rounds < 1 or rounds > 8:
                raise ValueError("Количество раундов должно быть от 1 до 8.")

            scores = parse_int_list(self.stage_scores.get(), rounds + 1, "Очки по стадиям")
            legs = parse_int_list(self.winning_legs.get(), rounds, "Победные леги")
            budget = float(self.budget.get().replace(",", "."))
            roster_size = int(self.roster_size.get())

            new_project = read_json(source, {})
            if not new_project.get("participants"):
                raise ValueError("В выбранном project.json нет участников.")

            archived = archive_current() if self.archive_enabled.get() else None

            backup_dir = ROOT / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_dir.mkdir(parents=True, exist_ok=True)
            for path in (CONFIG_PATH, PROJECT_PATH, SITE_DATA_PATH):
                if path.exists():
                    shutil.copy2(path, backup_dir / path.name)

            new_project = reset_project(new_project)
            settings = new_project.setdefault("settings", {})
            settings.update(
                {
                    "tournament_name": name,
                    "start_stage": self.start_stage.get().strip(),
                    "budget_limit": budget,
                    "roster_size": roster_size,
                    "stage_scores": scores,
                    "winning_legs_by_round": {
                        str(index): value
                        for index, value in enumerate(legs, start=1)
                    },
                }
            )
            write_json(PROJECT_PATH, new_project)

            config = read_json(CONFIG_PATH, {})
            config.update(
                {
                    "tournament_url": url,
                    "result_source": source_type,
                    "site_title": title,
                    "refresh_seconds": int(config.get("refresh_seconds", 60)),
                    "source_label": "DartConnect" if source_type == "dartconnect" else "Wikipedia",
                    "timezone": config.get("timezone", "Europe/Moscow"),
                }
            )
            write_json(CONFIG_PATH, config)

            empty_site = {
                "tournament": name,
                "site_title": title,
                "updated_at": None,
                "standings": [],
                "matches": [],
                "matches_count": 0,
                "refresh_seconds": config["refresh_seconds"],
                "source_url": url,
                "source_label": config["source_label"],
            }
            write_json(SITE_DATA_PATH, empty_site)

            archive_message = f"\nАрхив: {archived}" if archived else ""
            messagebox.showinfo(
                "Готово",
                "Новый турнир создан.\n\n"
                "Теперь загрузи изменённые файлы в GitHub и запусти workflow."
                + archive_message,
            )
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))


if __name__ == "__main__":
    TournamentWizard().mainloop()
