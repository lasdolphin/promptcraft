"""Строительный API. Эти функции доступны в скриптах (и тем, что пишет модель).

Координаты считаются от точки перед игроком:
  x — вправо/влево (восток/запад), y — вверх, z — вперёд/назад (юг/север).
  (0, 0, 0) — блок на уровне ног игрока.

Результат работы скрипта — список операций:
  ["fill", x1, y1, z1, x2, y2, z2, material]
  ["block", x, y, z, material]
"""
import math
import re

MAX_BLOCKS = 100_000   # сколько блоков можно поставить за один запуск
MAX_OPS = 20_000       # сколько команд можно отправить в игру
MAX_COORD = 80         # как далеко от игрока можно строить
MIN_Y, MAX_Y = -40, 120

MATERIAL_RE = re.compile(r"^(minecraft:)?[a-z0-9_]+$")


class BuildError(Exception):
    pass


class Builder:
    def __init__(self):
        self.ops = []
        self.messages = []
        self.blocks = 0

    # --- проверки ---------------------------------------------------------

    def _material(self, material):
        if not isinstance(material, str) or not MATERIAL_RE.match(material):
            raise BuildError(f"Странный материал: {material!r}. Пример: 'stone', 'oak_planks'")
        return material.removeprefix("minecraft:")

    def _coord(self, v, axis):
        if not isinstance(v, (int, float)):
            raise BuildError(f"Координата {axis} должна быть числом, а не {v!r}")
        v = int(math.floor(v))
        lo, hi = (MIN_Y, MAX_Y) if axis == "y" else (-MAX_COORD, MAX_COORD)
        if not lo <= v <= hi:
            raise BuildError(f"Координата {axis}={v} слишком далеко (можно от {lo} до {hi})")
        return v

    def _add(self, op, volume):
        self.blocks += volume
        if self.blocks > MAX_BLOCKS:
            raise BuildError(f"Слишком большая постройка: больше {MAX_BLOCKS} блоков")
        if len(self.ops) >= MAX_OPS:
            raise BuildError(f"Слишком много команд: больше {MAX_OPS}")
        self.ops.append(op)

    # --- функции для скриптов ---------------------------------------------

    def block(self, x, y, z, material):
        """Поставить один блок."""
        m = self._material(material)
        self._add(["block", self._coord(x, "x"), self._coord(y, "y"), self._coord(z, "z"), m], 1)

    def fill(self, x1, y1, z1, x2, y2, z2, material, hollow=False):
        """Заполнить прямоугольный блок от (x1,y1,z1) до (x2,y2,z2).
        hollow=True — только стенки, пол и потолок (внутри ничего не меняется)."""
        m = self._material(material)
        x1, x2 = sorted((self._coord(x1, "x"), self._coord(x2, "x")))
        y1, y2 = sorted((self._coord(y1, "y"), self._coord(y2, "y")))
        z1, z2 = sorted((self._coord(z1, "z"), self._coord(z2, "z")))
        if hollow and x2 - x1 >= 2 and y2 - y1 >= 2 and z2 - z1 >= 2:
            for box in (
                (x1, y1, z1, x2, y1, z2), (x1, y2, z1, x2, y2, z2),              # пол, потолок
                (x1, y1 + 1, z1, x2, y2 - 1, z1), (x1, y1 + 1, z2, x2, y2 - 1, z2),  # стены по z
                (x1, y1 + 1, z1 + 1, x1, y2 - 1, z2 - 1), (x2, y1 + 1, z1 + 1, x2, y2 - 1, z2 - 1),
            ):
                self._fill_box(*box, m)
        else:
            self._fill_box(x1, y1, z1, x2, y2, z2, m)

    def _fill_box(self, x1, y1, z1, x2, y2, z2, m):
        volume = (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1)
        if volume == 1:
            self._add(["block", x1, y1, z1, m], 1)
        else:
            self._add(["fill", x1, y1, z1, x2, y2, z2, m], volume)

    def walls(self, x1, y1, z1, x2, y2, z2, material):
        """Четыре стены без пола и потолка — удобно для домов и башен."""
        m = self._material(material)
        self.fill(x1, y1, z1, x2, y2, z1, m)
        self.fill(x1, y1, z2, x2, y2, z2, m)
        self.fill(x1, y1, z1, x1, y2, z2, m)
        self.fill(x2, y1, z1, x2, y2, z2, m)

    def sphere(self, cx, cy, cz, radius, material, hollow=False):
        """Шар с центром (cx,cy,cz). hollow=True — пустой внутри."""
        m = self._material(material)
        r = float(radius)
        if r <= 0:
            raise BuildError("Радиус должен быть больше нуля")
        ri = int(math.ceil(r))
        for dy in range(-ri, ri + 1):
            for dz in range(-ri, ri + 1):
                cells = []
                for dx in range(-ri, ri + 1):
                    d = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if d <= r and (not hollow or d > r - 1.2):
                        cells.append(dx)
                self._rows(cells, cx, cy + dy, cz + dz, m)

    def cylinder(self, cx, y, cz, radius, height, material, hollow=False):
        """Цилиндр (круглая башня): центр основания (cx, y, cz), высота height."""
        m = self._material(material)
        r = float(radius)
        if r <= 0 or height <= 0:
            raise BuildError("Радиус и высота должны быть больше нуля")
        ri = int(math.ceil(r))
        for dz in range(-ri, ri + 1):
            cells = []
            for dx in range(-ri, ri + 1):
                d = math.sqrt(dx * dx + dz * dz)
                if d <= r and (not hollow or d > r - 1.2):
                    cells.append(dx)
            for start, end in _runs(cells):
                self.fill(cx + start, y, cz + dz, cx + end, y + int(height) - 1, cz + dz, m)

    def pyramid(self, cx, y, cz, size, material, hollow=False):
        """Пирамида: центр основания (cx, y, cz), size — половина ширины основания."""
        m = self._material(material)
        size = int(size)
        for level in range(size + 1):
            s = size - level
            if hollow and s > 0:
                self.walls(cx - s, y + level, cz - s, cx + s, y + level, cz + s, m)
            else:
                self.fill(cx - s, y + level, cz - s, cx + s, y + level, cz + s, m)

    def line(self, x1, y1, z1, x2, y2, z2, material):
        """Линия блоков из одной точки в другую (можно по диагонали)."""
        m = self._material(material)
        steps = int(max(abs(x2 - x1), abs(y2 - y1), abs(z2 - z1)))
        seen = set()
        for i in range(steps + 1):
            t = i / steps if steps else 0
            p = (round(x1 + (x2 - x1) * t), round(y1 + (y2 - y1) * t), round(z1 + (z2 - z1) * t))
            if p not in seen:
                seen.add(p)
                self.block(*p, m)

    def say(self, text):
        """Написать сообщение в чат игры."""
        self.messages.append(str(text)[:200])

    def _rows(self, xs, cx, y, z, m):
        for start, end in _runs(xs):
            self.fill(cx + start, y, z, cx + end, y, z, m)

    def functions(self):
        return {name: getattr(self, name) for name in
                ("block", "fill", "walls", "sphere", "cylinder", "pyramid", "line", "say")}


def _runs(xs):
    """[1,2,3,7,8] -> [(1,3), (7,8)] — чтобы ставить ряды одной командой fill."""
    runs = []
    for x in sorted(xs):
        if runs and x == runs[-1][1] + 1:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    return [tuple(r) for r in runs]
