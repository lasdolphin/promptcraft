"""Мост для Minecraft Education / Bedrock: websocket-сервер для команды /connect.
Команды чата — в mcai/game.py."""
import asyncio
import json
import logging
import os
import uuid

import websockets

from mcai.builds import BuildStore
from mcai.game import Session, builds_folder, safe, seed_examples, log_llm_settings

log = logging.getLogger("bridge")

TOKEN = os.environ.get("BRIDGE_TOKEN", "")
STORE = None   # постройки общие для всех подключений к мосту


class Game(Session):
    def __init__(self, ws):
        super().__init__(STORE)
        self.ws = ws
        self.pending = {}

    def selector(self, player):
        return '"' + player.replace('"', "") + '"'

    async def send(self, purpose, body, wait=True):
        request_id = str(uuid.uuid4())
        msg = {"header": {"version": 1, "requestId": request_id,
                          "messageType": "commandRequest", "messagePurpose": purpose},
               "body": body}
        if not wait:
            await self.ws.send(json.dumps(msg))
            return None
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError:
            return {"statusCode": -1, "statusMessage": "timeout"}
        finally:
            self.pending.pop(request_id, None)

    async def raw_command(self, line, wait=True):
        return await self.send("commandRequest", {"version": 1, "commandLine": line,
                                                  "origin": {"type": "player"}}, wait)

    async def command(self, line):
        resp = await self.raw_command(line)
        return resp.get("statusCode", 0) >= 0, resp.get("statusMessage", "")

    async def say(self, text):
        for line in str(text).splitlines():
            await self.raw_command(f"say {line}", wait=False)

    async def player_position(self, player):
        name = player.replace('"', "")
        resp = await self.raw_command(f'querytarget @a[name="{name}"]')
        try:
            target = json.loads(resp["details"])[0]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise RuntimeError(f"не получилось узнать, где стоит игрок: {resp.get('statusMessage')}")
        pos = target["position"]
        return (pos["x"], pos["y"], pos["z"]), target.get("yRot", 0)


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


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    global STORE
    port = int(os.environ.get("BRIDGE_PORT", "8765"))
    seed_examples()
    STORE = BuildStore(builds_folder("education"))
    log_llm_settings()
    async with websockets.serve(handle, "0.0.0.0", port, max_size=2**22):
        log.info("Listening on :%d, in the game type: /connect localhost:%d%s",
                 port, port, "/" + TOKEN if TOKEN else "")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
