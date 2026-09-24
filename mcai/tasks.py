"""Задачи для модели в контексте мира: «расчисти задний двор у #1», «проложи дорожку к двери».

Модель не угадывает рельеф, а спрашивает мир через инструменты (всё в координатах постройки):
  get_build(id)                      — что за постройка, её габариты и код
  scan_terrain(x1, z1, x2, z2)       — высоты земли и что на поверхности (деревья, вода)
  get_blocks(x1, y1, z1, x2, y2, z2) — какие блоки в объёме, по слоям
и в конце пишет обычный скрипт (block, fill, ...), который ставится на то же место.

Мир читает сайдкар world-reader (mcai/world_server.py) из файлов сервера.
"""
import base64
from collections import Counter

import httpx

from mcai.game import FORWARD_OFFSET, rotate
from mcai.llm import API_DOC, STYLE
from mcai.world_server import kind

MAX_SCAN = 64          # сторона участка для scan_terrain
Y_BELOW, Y_ABOVE = 48, 64
VOLUME_SIDE = 64       # объём читаем кубами не больше 64x64x64


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

    async def volume(self, x1, y1, z1, x2, y2, z2):
        """Все блоки объёма (мировые координаты): {(x, y, z): 'minecraft:stone'}; большие — по кускам."""
        await self.rcon("save-all flush")
        out = {}
        async with httpx.AsyncClient(timeout=120) as client:
            for bx in range(x1, x2 + 1, VOLUME_SIDE):
                for bz in range(z1, z2 + 1, VOLUME_SIDE):
                    for by in range(y1, y2 + 1, VOLUME_SIDE):
                        box = dict(x1=bx, y1=by, z1=bz, x2=min(bx + VOLUME_SIDE - 1, x2),
                                   y2=min(by + VOLUME_SIDE - 1, y2), z2=min(bz + VOLUME_SIDE - 1, z2))
                        resp = await client.get(f"{self.url}/volume", params=box)
                        if resp.status_code != 200:
                            raise RuntimeError(resp.text)
                        d = resp.json()
                        raw = base64.b64decode(d["data"])
                        i = 0
                        for y in range(d["y1"], d["y2"] + 1):
                            for z in range(d["z1"], d["z2"] + 1):
                                for x in range(d["x1"], d["x2"] + 1):
                                    out[(x, y, z)] = d["palette"][raw[i] | raw[i + 1] << 8]
                                    i += 2
        return out


def op_cells(ops):
    """Клетки, которые ставят операции: {(x, y, z): материал} (в координатах постройки)."""
    out = {}
    for op in ops:
        if op[0] == "block":
            out[(op[1], op[2], op[3])] = op[4]
        elif op[0] == "fill":
            for x in range(op[1], op[4] + 1):
                for y in range(op[2], op[5] + 1):
                    for z in range(op[3], op[6] + 1):
                        out[(x, y, z)] = op[7]
    return out


_world_cells_cache = {}


def build_world_cells(b):
    """Мировые клетки постройки (без воздуха) — их нельзя трогать при расчистке."""
    key = (b["id"], b["updated"])
    if key not in _world_cells_cache:
        frame = Frame(b["origin"], b["facing"])
        _world_cells_cache[key] = {frame.to_world(*c) for c, m in op_cells(b["ops"]).items() if m != "air"}
    return _world_cells_cache[key]


def protected_cells(store):
    cells = set()
    for b in store.builds.values():
        cells |= build_world_cells(b)
    return cells


def base_level(ops):
    """На каком уровне стоит постройка: чаще всего встречающийся самый нижний блок колонки."""
    lowest = {}
    for (x, y, z), m in op_cells(ops).items():
        if m != "air":
            lowest[(x, z)] = min(y, lowest.get((x, z), y))
    return Counter(lowest.values()).most_common(1)[0][0] if lowest else 0


def natural(name):
    return kind(name) in ("solid", "plant", "fluid")


async def expand_clear_terrain(ops, frame, reader, store):
    """clear_terrain(...) -> обычные fill(..., "air") только по природным блокам, не по постройкам."""
    boxes = [op for op in ops if op[0] == "clear_terrain"]
    if not boxes:
        return ops, 0
    protected = protected_cells(store)
    out = [op for op in ops if op[0] != "clear_terrain"]
    removed = 0
    # блоки построек в этих же координатах — чтобы вернуть то, что испортила насыпавшаяся земля
    # (трава под землёй сама становится землёй)
    own = {}
    for b in store.builds.values():
        if tuple(b["origin"]) == frame.origin and b["facing"] == frame.facing:
            own.update({c: m for c, m in op_cells(b["ops"]).items() if m != "air"})
    for _, x1, y1, z1, x2, y2, z2 in boxes:
        wx1, wz1, wx2, wz2 = frame.world_box(x1, z1, x2, z2)
        oy = frame.origin[1]
        # на слой ниже: газон постройки обычно лежит прямо под расчищаемым объёмом
        world = await reader.volume(wx1, oy + y1 - 1, wz1, wx2, oy + y2, wz2)
        for (x, y, z), m in own.items():
            if x1 <= x <= x2 and y1 - 1 <= y <= y2 and z1 <= z <= z2:
                if world.get(frame.to_world(x, y, z)) not in ("minecraft:" + m, m):
                    out.append(["block", x, y, z, m])
        for x in range(x1, x2 + 1):
            for z in range(z1, z2 + 1):
                run = None
                for y in range(y1, y2 + 2):
                    w = frame.to_world(x, y, z) if y <= y2 else None
                    clear = w is not None and w not in protected and natural(world.get(w))
                    if clear:
                        removed += 1
                        run = run if run is not None else y
                    elif run is not None:
                        out.append(["fill", x, run, z, x, y - 1, z, "air"] if y - 1 > run
                                   else ["block", x, run, z, "air"])
                        run = None
    return out, removed


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
        if op[0] not in ("block", "fill") or op[-1] == "air":
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
        "name": "terrain_in_build",
        "description": "Natural terrain (dirt, stone, grass, trees, water) that is inside or next to a build but is "
                       "NOT part of it — e.g. a hill that covers the back yard. Returns a height map of it.",
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
1. Look first. For a build: get_build, and terrain_in_build if the task is about terrain (a hill, dirt, trees)
   covering or crowding the build — it tells exactly which blocks are nature and which belong to the build.
   scan_terrain shows the ground around (build box plus 10-15 blocks), get_blocks gives details.
   Do not repeat a call with the same arguments — you already have that answer.
2. Then reply with ONE ```python code block: the script that does the task.
   - To remove natural terrain (hill, dirt, stone, trees, water) use clear_terrain(x1, y1, z1, x2, y2, z2):
     it removes only nature and never the blocks of any build, so a generous box is safe.
     Start y1 one block above the build's ground (the level terrain_in_build reports) so the ground stays.
   - fill(..., "air") removes EVERYTHING in the box, including the build — use it only where you are sure.
   - The ground level is different for every build: take it from terrain_in_build / get_build, do not guess.
   - Prefer a few big boxes over loops of block(): at most 20000 operations and 50000 blocks.
   - Do not change the build itself unless asked. Coordinates within ±80, y from -40 to 120.

{API_DOC}
{STYLE}"""


def frame_note(build):
    if build:
        bbox = build_bbox(build["ops"])
        box = f"x {bbox[0]}..{bbox[3]}, y {bbox[1]}..{bbox[4]}, z {bbox[2]}..{bbox[5]}" if bbox else "empty"
        return (f"Same as build #{build['id']} ({build['name']}): its blocks occupy {box}.\n"
                f"The builder stood at z=-{FORWARD_OFFSET} looking toward +z, so the side of the build with the "
                f"smallest z faces the builder (usually its front); larger z is behind it (the back yard).\n"
                f"Its lowest blocks are at y={base_level(build['ops'])}.")
    return (f"(0, 0, 0) is {FORWARD_OFFSET} blocks in front of the player at feet level, +z is where the player "
            f"looks, y=-1 is the block under the player's feet.")


class TaskTools:
    def __init__(self, reader, frame, store):
        self.reader, self.frame, self.store = reader, frame, store
        self.asked = set()

    async def call(self, name, args):
        key = (name, tuple(sorted(args.items())))
        if key in self.asked:
            return "You already asked exactly this — use the previous answer. Now write the script."
        self.asked.add(key)
        if name == "get_build":
            return self.get_build(int(args["id"]))
        if name == "terrain_in_build":
            return await self.terrain_in_build(int(args["id"]))
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

    async def terrain_in_build(self, build_id, margin=3, above=12):
        b = self.store.builds.get(build_id)
        if not b:
            return f"No build #{build_id}"
        if tuple(b["origin"]) != self.frame.origin or b["facing"] != self.frame.facing:
            return f"Build #{build_id} uses other coordinates"
        x1, y1, z1, x2, y2, z2 = build_bbox(b["ops"])
        base = base_level(b["ops"])
        x1, z1, x2, z2, y2 = x1 - margin, z1 - margin, x2 + margin, z2 + margin, y2 + above
        wx1, wz1, wx2, wz2 = self.frame.world_box(x1, z1, x2, z2)
        oy = self.frame.origin[1]
        world = await self.reader.volume(wx1, oy + base, wz1, wx2, oy + y2, wz2)
        protected = protected_cells(self.store)
        own = op_cells(b["ops"])
        # что считаем «наползшим»: выше самого нижнего блока постройки в колонке (под газоном — это земля),
        # а там, где постройки нет, — выше уровня, на котором она стоит
        lowest = {}
        for (x, y, z), m in own.items():
            if m != "air":
                lowest[(x, z)] = min(y, lowest.get((x, z), y))
        tops, per_y, kinds = {}, Counter(), Counter()
        for x in range(x1, x2 + 1):
            for z in range(z1, z2 + 1):
                for y in range(lowest.get((x, z), base) + 1, y2 + 1):
                    w = self.frame.to_world(x, y, z)
                    name = world.get(w)
                    if w in protected or not natural(name):
                        continue
                    tops[(x, z)] = max(y, tops.get((x, z), y))
                    per_y[y] += 1
                    kinds[name.removeprefix("minecraft:")] += 1
        floor = Counter(y for (x, y, z), m in own.items() if m in ("grass_block", "dirt_path")
                        or m.endswith("_planks") and y <= base + 1).most_common(1)
        lines = [f"Build #{build_id} ({b['name']}): box x {x1 + margin}..{x2 - margin}, z {z1 + margin}..{z2 - margin}; "
                 f"it stands on y={base} (its lowest blocks)" +
                 (f", its own lawn/floor is mostly at y={floor[0][0]}" if floor else "") + ".",
                 f"Natural blocks above the build's ground inside x {x1}..{x2}, z {z1}..{z2} "
                 f"that are NOT part of any build: "
                 f"{sum(per_y.values())} ({', '.join(f'{k}×{v}' for k, v in kinds.most_common(5)) or 'none'}).",
                 "Per layer: " + (", ".join(f"y={y}: {n}" for y, n in sorted(per_y.items())) or "none")]
        if tops:
            lines.append("Map: highest natural block y per column ('.' = none), x left to right, z top to bottom:")
            lines.append("  z\\x " + "".join(f"{x:>4}" for x in range(x1, x2 + 1)))
            for z in range(z2, z1 - 1, -1):
                lines.append(f"{z:>5} " + "".join(f"{tops[(x, z)]:>4}" if (x, z) in tops else "   ."
                                                    for x in range(x1, x2 + 1)))
            lines.append("To remove it without touching the build use clear_terrain(x1, y1, z1, x2, y2, z2) over "
                         "those columns: it removes only natural blocks.")
        return "\n".join(lines)

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
