"""Задачи для модели в контексте мира: «расчисти задний двор у #1», «проложи дорожку к двери».

Модель не угадывает рельеф, а спрашивает мир через инструменты (всё в координатах постройки):
  get_build(id)                      — что за постройка, её габариты и код
  scan_terrain(x1, z1, x2, z2)       — высоты земли и что на поверхности (деревья, вода)
  get_blocks(x1, y1, z1, x2, y2, z2) — какие блоки в объёме, по слоям
и в конце пишет обычный скрипт (block, fill, ...), который ставится на то же место.

Мир читает сайдкар world-reader (mcai/world_server.py) из файлов сервера.
"""
import httpx

from mcai.game import FORWARD_OFFSET, rotate
from mcai.llm import API_DOC, STYLE

MAX_SCAN = 64          # сторона участка для scan_terrain
Y_BELOW, Y_ABOVE = 48, 64


class WorldReader:
    """Мир глазами модели: перед чтением просим сервер сохранить мир на диск."""

    def __init__(self, rcon, url):
        self.rcon = rcon
        self.url = url.rstrip("/")

    async def _get(self, path, **params):
        await self.rcon("save-all flush")
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(f"{self.url}/{path}", params=params)
            if resp.status_code != 200:
                raise RuntimeError(resp.text)
            return resp.json()

    async def surface(self, x1, z1, x2, z2, ymin, ymax):
        return await self._get("surface", x1=x1, z1=z1, x2=x2, z2=z2, ymin=ymin, ymax=ymax)

    async def blocks(self, x1, y1, z1, x2, y2, z2):
        return await self._get("blocks", x1=x1, y1=y1, z1=z1, x2=x2, y2=y2, z2=z2)


class Frame:
    """Система координат задачи: как у постройки — (0,0,0) у ног строителя + 3 блока вперёд, +z вперёд."""

    def __init__(self, origin, facing):
        self.origin, self.facing = tuple(origin), facing

    def to_world(self, x, y, z):
        wx, wz = rotate(x, z, self.facing)
        return self.origin[0] + wx, self.origin[1] + y, self.origin[2] + wz

    def world_box(self, x1, z1, x2, z2):
        corners = [self.to_world(x, 0, z) for x in (x1, x2) for z in (z1, z2)]
        xs, zs = [c[0] for c in corners], [c[2] for c in corners]
        return min(xs), min(zs), max(xs), max(zs)


def build_bbox(ops):
    xs, ys, zs = [], [], []
    for op in ops:
        if op[-1] == "air":
            continue
        if op[0] == "block":
            xs.append(op[1]); ys.append(op[2]); zs.append(op[3])
        else:
            xs += [op[1], op[4]]; ys += [op[2], op[5]]; zs += [op[3], op[6]]
    if not xs:
        return None
    return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)


TOOLS = [
    {"type": "function", "function": {
        "name": "get_build",
        "description": "Info about an existing build: request, bounding box in task coordinates and its script.",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "scan_terrain",
        "description": "Ground height map of a rectangle (task coordinates, max 64x64). For every column: y of the "
                       "top ground block (relative), 'T' = tree/plants above the ground, '~' = water on top.",
        "parameters": {"type": "object", "properties": {
            "x1": {"type": "integer"}, "z1": {"type": "integer"}, "x2": {"type": "integer"}, "z2": {"type": "integer"}},
            "required": ["x1", "z1", "x2", "z2"]}}},
    {"type": "function", "function": {
        "name": "get_blocks",
        "description": "Which blocks are inside a box (task coordinates), counted per layer y. Max 64x64x32.",
        "parameters": {"type": "object", "properties": {k: {"type": "integer"} for k in
                                                        ("x1", "y1", "z1", "x2", "y2", "z2")},
                       "required": ["x1", "y1", "z1", "x2", "y2", "z2"]}}},
]


def system_prompt(frame_note):
    return f"""You are a Minecraft builder assistant. You do tasks in an existing world: clear land, level ground,
make paths, dig out or cover things. You can look at the world with the tools before writing code.

Coordinates of this task (all tools and the script use them):
{frame_note}
y goes up, +z goes forward (away from where the builder stood), +x is to the builder's left.
The tools return everything in these same coordinates.

How to work:
1. Look first: get_build for the build you work with, scan_terrain for the area around it
   (usually the build box plus 10-15 blocks on each side), get_blocks if you need details.
2. Then reply with ONE ```python code block: the script that does the task.
   To remove terrain use fill(..., "air"). To level ground fill below with "dirt" and put "grass_block" on top.
   Do not touch the build itself unless asked. Keep it under 50000 blocks, coordinates within ±80, y from -40 to 120.

{API_DOC}
{STYLE}"""


def frame_note(build):
    if build:
        bbox = build_bbox(build["ops"])
        box = f"x {bbox[0]}..{bbox[3]}, y {bbox[1]}..{bbox[4]}, z {bbox[2]}..{bbox[5]}" if bbox else "empty"
        return (f"Same as build #{build['id']} ({build['name']}): its blocks occupy {box}.\n"
                f"The builder stood at z=-{FORWARD_OFFSET} looking toward +z, so the side of the build with the "
                f"smallest z faces the builder (usually its front); larger z is behind it (the back yard).\n"
                f"y=0 is the level the build stands on (y=-1 is its ground).")
    return (f"(0, 0, 0) is {FORWARD_OFFSET} blocks in front of the player at feet level, +z is where the player "
            f"looks, y=-1 is the ground under the player.")


class TaskTools:
    def __init__(self, reader, frame, store):
        self.reader, self.frame, self.store = reader, frame, store

    async def call(self, name, args):
        if name == "get_build":
            return self.get_build(int(args["id"]))
        if name == "scan_terrain":
            return await self.scan_terrain(*(int(args[k]) for k in ("x1", "z1", "x2", "z2")))
        if name == "get_blocks":
            return await self.get_blocks(*(int(args[k]) for k in ("x1", "y1", "z1", "x2", "y2", "z2")))
        return f"Unknown tool {name}"

    def get_build(self, build_id):
        b = self.store.builds.get(build_id)
        if not b:
            return f"No build #{build_id}"
        if tuple(b["origin"]) != self.frame.origin or b["facing"] != self.frame.facing:
            return f"Build #{build_id} ({b['name']}) uses other coordinates; only its request: {b['request']}"
        bbox = build_bbox(b["ops"])
        return (f"#{b['id']} {b['name']}: request «{b['request']}», {b['blocks']} blocks, "
                f"box x {bbox[0]}..{bbox[3]}, y {bbox[1]}..{bbox[4]}, z {bbox[2]}..{bbox[5]}\n"
                f"Script:\n{b['code'][:4000]}")

    async def scan_terrain(self, x1, z1, x2, z2):
        x1, x2, z1, z2 = min(x1, x2), max(x1, x2), min(z1, z2), max(z1, z2)
        if x2 - x1 + 1 > MAX_SCAN or z2 - z1 + 1 > MAX_SCAN:
            return f"Too big, max {MAX_SCAN}x{MAX_SCAN}"
        oy = self.frame.origin[1]
        wx1, wz1, wx2, wz2 = self.frame.world_box(x1, z1, x2, z2)
        data = await self.reader.surface(wx1, wz1, wx2, wz2, oy - Y_BELOW, oy + Y_ABOVE)
        cols = {}
        for i, row in enumerate(data["rows"]):
            for j, cell in enumerate(row):
                cols[(data["x1"] + j, data["z1"] + i)] = cell
        lines = [f"Ground height (relative y of the top ground block) for x {x1}..{x2} (left to right), "
                 f"z {z2}..{z1} (top to bottom). Marks: T = trees/plants above the ground, ~ = water, "
                 f"v = ground is lower than y=-{Y_BELOW}, ? = no data (world not generated there).",
                 "  z\\x " + "".join(f"{x:>5}" for x in range(x1, x2 + 1))]
        for z in range(z2, z1 - 1, -1):
            cells = []
            for x in range(x1, x2 + 1):
                wx, _, wz = self.frame.to_world(x, 0, z)
                c = cols.get((wx, wz))
                if not c or c.get("missing"):
                    cells.append("    ?")
                elif c["ground"] is None:
                    cells.append("    v")
                else:
                    mark = "~" if "water" in (c["ground_block"] or "") else ("T" if c["top"] != c["ground"] else " ")
                    cells.append(f"{c['ground'] - oy:>4}{mark}")
            lines.append(f"{z:>6} " + "".join(cells))
        return "\n".join(lines)

    async def get_blocks(self, x1, y1, z1, x2, y2, z2):
        wx1, wz1, wx2, wz2 = self.frame.world_box(x1, z1, x2, z2)
        oy = self.frame.origin[1]
        data = await self.reader.blocks(wx1, oy + min(y1, y2), wz1, wx2, oy + max(y1, y2), wz2)
        lines = []
        for y, counts in sorted(data["layers"].items(), key=lambda kv: -int(kv[0])):
            solid = {k.removeprefix("minecraft:"): v for k, v in counts.items() if k != "minecraft:air"}
            text = ", ".join(f"{k}×{v}" for k, v in sorted(solid.items(), key=lambda kv: -kv[1])) or "all air"
            lines.append(f"y={int(y) - oy}: {text}")
        return "\n".join(lines)
