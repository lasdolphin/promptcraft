# Promptcraft

Пишешь в чат Minecraft `ai замок с башнями`, модель пишет Python-скрипт,
скрипт строит замок. Скрипты можно читать, менять и писать свои.

Работает в двух играх, команды одинаковые:
- **Minecraft Java** (наш сервер в кластере) — мост `mcai.java_bridge`, ходит в сервер по RCON;
- **Minecraft Education / Bedrock** (свой мир) — мост `mcai.ws_bridge`, игра подключается командой `/connect`.

## Запуск

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # заполнить адрес LiteLLM и модель
set -a; . ./.env; set +a
```

Education / Bedrock: `.venv/bin/python -m mcai.ws_bridge`, в игре (мир с включёнными читами):
`/connect localhost:8765` (или `.../<BRIDGE_TOKEN>`).

Java: нужен сервер с RCON, например в Docker:

```
docker run -d --name mc -p 25565:25565 -p 25575:25575 -e EULA=TRUE -e TYPE=PAPER \
  -e MODE=creative -e RCON_PASSWORD=secret itzg/minecraft-server:2026.9.1
RCON_PASSWORD=secret MC_LOG_CMD="docker logs -f --since 0s mc" .venv/bin/python -m mcai.java_bridge
```

## Команды в чате

| Команда | Что делает |
|---|---|
| `ai <что построить>` | модель пишет скрипт и строит; постройка получает номер, например `#7` |
| `ai+ #7 <что изменить>` | доработать постройку #7 (или по имени: `ai+ замок ...`); старая версия убирается, новая ставится на то же место. Без номера — последняя своя |
| `run <скрипт>` | запускает `scripts/<скрипт>.py` (тоже становится постройкой с номером) |
| `builds` / `builds all` | таблица построек: последние 10 / все |
| `name #7 <имя>` | переименовать (имя — одно слово) |
| `tp #7` | перенестись к постройке |
| `delete #7` | удалить из мира — бот спросит подтверждение, ответ `да` |
| `undo` | удалить свою последнюю постройку (тоже с подтверждением) |
| `help` | подсказка |

Менять и удалять постройку может её автор или админ. Постройки хранятся рядом со скриптами
(`.builds/java` и `.builds/education`), у каждой — запрос, код, место и поставленные блоки.
В админке сервера Java есть таблица построек с переименованием и удалением.

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

## Деплой в кластер

Образ моста собирает GitHub Actions при push в `main`: `ghcr.io/lasdolphin/promptcraft-bridge`.
Манифесты в `k8s/`, их синхронизирует ArgoCD (namespace `promptcraft`):

| Что | Зачем |
|---|---|
| `minecraft/` | сервер Minecraft Java (Paper, творческий режим, whitelist), TCP 25565 через MetalLB. С Geyser и Floodgate: игроки с Bedrock заходят на тот же адрес, порт 19132 (UDP+TCP) и 19133 (UDP) |
| `playit/` | агент playit.gg — друзья заходят снаружи |
| `bridge/` | под моста: `bridge` — для `/connect` из Education (наружу через Cloudflare Tunnel), `java-bridge` — для сервера Java (RCON + чат из лога пода `minecraft`) |

Секреты берутся из 1Password (vault `antfarm.dev`): `playit-agent` (поле `password`),
`minecraft-litellm` (поле `LITELLM_API_KEY`), `promptcraft-bridge-token` (поле `password`;
он же пароль RCON сервера — RCON доступен только внутри кластера).

Один раз:
1. После первой сборки сделать пакет `promptcraft-bridge` публичным (GitHub → Packages → Settings).
2. `kubectl apply -f k8s/argocd-app.yaml`
3. В cosmo-fleet добавить правило cloudflared для `promptcraft.antfarm.dev`.
4. В панели playit.gg два туннеля на адрес сервиса `minecraft` (`kubectl -n promptcraft get svc minecraft`):
   Minecraft Java → порт 25565, Minecraft Bedrock (UDP) → порт 19132.

### Кто может играть: вход с одобрением

Админка: https://promptcraft-admin.antfarm.dev (только из домашней сети), пароль — `promptcraft-bridge-token`.
Там видно, кто ждёт одобрения и кто одобрен, есть кнопки «Одобрить», «Удалить» и переключатель
«Приём новых игроков».

Как добавить друга:
1. В админке открыть приём (или написать в чате `open`).
2. Друг заходит на сервер. Он гость: режим adventure, строить и запускать команды нельзя.
   Админам в игре приходит сообщение.
3. Одобрить: кнопка в админке или `allow <ник>` в чате. Друг сразу получает creative.
4. Закрыть приём (`close`) — дальше заходят только одобренные.

Команды админов в чате: `allow <ник>`, `deny <ник>` (убрать и выкинуть), `players`, `open`, `close`.
Админы — переменная `ADMINS` в `k8s/bridge/deployment.yaml`. Игроки с Bedrock в игре — с точкой: `.Гейммтег`.

Одобренные — это обычный whitelist сервера. Bedrock-игрока сервер может добавить в него, только
пока тот онлайн (иначе неоткуда взять его UUID), поэтому и нужен вход гостем.
Если мост не работает, все остаются в adventure — сломать ничего нельзя.

Minecraft Education на сервер зайти не может — для него мост `/connect`.
Консоль сервера: `kubectl -n promptcraft exec deploy/minecraft -- rcon-cli <команда>`.
