"""Мост для Minecraft Java (сервер Paper): команды чата те же, что в mcai/game.py.

Как он общается с сервером:
- команды (setblock, fill, tellraw) отправляет по RCON;
- сообщения чата читает из лога сервера: строки вида "[12:34:56 INFO]: <Игрок> текст".

Откуда брать лог (переменная MC_LOG_CMD):
- не задана — из Kubernetes API: лог пода с меткой MC_POD_SELECTOR (так работает в кластере);
- задана — из вывода команды, например: MC_LOG_CMD="docker logs -f --since 0s mc".

Кто может строить — mcai/access.py (вход с одобрением), админка — mcai/admin_web.py.
Команды админов в чате: allow <ник>, deny <ник>, players, open, close.
"""
import asyncio
import datetime
import json
import logging
import os
import re
import struct

import httpx

from mcai import admin_web
from mcai.access import Access
from mcai.builds import BuildStore
from mcai.game import SCRIPTS_DIR, Session, builds_folder, safe, seed_examples, log_llm_settings

log = logging.getLogger("java")

RCON_HOST = os.environ.get("RCON_HOST", "localhost")
RCON_PORT = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")
RCON_CONNECTIONS = 4
# ники админов через запятую (игроки с Bedrock — с точкой: .Steve)
ADMINS = [a.strip() for a in os.environ.get("ADMINS", "").split(",") if a.strip()]
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8080"))
ACCESS_FILE = os.environ.get("ACCESS_FILE", os.path.join(SCRIPTS_DIR, ".access.json"))

# Игроки с Bedrock (через Geyser/Floodgate) пишут с точкой перед ником: <.Steve>
CHAT_RE = re.compile(r"\]: (?:\[Not Secure\] )?<(\.?[A-Za-z0-9_]{1,16})> (.*)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:E-?\d+)?")

# Если модель или скрипт назовёт блок по-бедроковски — переведём в название Java
JAVA_NAMES = {
    "grass": "grass_block",
    "planks": "oak_planks",
    "log": "oak_log",
    "leaves": "oak_leaves",
    "wool": "white_wool",
    "concrete": "white_concrete",
    "stonebrick": "stone_bricks",
    "brick_block": "bricks",
    "red_flower": "poppy",
    "yellow_flower": "dandelion",
    "web": "cobweb",
}

# Ответы сервера, которые значат, что команда не сработала.
# "Could not set the block" и "No blocks were filled" — не ошибки: там уже стоит такой блок.
FAILURES = ("Unknown", "Incorrect argument", "Invalid", "Expected", "not loaded",
            "Too many blocks", "out of the world", "No player was found")


# --- RCON ----------------------------------------------------------------------

class RconError(RuntimeError):
    pass


class Rcon:
    """Одно соединение RCON: пакет = длина, id, тип, текст, два нулевых байта."""

    def __init__(self):
        self.reader = self.writer = None
        self.next_id = 0

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(RCON_HOST, RCON_PORT)
        if await self._request(3, RCON_PASSWORD) is None:
            raise RconError("RCON: неверный пароль")

    async def _request(self, kind, body):
        self.next_id += 1
        data = struct.pack("<ii", self.next_id, kind) + body.encode("utf-8") + b"\0\0"
        self.writer.write(struct.pack("<i", len(data)) + data)
        await self.writer.drain()
        while True:
            size, = struct.unpack("<i", await self.reader.readexactly(4))
            packet = await self.reader.readexactly(size)
            request_id, _ = struct.unpack("<ii", packet[:8])
            if request_id == -1:
                return None   # так сервер отвечает на неверный пароль
            if request_id == self.next_id:
                return packet[8:-2].decode("utf-8", errors="replace")

    async def command(self, line):
        return await self._request(2, line)

    def close(self):
        if self.writer:
            self.writer.close()
        self.reader = self.writer = None


class RconPool:
    """Несколько соединений: сервер выполняет команды по очереди в каждом из них."""

    def __init__(self, size):
        self.free = asyncio.Queue()
        for _ in range(size):
            self.free.put_nowait(Rcon())

    async def command(self, line):
        conn = await self.free.get()
        try:
            for attempt in range(2):
                try:
                    if not conn.writer:
                        await conn.connect()
                    return await conn.command(line)
                except (OSError, asyncio.IncompleteReadError) as e:
                    conn.close()
                    if attempt:
                        raise RconError(f"RCON недоступен: {e}") from e
        finally:
            self.free.put_nowait(conn)


# --- игра ------------------------------------------------------------------------

class JavaGame(Session):
    parallel = RCON_CONNECTIONS

    def __init__(self, rcon):
        super().__init__(BuildStore(builds_folder("java")))
        self.rcon = rcon
        self.access = Access(self.rcon.command, self.tell, ADMINS, ACCESS_FILE)

    def is_admin(self, player):
        return self.access.is_admin(player)

    async def on_chat(self, player, text):
        words = text.strip().split()
        if not words:
            return
        cmd = words[0].lower()
        if cmd in ("allow", "deny", "players", "open", "close") and self.access.is_admin(player):
            await self.admin_command(player, cmd, words[1:])
        elif self.access.is_approved(player):
            await super().on_chat(player, text)
        elif cmd in ("ai", "ai+", "run", "undo", "help"):
            await self.tell(player, "Ты пока гость: команды заработают, когда админ тебя одобрит.")

    async def admin_command(self, player, cmd, args):
        a = self.access
        if cmd in ("allow", "deny"):
            if not args:
                await self.tell(player, f"Напиши: {cmd} <ник>")
                return
            name = a.resolve(args[0]) or args[0]
            await self.tell(player, await (a.approve(name) if cmd == "allow" else a.remove(name)))
        elif cmd in ("open", "close"):
            await self.tell(player, await a.set_open(cmd == "open"))
        else:
            s = a.state()
            waiting = ", ".join(p["name"] for p in s["pending"]) or "никто"
            await self.tell(player, f"Приём {'открыт' if s['open'] else 'закрыт'}. "
                                    f"Одобрены: {', '.join(s['approved']) or 'никто'}. Ждут: {waiting}")

    async def tell(self, player, text):
        for line in str(text).splitlines():
            message = json.dumps({"text": f"[AI] {line}", "color": "yellow"})
            await self.command(f"tellraw {player} {message}")

    async def command(self, line):
        try:
            reply = await self.rcon.command(line)
        except RconError as e:
            return False, str(e)
        return not any(f in reply for f in FAILURES), reply

    async def say(self, text):
        for line in str(text).splitlines():
            # ensure_ascii: русские буквы уходят как \uXXXX — так они точно доживут до чата
            message = json.dumps({"text": f"[AI] {line}", "color": "aqua"})
            await self.command(f"tellraw @a {message}")

    async def player_position(self, player):
        pos = await self._entity_numbers(player, "Pos")
        rotation = await self._entity_numbers(player, "Rotation")
        if len(pos) != 3 or not rotation:
            raise RuntimeError(f"не получилось узнать, где стоит игрок {player}")
        return pos, rotation[0]

    async def _entity_numbers(self, player, path):
        # Ответ: "Steve has the following entity data: [10.5d, 64.0d, 20.5d]"
        reply = await self.rcon.command(f"data get entity {player} {path}")
        _, _, data = reply.partition(": ")
        return [float(n) for n in NUMBER_RE.findall(data)]

    def block_name(self, material):
        return JAVA_NAMES.get(material, material)


# --- чтение чата из лога ---------------------------------------------------------

async def log_lines_from_command(cmd):
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
    async for raw in proc.stdout:
        yield raw.decode("utf-8", errors="replace")
    raise RuntimeError(f"команда чтения лога завершилась: {await proc.wait()}")


async def log_lines_from_kubernetes():
    """Лог пода сервера через Kubernetes API (нужна роль с правами pods и pods/log)."""
    sa = "/var/run/secrets/kubernetes.io/serviceaccount"
    with open(f"{sa}/token") as f:
        token = f.read().strip()
    with open(f"{sa}/namespace") as f:
        namespace = f.read().strip()
    selector = os.environ.get("MC_POD_SELECTOR", "app=minecraft")
    container = os.environ.get("MC_CONTAINER", "minecraft")
    base = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/pods"
    since = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with httpx.AsyncClient(verify=f"{sa}/ca.crt", timeout=httpx.Timeout(10, read=None),
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        pods = (await client.get(base, params={"labelSelector": selector})).raise_for_status().json()
        running = [p["metadata"]["name"] for p in pods["items"] if p["status"].get("phase") == "Running"]
        if not running:
            raise RuntimeError(f"нет запущенного пода с меткой {selector}")
        log.info("reading chat from pod %s", running[0])
        params = {"container": container, "follow": "true", "sinceTime": since}
        async with client.stream("GET", f"{base}/{running[0]}/log", params=params) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                yield line


def parse_chat(line):
    """'[12:34:56 INFO]: <Steve> ai домик' -> ('Steve', 'ai домик')"""
    m = CHAT_RE.search(ANSI_RE.sub("", line).rstrip())
    return (m.group(1), m.group(2)) if m else None


async def follow_chat(game):
    cmd = os.environ.get("MC_LOG_CMD")
    tasks = set()
    while True:
        try:
            lines = log_lines_from_command(cmd) if cmd else log_lines_from_kubernetes()
            # новый лог — возможно, сервер перезапустился: сверить списки и режим приёма
            sync_task = asyncio.create_task(game.access.sync_with_retry())
            async for line in lines:
                line = ANSI_RE.sub("", line).rstrip()
                # вход/выход игроков — по порядку, чтобы одобрение не обогнало вход
                await safe(game.access.on_log_line(line))
                chat = parse_chat(line)
                if chat:
                    task = asyncio.create_task(safe(game.on_chat(*chat)))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except Exception as e:
            log.warning("chat log stopped (%s), retrying in 5 s", e)
        await asyncio.sleep(5)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    seed_examples()
    log_llm_settings()
    game = JavaGame(RconPool(RCON_CONNECTIONS))
    await admin_web.start(game, ADMIN_TOKEN, ADMIN_PORT)
    log.info("RCON %s:%d, admins %s, waiting for chat commands", RCON_HOST, RCON_PORT, ADMINS or "-")
    await follow_chat(game)


if __name__ == "__main__":
    asyncio.run(main())
