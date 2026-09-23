"""Мост для Minecraft Education / Bedrock: websocket-сервер для команды /connect.

Команды в чате игры:
  ai <что построить>     — модель пишет скрипт и строит
  ai+ <что изменить>     — модель меняет последний скрипт
  run <имя>              — запустить свой скрипт scripts/<имя>.py
  undo                   — убрать последнюю постройку (заменить воздухом)
  help                   — подсказка
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid

import websockets

from mcai import llm, sandbox

log = logging.getLogger("bridge")

EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", EXAMPLES_DIR)
TOKEN = os.environ.get("BRIDGE_TOKEN", "")
MAX_IN_FLIGHT = 50          # игра не любит, когда ей шлют слишком много команд сразу
MAX_FILL_VOLUME = 32768     # ограничение команды /fill
FORWARD_OFFSET = 3          # на сколько блоков перед игроком начинать постройку
NAME_RE = re.compile(r"^[A-Za-z0-9_\-/]+$")

HELP = [
    "ai <что построить> — например: ai замок с четырьмя башнями",
    "ai+ <что изменить> — например: ai+ сделай башни выше",
    "run <имя> — запустить свой скрипт, например: run tower",
    "undo — убрать последнюю постройку",
]


# --- превращение операций в команды игры -------------------------------------

def rotate(x, z, facing):
    """Повернуть локальные координаты так, чтобы +z смотрело туда, куда смотрит игрок."""
    return [(x, z), (-z, x), (-x, -z), (z, -x)][facing]


def split_box(x1, y1, z1, x2, y2, z2):
    """Разбить большой fill на куски не больше MAX_FILL_VOLUME блоков (режем по y).
    Один слой всегда помещается: координаты ограничены ±80, а 161*161 < 32768."""
    layer = (x2 - x1 + 1) * (z2 - z1 + 1)
    step = max(1, MAX_FILL_VOLUME // layer)
    for y in range(y1, y2 + 1, step):
        yield x1, y, z1, x2, min(y + step - 1, y2), z2


def ops_to_commands(ops, origin, facing, material=None):
    ox, oy, oz = origin
    commands = []
    for op in ops:
        mat = material or op[-1]
        if op[0] == "block":
            _, x, y, z, _ = op
            wx, wz = rotate(x, z, facing)
            commands.append(f"setblock {ox + wx} {oy + y} {oz + wz} {mat}")
        else:
            _, x1, y1, z1, x2, y2, z2, _ = op
            ax, az = rotate(x1, z1, facing)
            bx, bz = rotate(x2, z2, facing)
            box = (min(ax, bx), y1, min(az, bz), max(ax, bx), y2, max(az, bz))
            for b in split_box(*box):
                commands.append(f"fill {ox + b[0]} {oy + b[1]} {oz + b[2]} "
                                f"{ox + b[3]} {oy + b[4]} {oz + b[5]} {mat}")
    return commands


# --- соединение с игрой ------------------------------------------------------

class Game:
    def __init__(self, ws):
        self.ws = ws
        self.pending = {}
        self.slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        self.last_build = {}   # игрок -> (ops, origin, facing)
        self.last_code = {}    # игрок -> (запрос, код)
        self.busy = set()

    async def send(self, purpose, body, wait=True):
        request_id = str(uuid.uuid4())
        msg = {"header": {"version": 1, "requestId": request_id,
                          "messageType": "commandRequest", "messagePurpose": purpose},
               "body": body}
        if not wait:
            await self.ws.send(json.dumps(msg))
            return None
        async with self.slots:
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            await self.ws.send(json.dumps(msg))
            try:
                return await asyncio.wait_for(future, timeout=10)
            except asyncio.TimeoutError:
                return {"statusCode": -1, "statusMessage": "timeout"}
            finally:
                self.pending.pop(request_id, None)

    async def command(self, line, wait=True):
        return await self.send("commandRequest", {"version": 1, "commandLine": line,
                                                  "origin": {"type": "player"}}, wait)

    async def say(self, text):
        for line in str(text).splitlines():
            await self.command(f"say {line}", wait=False)

    async def player_position(self, player):
        name = player.replace('"', "")
        resp = await self.command(f'querytarget @a[name="{name}"]')
        try:
            target = json.loads(resp["details"])[0]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise RuntimeError(f"не получилось узнать, где стоит игрок: {resp.get('statusMessage')}")
        pos = target["position"]
        facing = round(target.get("yRot", 0) / 90) % 4   # 0 юг, 1 запад, 2 север, 3 восток
        fx, fz = rotate(0, FORWARD_OFFSET, facing)
        return (int(pos["x"] // 1) + fx, int(pos["y"] // 1), int(pos["z"] // 1) + fz), facing

    async def run_commands(self, commands):
        results = await asyncio.gather(*(self.command(c) for c in commands))
        failed = [(c, r) for c, r in zip(commands, results) if r.get("statusCode", 0) < 0]
        for c, r in failed[:3]:
            log.warning("command failed: %s -> %s", c, r.get("statusMessage"))
        return failed

    # --- обработка чата ------------------------------------------------------

    async def on_chat(self, player, text):
        text = text.strip()
        low = text.lower()
        if low in ("help", "помощь"):
            await self.say("\n".join(HELP))
        elif low == "undo":
            await self.undo(player)
        elif low.startswith("run "):
            await self.run_script(player, text[4:].strip())
        elif low.startswith("ai+ "):
            await self.ai(player, text[4:].strip(), modify=True)
        elif low.startswith("ai "):
            await self.ai(player, text[3:].strip())

    async def ai(self, player, request, modify=False):
        if player in self.busy:
            await self.say(f"{player}, подожди, я ещё строю прошлое")
            return
        self.busy.add(player)
        try:
            previous = self.last_code.get(player) if modify else None
            if modify and not previous:
                await self.say("Сначала попроси что-нибудь построить: ai <что построить>")
                return
            await self.say(f"Думаю над: {request} ...")
            t = time.time()
            if previous:
                prompt = f"{previous[0]}. Change: {request}"
                code = await llm.generate_code(previous[0], previous[1], f"Change the script: {request}")
            else:
                prompt = request
                code = await llm.generate_code(prompt)
            result = await asyncio.to_thread(sandbox.run, code)
            if result["error"]:
                log.info("AI code failed (%s), asking to fix", result["error"])
                code = await llm.generate_code(prompt, code, f"The script failed with: {result['error']}\nFix it.")
                result = await asyncio.to_thread(sandbox.run, code)
            name = save_ai_script(player, prompt, code)
            self.last_code[player] = (prompt, code)
            await self.say(f"Код готов за {time.time() - t:.0f} с, он лежит в файле {name}.py")
            await self.build(player, result, name)
        except llm.LLMError as e:
            await self.say(str(e))
        finally:
            self.busy.discard(player)

    async def run_script(self, player, name):
        if not NAME_RE.match(name) or ".." in name:
            await self.say("Имя скрипта: латинские буквы, цифры, _ и -")
            return
        path = os.path.join(SCRIPTS_DIR, name + ".py")
        if not os.path.exists(path):
            await self.say(f"Нет скрипта {name}.py")
            return
        with open(path, encoding="utf-8") as f:
            result = await asyncio.to_thread(sandbox.run, f.read())
        await self.build(player, result, name)

    async def build(self, player, result, name):
        if result["error"]:
            await self.say(f"Ошибка в {name}.py: {result['error']}")
            return
        try:
            origin, facing = await self.player_position(player)
        except RuntimeError as e:
            await self.say(str(e))
            return
        commands = ops_to_commands(result["ops"], origin, facing)
        failed = await self.run_commands(commands)
        self.last_build[player] = (result["ops"], origin, facing)
        for m in result["messages"]:
            await self.say(m)
        note = f", не получилось: {len(failed)}" if failed else ""
        await self.say(f"Поставлено {result['blocks']} блоков ({len(commands)} команд{note})")

    async def undo(self, player):
        last = self.last_build.pop(player, None)
        if not last:
            await self.say("Нечего отменять")
            return
        ops, origin, facing = last
        await self.run_commands(ops_to_commands(list(reversed(ops)), origin, facing, material="air"))
        await self.say("Убрал последнюю постройку")


def save_ai_script(player, request, code):
    folder = os.path.join(SCRIPTS_DIR, "ai")
    os.makedirs(folder, exist_ok=True)
    safe_player = re.sub(r"[^A-Za-z0-9_]", "_", player) or "player"
    name = f"ai/{safe_player}_{time.strftime('%m%d_%H%M%S')}"
    with open(os.path.join(SCRIPTS_DIR, name + ".py"), "w", encoding="utf-8") as f:
        f.write(f"# Запрос: {request}\n# Запустить снова: run {name}\n\n{code}")
    return name


def seed_examples():
    """Если скрипты лежат не рядом с кодом (например, на диске в кластере), положить туда примеры."""
    if os.path.abspath(SCRIPTS_DIR) == EXAMPLES_DIR:
        return
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    for name in os.listdir(EXAMPLES_DIR):
        target = os.path.join(SCRIPTS_DIR, name)
        if name.endswith(".py") and not os.path.exists(target):
            shutil.copy(os.path.join(EXAMPLES_DIR, name), target)
            log.info("added example script %s", name)


# --- сервер ------------------------------------------------------------------

async def handle(ws):
    path = ws.request.path if ws.request else "/"
    if TOKEN and path.strip("/") != TOKEN:
        log.warning("rejected connection with path %s", path)
        await ws.close(code=4003, reason="bad token")
        return
    log.info("Minecraft connected from %s", ws.remote_address)
    game = Game(ws)
    await game.send("subscribe", {"eventName": "PlayerMessage"}, wait=False)
    await game.say("Мост подключён! Напиши в чат: help")

    tasks = set()
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        header, body = msg.get("header", {}), msg.get("body", {})
        future = game.pending.get(header.get("requestId"))
        if future and not future.done():
            future.set_result(body)
        elif header.get("eventName") == "PlayerMessage" and body.get("type") == "chat":
            task = asyncio.create_task(safe(game.on_chat(body.get("sender", ""), body.get("message", ""))))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    log.info("Minecraft disconnected")


async def safe(coro):
    try:
        await coro
    except Exception:
        log.exception("chat handler failed")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    port = int(os.environ.get("BRIDGE_PORT", "8765"))
    seed_examples()
    missing = llm.missing_settings()
    if missing:
        log.warning("%s not set: 'ai' command is disabled, 'run' still works", ", ".join(missing))
    else:
        log.info("LLM: %s, model %s", os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"])
    async with websockets.serve(handle, "0.0.0.0", port, max_size=2**22):
        log.info("Listening on :%d, in the game type: /connect localhost:%d%s",
                 port, port, "/" + TOKEN if TOKEN else "")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
