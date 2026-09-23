"""Имитатор Minecraft: подключается к мосту как игра и пишет в "чат".

  python tools/simulator.py "run tower"
  python tools/simulator.py --url ws://bridge:8765/TOKEN "ai домик на дереве" "ai+ добавь лестницу"

Показывает сообщения моста и вид сверху на то, что построилось.
"""
import argparse
import asyncio
import json
import re
import uuid

import websockets

PLAYER = {"x": 10.5, "y": 64.0, "z": 20.5, "yRot": 0.0}
FILL_RE = re.compile(r"^fill (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (\S+)$")
SETBLOCK_RE = re.compile(r"^setblock (-?\d+) (-?\d+) (-?\d+) (\S+)$")


class FakeWorld:
    def __init__(self):
        self.blocks = {}
        self.commands = 0

    def apply(self, line):
        self.commands += 1
        if m := FILL_RE.match(line):
            x1, y1, z1, x2, y2, z2 = map(int, m.groups()[:6])
            if (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1) > 32768:
                return -2147483648, "Too many blocks in the specified area"
            for x in range(x1, x2 + 1):
                for y in range(y1, y2 + 1):
                    for z in range(z1, z2 + 1):
                        self._set(x, y, z, m.group(7))
            return 0, "filled"
        if m := SETBLOCK_RE.match(line):
            self._set(*map(int, m.groups()[:3]), m.group(4))
            return 0, "placed"
        return 0, "ok"

    def _set(self, x, y, z, mat):
        if mat == "air":
            self.blocks.pop((x, y, z), None)
        else:
            self.blocks[(x, y, z)] = mat

    def top_view(self, size=30):
        if not self.blocks:
            return "(пусто)"
        cx, cz = int(PLAYER["x"]), int(PLAYER["z"])
        rows = []
        for z in range(cz + size, cz - 2, -1):
            row = ""
            for x in range(cx - size, cx + size + 1):
                if (x, z) == (cx, cz):
                    row += "@"
                    continue
                ys = [y for (bx, y, bz) in self.blocks if bx == x and bz == z]
                row += " " if not ys else "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"[min(35, max(ys) - int(PLAYER["y"]) + 1)] \
                    if max(ys) >= int(PLAYER["y"]) - 1 else "."
            rows.append(row.rstrip())
        while rows and not rows[0]:
            rows.pop(0)
        return "\n".join(rows)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8765")
    ap.add_argument("--quiet-seconds", type=float, default=3.0, help="сколько ждать тишины после сообщения")
    ap.add_argument("messages", nargs="+")
    args = ap.parse_args()

    world = FakeWorld()
    last_activity = asyncio.Event()

    async with websockets.connect(args.url) as ws:
        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                last_activity.set()
                header, body = msg["header"], msg["body"]
                if header["messagePurpose"] != "commandRequest":
                    continue
                line = body["commandLine"]
                resp = {"statusCode": 0}
                if line.startswith("say "):
                    print(f"  [мост] {line[4:]}")
                    continue  # say без ожидания ответа
                if line.startswith("querytarget"):
                    resp["details"] = json.dumps([{"dimension": 0, "id": 1, "uniqueId": "-1",
                                                   "position": {k: PLAYER[k] for k in "xyz"},
                                                   "yRot": PLAYER["yRot"]}])
                else:
                    resp["statusCode"], resp["statusMessage"] = world.apply(line)
                await ws.send(json.dumps({"header": {"requestId": header["requestId"],
                                                     "messagePurpose": "commandResponse", "version": 1},
                                          "body": resp}))

        reader_task = asyncio.create_task(reader())

        async def wait_quiet():
            while True:
                last_activity.clear()
                try:
                    await asyncio.wait_for(last_activity.wait(), args.quiet_seconds)
                except asyncio.TimeoutError:
                    return

        await wait_quiet()
        for text in args.messages:
            print(f"> {text}")
            await ws.send(json.dumps({"header": {"eventName": "PlayerMessage", "messagePurpose": "event",
                                                 "requestId": str(uuid.uuid4()), "version": 1},
                                      "body": {"message": text, "sender": "Steve", "type": "chat"}}))
            await wait_quiet()
        reader_task.cancel()

    print(f"\nКоманд выполнено: {world.commands}, блоков в мире: {len(world.blocks)}")
    print("Вид сверху (@ — игрок, цифра — высота постройки над землёй):")
    print(world.top_view())


if __name__ == "__main__":
    asyncio.run(main())
