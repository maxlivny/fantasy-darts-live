# Fantasy Darts Live — MVP

Автоматическая онлайн-таблица текущего фэнтези-турнира.

## Как работает

1. В `data/project.json` один раз кладётся проект, сохранённый из Fantasy Darts Counter.
2. В `data/config.json` указывается ссылка на страницу турнира Wikipedia.
3. GitHub Actions каждые 5 минут запускает `scripts/update_results.py`.
4. Скрипт получает результаты, пересчитывает таблицу и записывает `site/data.json`.
5. GitHub Pages публикует сайт. Участникам достаточно открыть одну ссылку.

## Быстрый запуск

1. Создайте новый публичный репозиторий GitHub.
2. Загрузите в него все файлы из этого архива.
3. Замените `data/project.json` своим проектом из Fantasy Darts Counter.
4. Проверьте ссылку в `data/config.json`.
5. Откройте **Settings → Pages**.
6. В разделе **Build and deployment** выберите **GitHub Actions**.
7. Откройте вкладку **Actions** и вручную запустите workflow
   `Update fantasy standings`.
8. После первого запуска сайт появится по адресу:
   `https://ИМЯ-ПОЛЬЗОВАТЕЛЯ.github.io/ИМЯ-РЕПОЗИТОРИЯ/`

## Важно

- Сайт не принимает составы и не требует регистрации.
- Обновление происходит автоматически.
- Ручные исправления из `manual_score_overrides` и
  `manual_status_overrides` имеют приоритет.
- Для нового турнира достаточно заменить `project.json` и URL турнира.

## Локальная проверка

```bash
python -m pip install -r requirements.txt
python scripts/update_results.py
python -m http.server 8000 --directory site
```

Откройте `http://localhost:8000`.

## Ограничение MVP

Wikipedia может менять структуру таблиц. Скрипт использует несколько способов
распознавания результатов, но перед полноценным запуском желательно проверить
его на текущем турнире.
