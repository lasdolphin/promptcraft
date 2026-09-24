"""Сайдкар в поде сервера Java: отдаёт по HTTP, что лежит в мире (читает файлы регионов, только чтение).

  GET /surface?x1=&z1=&x2=&z2=&ymin=&ymax=   — для каждой колонки: земля и что на самом верху
  GET /blocks?x1=&y1=&z1=&x2=&y2=&z2=        — какие блоки в объёме, по слоям
  GET /volume?x1=&y1=&z1=&x2=&y2=&z2=        — все блоки объёма поимённо (палитра + индексы)

Координаты — мировые. Перед запросом мост делает на сервере `save-all flush`, чтобы файлы были свежими.
"""
import base64
import glob
import logging
import os

from aiohttp import web

from mcai.anvil import World

log = logging.getLogger("world")

MAX_COLUMNS = 96 * 96
MAX_VOLUME = 64 * 64 * 32

# что не считается землёй: растительность, деревья, снег-слой
PLANT_WORDS = ("leaves", "_log", "_wood", "stem", "hyphae", "sapling", "short_grass", "tall_grass", "fern", "flower",
               "tulip", "orchid", "allium", "bluet", "daisy", "poppy", "dandelion", "cornflower", "lily", "rose",
               "bush", "vine", "sugar_cane", "bamboo", "cactus", "mushroom", "kelp", "seagrass", "dripleaf",
               "azalea", "moss_carpet", "pink_petals", "leaf_litter", "firefly")
FLUIDS = ("minecraft:water", "minecraft:lava", "minecraft:bubble_column")


def kind(name):
    if name in (None, "minecraft:air", "minecraft:cave_air", "minecraft:void_air"):
        return "air"
    if name in FLUIDS:
        return "fluid"
    if name == "minecraft:snow" or any(w in name for w in PLANT_WORDS) and "mushroom_block" not in name:
        return "plant"
    return "solid"


def region_dir():
    path = os.environ.get("WORLD_REGION_DIR")
    if path:
        return path
    found = glob.glob("/data/*/dimensions/minecraft/overworld/region")
    if not found:
        raise web.HTTPServiceUnavailable(text="world region folder not found")
    return found[0]


def ints(request, *names):
    try:
        return [int(request.query[n]) for n in names]
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text=f"need integer params: {', '.join(names)}")


async def surface(request):
    x1, z1, x2, z2, ymin, ymax = ints(request, "x1", "z1", "x2", "z2", "ymin", "ymax")
    x1, x2, z1, z2 = min(x1, x2), max(x1, x2), min(z1, z2), max(z1, z2)
    if (x2 - x1 + 1) * (z2 - z1 + 1) > MAX_COLUMNS:
        raise web.HTTPBadRequest(text="area too big")
    world = World(region_dir())
    rows = []
    for z in range(z1, z2 + 1):
        row = []
        for x in range(x1, x2 + 1):
            top = ground = None
            missing = False
            for y in range(ymax, ymin - 1, -1):
                name = world.block(x, y, z)
                if name is None:
                    missing = True              # чанк ещё не сгенерирован
                    break
                k = kind(name)
                if k == "air":
                    continue
                if top is None:
                    top = (y, name)
                if k in ("solid", "fluid"):
                    ground = (y, name)
                    break
            row.append({"ground": ground and ground[0], "ground_block": ground and ground[1],
                        "top": top and top[0], "top_block": top and top[1], "missing": missing})
        rows.append(row)
    return web.json_response({"x1": x1, "z1": z1, "x2": x2, "z2": z2, "rows": rows})


async def blocks(request):
    x1, y1, z1, x2, y2, z2 = ints(request, "x1", "y1", "z1", "x2", "y2", "z2")
    x1, x2, y1, y2, z1, z2 = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2), min(z1, z2), max(z1, z2)
    if (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1) > MAX_VOLUME:
        raise web.HTTPBadRequest(text="volume too big")
    world = World(region_dir())
    layers = {}
    for y in range(y1, y2 + 1):
        counts = {}
        for z in range(z1, z2 + 1):
            for x in range(x1, x2 + 1):
                name = world.block(x, y, z) or "unknown"
                counts[name] = counts.get(name, 0) + 1
        layers[y] = counts
    return web.json_response({"layers": layers})


MAX_CELLS = 64 * 64 * 64


async def volume(request):
    """Все блоки объёма: палитра + индексы (по 2 байта, порядок y, z, x) в base64."""
    x1, y1, z1, x2, y2, z2 = ints(request, "x1", "y1", "z1", "x2", "y2", "z2")
    x1, x2, y1, y2, z1, z2 = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2), min(z1, z2), max(z1, z2)
    if (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1) > MAX_CELLS:
        raise web.HTTPBadRequest(text="volume too big")
    world = World(region_dir())
    palette, index, out = [], {}, bytearray()
    for y in range(y1, y2 + 1):
        for z in range(z1, z2 + 1):
            for x in range(x1, x2 + 1):
                name = world.block(x, y, z) or "unknown"
                if name not in index:
                    index[name] = len(palette)
                    palette.append(name)
                out += index[name].to_bytes(2, "little")
    return web.json_response({"x1": x1, "y1": y1, "z1": z1, "x2": x2, "y2": y2, "z2": z2,
                              "palette": palette, "data": base64.b64encode(bytes(out)).decode()})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = web.Application()
    app.router.add_get("/surface", surface)
    app.router.add_get("/blocks", blocks)
    app.router.add_get("/volume", volume)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    web.run_app(app, port=int(os.environ.get("WORLD_PORT", "8090")), access_log=None)


if __name__ == "__main__":
    main()
