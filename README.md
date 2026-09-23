# Minecraft AI Builder

Пишешь в чат Minecraft Education/Bedrock `ai замок с башнями`, модель пишет Python-скрипт,
скрипт строит замок. Скрипты можно читать, менять и писать свои.

## Запуск

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # заполнить адрес LiteLLM и модель
set -a; . ./.env; set +a
.venv/bin/python -m mcai.ws_bridge
```

В игре (мир с включёнными читами): `/connect localhost:8765` (или `.../<BRIDGE_TOKEN>`).

## Команды в чате

| Команда | Что делает |
|---|---|
| `ai <что построить>` | модель пишет скрипт и строит |
| `ai+ <что изменить>` | модель меняет последний скрипт |
| `run <имя>` | запускает `scripts/<имя>.py` |
| `undo` | убирает последнюю постройку (заменяет воздухом) |
| `help` | подсказка |

## Свои скрипты

Лежат в `scripts/`. Функции: `block`, `fill`, `walls`, `sphere`, `cylinder`, `pyramid`, `line`, `say`
(описание — в `mcai/build_api.py`). `(0, 0, 0)` — уровень ног, несколько блоков перед игроком,
`+z` — вперёд. Проверить скрипт без игры: `.venv/bin/python -m mcai.sandbox scripts/tower.py`

## Тесты без игры

```
.venv/bin/python -m mcai.ws_bridge &
.venv/bin/python tools/simulator.py "run tower" "undo"
```

`tools/fake_llm.py` — поддельная модель для проверки команды `ai` без LiteLLM.
# promptcraft
