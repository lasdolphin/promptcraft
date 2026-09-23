# Promptcraft

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

## Деплой в кластер

Образ моста собирает GitHub Actions при push в `main`: `ghcr.io/lasdolphin/promptcraft-bridge`.
Манифесты в `k8s/`, их синхронизирует ArgoCD (namespace `promptcraft`):

| Что | Зачем |
|---|---|
| `bedrock/` | сервер Bedrock (творческий режим, allow-list), TCP 19132 через MetalLB |
| `playit/` | агент playit.gg — друзья заходят снаружи |
| `bridge/` | мост для `/connect` из Minecraft Education, наружу через Cloudflare Tunnel |

Секреты берутся из 1Password (vault `antfarm.dev`): `playit-agent` (поле `password`),
`minecraft-litellm` (поле `LITELLM_API_KEY`), `promptcraft-bridge-token` (поле `password`).

Один раз:
1. После первой сборки сделать пакет `promptcraft-bridge` публичным (GitHub → Packages → Settings).
2. `kubectl apply -f k8s/argocd-app.yaml`
3. В cosmo-fleet добавить правило cloudflared для `promptcraft.antfarm.dev`.
4. В панели playit.gg: TCP-туннель → адрес сервиса `bedrock` (`kubectl -n promptcraft get svc bedrock`), порт 19132.
   Bedrock 1.26 слушает TCP, а не UDP, поэтому готовый тип «Minecraft Bedrock» (UDP) не подойдёт.

Добавить друга: гейммтег в `ALLOW_LIST_USERS` в `k8s/bedrock/deployment.yaml`.
Консоль сервера: `kubectl -n promptcraft exec deploy/bedrock -- send-command <команда>`.
