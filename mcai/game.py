"""Общее для всех адаптеров: команды чата, постройки-проекты, превращение операций в команды игры.

Адаптер (мост к конкретной игре) наследует Session и умеет:
  command(line)            — выполнить команду, вернуть (успех, сообщение)
  say(text)                — написать в чат
  player_position(player)  — где стоит игрок: ((x, y, z), поворот головы в градусах)
  selector(player)         — как назвать игрока в команде (tp)

Команды в чате игры:
  ai <что построить>           — модель пишет скрипт и строит; постройка получает номер: #7
  ai+ [#7|имя] <что изменить>  — доработать постройку (без номера — свою последнюю)
  run <скрипт>                 — запустить свой скрипт scripts/<скрипт>.py
  builds [all]                 — таблица построек
  name #7 <новое имя>          — переименовать
  tp #7                        — перенестись к постройке
  delete #7                    — удалить (спросит подтверждение)
  undo                         — удалить свою последнюю постройку (тоже с подтверждением)
  help                         — подсказка
"""
import asyncio
import logging
import os
import re
import shutil
import time

from mcai import llm, sandbox
from mcai.builds import BuildError, BuildStore, line

log = logging.getLogger("game")

EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", EXAMPLES_DIR)
MAX_FILL_VOLUME = 32768     # ограничение команды /fill
FORWARD_OFFSET = 3          # на сколько блоков перед игроком начинать постройку
NAME_RE = re.compile(r"^[A-Za-z0-9_\-/]+$")
CONFIRM_SECONDS = 60
YES = {"да", "yes", "y", "д", "ага"}

HELP = [
    "ai <что построить> — например: ai замок с четырьмя башнями",
    "ai+ #7 <что изменить> — доработать постройку #7 (без номера — последнюю)",
    "run <скрипт> — запустить свой скрипт, например: run tower",
    "builds — список построек, name #7 <имя> — переименовать",
    "tp #7 — перенестись к постройке, delete #7 — удалить",
]


def builds_folder(adapter):
    """Постройки каждой игры отдельно: у Java-сервера и мира Education разные миры."""
    return os.path.join(SCRIPTS_DIR, ".builds", adapter)


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


def ops_to_commands(ops, origin, facing, material=None, rename=lambda m: m):
    ox, oy, oz = origin
    commands = []
    for op in ops:
        mat = rename(material or op[-1])
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


def origin_from(pos, yaw):
    """Точка постройки и направление по позиции и повороту игрока.
    Поворот в градусах: 0 юг, 90 запад, 180 север, 270 (или -90) восток — так в обеих версиях игры."""
    facing = round(yaw / 90) % 4
    fx, fz = rotate(0, FORWARD_OFFSET, facing)
    x, y, z = (int(v // 1) for v in pos)
    return (x + fx, y, z + fz), facing


# --- сессия с игрой ----------------------------------------------------------

class Session:
    # сколько команд отправлять в игру одновременно
    parallel = 50

    def __init__(self, store):
        self.builds = store
        self.busy = set()
        self.confirm = {}      # игрок -> (что удалить, до какого времени ждать ответа)

    async def command(self, line):
        raise NotImplementedError

    async def say(self, text):
        raise NotImplementedError

    async def player_position(self, player):
        raise NotImplementedError

    def selector(self, player):
        return player

    def block_name(self, material):
        """Имя блока для этой версии игры (переопределяется в адаптере)."""
        return material

    def is_admin(self, player):
        """Админ может менять и удалять чужие постройки. В своём мире (Education) — все."""
        return True

    async def run_commands(self, commands):
        slots = asyncio.Semaphore(self.parallel)

        async def one(c):
            async with slots:
                return await self.command(c)

        results = await asyncio.gather(*(one(c) for c in commands))
        failed = [(c, msg) for c, (ok, msg) in zip(commands, results) if not ok]
        for c, msg in failed[:3]:
            log.warning("command failed: %s -> %s", c, msg)
        return failed

    # --- обработка чата ------------------------------------------------------

    async def on_chat(self, player, text):
        text = text.strip()
        low = text.lower()
        word, _, rest = text.partition(" ")
        word, rest = word.lower(), rest.strip()
        if player in self.confirm and await self.answer_confirm(player, low):
            return
        if low in ("help", "помощь"):
            await self.say("\n".join(HELP))
        elif word == "undo":
            await self.ask_delete(player, self.builds.last(owner=player))
        elif word == "delete":
            await self.ask_delete(player, self._find_or_tell(rest) if rest else None, rest)
        elif word in ("builds", "постройки"):
            await self.list_builds(all_=rest.lower() == "all")
        elif word in ("name", "rename"):
            await self.rename(player, rest)
        elif word == "tp":
            await self.teleport(player, rest)
        elif word == "run":
            await self.run_script(player, rest)
        elif word == "ai+":
            await self.ai_modify(player, rest)
        elif word == "ai":
            await self.ai(player, rest)

    def _find_or_tell(self, ref):
        return self.builds.find(ref.split()[0]) if ref else None

    async def _get_editable(self, player, ref, action):
        b = self.builds.find(ref) if ref else self.builds.last(owner=player)
        if not b:
            await self.say(f"Не нашёл постройку {ref}. Список: builds" if ref else "У тебя пока нет построек")
            return None
        if b["owner"] != player and not self.is_admin(player):
            await self.say(f"#{b['id']} построил {b['owner']} — {action} может только он или админ")
            return None
        return b

    # --- новые постройки -----------------------------------------------------

    async def ai(self, player, request):
        if not request:
            await self.say("Напиши, что построить: ai домик с окнами")
            return
        async with self._one_at_a_time(player) as ok:
            if not ok:
                return
            await self.say(f"Думаю над: {request} ...")
            t = time.time()
            try:
                code, result = await self._generate(request)
            except llm.LLMError as e:
                await self.say(str(e))
                return
            script = save_ai_script(player, request, code)
            await self.say(f"Код готов за {time.time() - t:.0f} с, он лежит в файле {script}.py")
            await self.build_new(player, request, script, code, result)

    async def run_script(self, player, name):
        if not NAME_RE.match(name) or ".." in name:
            await self.say("Имя скрипта: латинские буквы, цифры, _ и -")
            return
        path = os.path.join(SCRIPTS_DIR, name + ".py")
        if not os.path.exists(path):
            await self.say(f"Нет скрипта {name}.py")
            return
        with open(path, encoding="utf-8") as f:
            code = f.read()
        result = await asyncio.to_thread(sandbox.run, code)
        await self.build_new(player, name, name, code, result)

    async def build_new(self, player, request, script, code, result):
        if result["error"]:
            await self.say(f"Ошибка в {script}.py: {result['error']}")
            return
        try:
            origin, facing = origin_from(*await self.player_position(player))
        except RuntimeError as e:
            await self.say(str(e))
            return
        failed, count = await self.place(result["ops"], origin, facing)
        b = self.builds.add(player, request, script, code, result["ops"], result["blocks"], origin, facing)
        for m in result["messages"]:
            await self.say(m)
        await self.say(f"Готово: #{b['id']} {b['name']}, {b['blocks']} блоков{self._failed_note(failed, count)}")
        await self.say(f"Доработать: ai+ #{b['id']} <что изменить>, переименовать: name #{b['id']} <имя>")

    # --- доработка -----------------------------------------------------------

    async def ai_modify(self, player, text):
        first, _, rest = text.partition(" ")
        target = self.builds.find(first) if first else None
        if target:
            ref, change = first, rest.strip()
        elif first.startswith("#"):
            await self.say(f"Не нашёл постройку {first}. Список: builds")
            return
        else:
            ref, change = None, text
        if not change:
            await self.say("Напиши, что изменить: ai+ #7 сделай выше")
            return
        b = await self._get_editable(player, ref, "менять")
        if not b:
            return
        async with self._one_at_a_time(player) as ok:
            if not ok:
                return
            await self.say(f"Меняю #{b['id']} {b['name']}: {change} ...")
            t = time.time()
            try:
                code, result = await self._generate(f"{b['request']}. Change: {change}",
                                                    previous=(b["request"], b["code"]), change=change)
            except llm.LLMError as e:
                await self.say(str(e))
                return
            if result["error"]:
                await self.say(f"Не получилось: {result['error']}")
                return
            script = save_ai_script(player, f"{b['request']}. Change: {change}", code)
            origin, facing = tuple(b["origin"]), b["facing"]
            await self.place(list(reversed(b["ops"])), origin, facing, material="air")
            failed, count = await self.place(result["ops"], origin, facing)
            b = self.builds.update(b["id"], code=code, script=script, ops=result["ops"], blocks=result["blocks"],
                                   request=f"{b['request']}. Change: {change}",
                                   history=b.get("history", []) + [change])
            for m in result["messages"]:
                await self.say(m)
            await self.say(f"#{b['id']} {b['name']} обновлена за {time.time() - t:.0f} с: "
                           f"{b['blocks']} блоков{self._failed_note(failed, count)}. Код: {script}.py")

    async def _generate(self, prompt, previous=None, change=None):
        """Модель пишет код; если он падает — один раз просим исправить."""
        if previous:
            code = await llm.generate_code(previous[0], previous[1], f"Change the script: {change}")
        else:
            code = await llm.generate_code(prompt)
        result = await asyncio.to_thread(sandbox.run, code)
        if result["error"]:
            log.info("AI code failed (%s), asking to fix", result["error"])
            code = await llm.generate_code(prompt, code, f"The script failed with: {result['error']}\nFix it.")
            result = await asyncio.to_thread(sandbox.run, code)
        return code, result

    # --- таблица, имя, телепорт, удаление ------------------------------------

    async def list_builds(self, all_=False):
        items = self.builds.listing(None if all_ else 10)
        if not items:
            await self.say("Построек пока нет. Попробуй: ai домик")
            return
        total = len(self.builds.builds)
        head = f"Постройки ({total}):" if all_ or total <= 10 else f"Последние 10 из {total} (все: builds all):"
        await self.say("\n".join([head] + [line(b) for b in items]))

    async def rename(self, player, text):
        ref, _, new = text.partition(" ")
        if not ref or not new.strip():
            await self.say("Напиши: name #7 новое-имя")
            return
        b = await self._get_editable(player, ref, "переименовать")
        if not b:
            return
        try:
            b = self.builds.rename(b["id"], new.strip())
        except BuildError as e:
            await self.say(str(e))
            return
        await self.say(f"Теперь #{b['id']} называется {b['name']}")

    async def teleport(self, player, ref):
        b = self.builds.find(ref) if ref else None
        if not b:
            await self.say("Напиши номер или имя: tp #7 (список: builds)")
            return
        # туда, где стоял автор, лицом к постройке
        (ox, oy, oz), facing = b["origin"], b["facing"]
        bx, bz = rotate(0, FORWARD_OFFSET, facing)
        ok, msg = await self.command(f"tp {self.selector(player)} {ox - bx + 0.5} {oy} {oz - bz + 0.5} "
                                     f"{facing * 90} 20")
        if not ok:
            await self.say(f"Не получилось перенести: {msg}")

    async def ask_delete(self, player, b, ref=""):
        if not b:
            await self.say(f"Не нашёл постройку {ref}. Список: builds" if ref else "Нечего удалять")
            return
        if b["owner"] != player and not self.is_admin(player):
            await self.say(f"#{b['id']} построил {b['owner']} — удалить может только он или админ")
            return
        self.confirm[player] = (b["id"], time.time() + CONFIRM_SECONDS)
        await self.say(f"{player}, удалить #{b['id']} {b['name']} ({b['blocks']} блоков)? "
                       f"Напиши: да (или что угодно другое — отмена)")

    async def answer_confirm(self, player, answer):
        """Ответ на «удалить?». True — сообщение было ответом."""
        build_id, until = self.confirm.pop(player)
        if time.time() > until:
            return False
        if answer not in YES:
            await self.say("Удаление отменено")
            return True
        b = self.builds.builds.get(build_id)
        if b:
            await self.remove_build(b)
            await self.say(f"Удалил #{b['id']} {b['name']}")
        return True

    async def remove_build(self, b):
        await self.place(list(reversed(b["ops"])), tuple(b["origin"]), b["facing"], material="air")
        self.builds.delete(b["id"])

    # --- общее ---------------------------------------------------------------

    async def place(self, ops, origin, facing, material=None):
        commands = ops_to_commands(ops, origin, facing, material=material, rename=self.block_name)
        return await self.run_commands(commands), len(commands)

    @staticmethod
    def _failed_note(failed, count):
        return f" (не получилось {len(failed)} из {count} команд)" if failed else ""

    def _one_at_a_time(self, player):
        session = self

        class Guard:
            async def __aenter__(self):
                if player in session.busy:
                    await session.say(f"{player}, подожди, я ещё строю прошлое")
                    return False
                session.busy.add(player)
                self.entered = True
                return True

            async def __aexit__(self, *exc):
                if getattr(self, "entered", False):
                    session.busy.discard(player)

        return Guard()


async def safe(coro):
    try:
        await coro
    except Exception:
        log.exception("chat handler failed")


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


def log_llm_settings():
    missing = llm.missing_settings()
    if missing:
        log.warning("%s not set: 'ai' command is disabled, 'run' still works", ", ".join(missing))
    else:
        log.info("LLM: %s, model %s", os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"])
