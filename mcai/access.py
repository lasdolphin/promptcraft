"""Кто может играть на сервере Java: вход с одобрением.

Два режима (переключаются в админке или командой open/close):
- приём закрыт: на сервере включён whitelist, заходят только одобренные;
- приём открыт: whitelist выключен, новичок заходит в режиме adventure (ничего не сломает)
  и ждёт, пока админ его одобрит.

На сервере режим по умолчанию adventure (force-gamemode), одобренным мост при входе ставит creative.
Если мост не работает — все остаются в adventure.

Одобренные — это обычный whitelist сервера. Добавить в него Bedrock-игрока можно, только пока он
онлайн (иначе серверу неоткуда взять его UUID), поэтому одобрение офлайн-игрока откладывается
до его следующего входа.
"""
import asyncio
import json
import logging
import os
import re
import time

log = logging.getLogger("access")

JOIN_RE = re.compile(r"\]: (\.?[A-Za-z0-9_]{1,16}) joined the game$")
LEFT_RE = re.compile(r"\]: (\.?[A-Za-z0-9_]{1,16}) left the game$")
# Geyser пишет имя без точки: "Steve has disconnected from the Java server because of ...whitelisted..."
REJECTED_RE = re.compile(r"\[Geyser-\w+\] ([A-Za-z0-9_]{1,16}) has disconnected from the Java server "
                         r"because of .*not whitelisted")
SERVER_READY_RE = re.compile(r"\]: Done \(")
NAMES_RE = re.compile(r":\s*(.*)$")


def names_from(reply):
    """'There are 2 whitelisted player(s): a, .b' -> {'a', '.b'}"""
    m = NAMES_RE.search(reply or "")
    return {n.strip() for n in m.group(1).split(",") if n.strip()} if m else set()


class Access:
    def __init__(self, rcon, tell, admins, state_file):
        self.rcon = rcon            # async (line) -> ответ сервера
        self.tell = tell            # async (игрок, текст) -> написать игроку
        self.admins = {a.lower() for a in admins}
        self.state_file = state_file
        self.open = False           # приём новых игроков
        self.approved = set()       # whitelist сервера
        self.online = set()
        self.pending = {}           # ник -> {"since": время, "online": bool}
        self.approve_on_join = set()
        self._load()

    # --- состояние на диске ------------------------------------------------

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            self.open = bool(data.get("open", False))
            self.approve_on_join = set(data.get("approve_on_join", []))
        except (OSError, ValueError):
            pass

    def _save(self):
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({"open": self.open, "approve_on_join": sorted(self.approve_on_join)}, f)

    # --- сверка с сервером -------------------------------------------------

    async def sync(self):
        """Применить режим приёма и перечитать списки (при старте моста и после перезапуска сервера)."""
        await self.rcon("whitelist off" if self.open else "whitelist on")
        self.approved = names_from(await self.rcon("whitelist list"))
        self.online = names_from(await self.rcon("list"))
        for name in self.online:
            await self._grant(name) if self.is_approved(name) else await self._hold(name)
        log.info("access synced: open=%s approved=%d online=%d", self.open, len(self.approved), len(self.online))

    def is_approved(self, name):
        return name.lower() in {a.lower() for a in self.approved}

    def is_admin(self, name):
        return name.lower() in self.admins and self.is_approved(name)

    def resolve(self, name):
        """Найти ник среди известных игроков: 'trivialwig39433' -> '.TrivialWig39433'."""
        want = name.strip().lstrip(".").lower()
        for known in list(self.online) + list(self.pending) + sorted(self.approved):
            if known.lstrip(".").lower() == want:
                return known
        return None

    # --- события из лога сервера -------------------------------------------

    async def on_log_line(self, line):
        if m := JOIN_RE.search(line):
            await self.on_join(m.group(1))
        elif m := LEFT_RE.search(line):
            self.online.discard(m.group(1))
            if m.group(1) in self.pending:
                self.pending[m.group(1)]["online"] = False
        elif m := REJECTED_RE.search(line):
            name = "." + m.group(1)
            if not self.is_approved(name):
                self.pending.setdefault(name, {"since": time.time()})["online"] = False
                await self._notify_admins(f"{name} пытался зайти, но приём закрыт")
        elif SERVER_READY_RE.search(line):
            # RCON доступен не сразу: под ещё не Ready, у сервиса нет адреса — пробуем в фоне
            self._retry_task = asyncio.create_task(self.sync_with_retry())

    async def sync_with_retry(self, attempts=24, delay=5):
        for attempt in range(attempts):
            try:
                await self.sync()
                return
            except Exception as e:
                if attempt == attempts - 1:
                    log.warning("access sync failed: %s", e)
                await asyncio.sleep(delay)

    async def on_join(self, name):
        self.online.add(name)
        if not self.is_approved(name) and name in self.approve_on_join:
            await self.rcon(f"whitelist add {name}")
            self.approved.add(name)
            self.approve_on_join.discard(name)
            self._save()
        if self.is_approved(name):
            self.pending.pop(name, None)
            await self._grant(name)
        else:
            self.pending.setdefault(name, {"since": time.time()})["online"] = True
            await self._hold(name)
            await self._notify_admins(f"{name} хочет играть. Напиши: allow {name}")

    async def _grant(self, name):
        await self.rcon(f"gamemode creative {name}")

    async def _hold(self, name):
        await self.rcon(f"gamemode adventure {name}")
        await self.tell(name, "Привет! Ты пока гость: строить нельзя. Подожди, пока админ тебя одобрит.")

    async def _notify_admins(self, text):
        for name in self.online:
            if self.is_admin(name):
                await self.tell(name, text)

    # --- действия админа ---------------------------------------------------

    async def approve(self, name):
        # Онлайн-игрока и Java-игрока (по нику через Mojang) сервер добавляет сразу
        reply = await self.rcon(f"whitelist add {name}")
        self.approved = names_from(await self.rcon("whitelist list"))
        if self.is_approved(name):
            self.pending.pop(name, None)
            self.approve_on_join.discard(name)
            self._save()
            if name in self.online:
                await self._grant(name)
                await self.tell(name, "Тебя одобрили, можно строить! Напиши help")
            return f"{name} одобрен"
        log.info("whitelist add %s deferred until join: %s", name, reply)
        self.approve_on_join.add(name)
        self._save()
        if name in self.pending:
            self.pending[name]["approved"] = True
        return f"{name} будет одобрен, когда зайдёт" + ("" if self.open else " (открой приём, чтобы он смог зайти)")

    async def remove(self, name):
        await self.rcon(f"whitelist remove {name}")
        self.approved = names_from(await self.rcon("whitelist list"))
        self.approve_on_join.discard(name)
        self.pending.pop(name, None)
        self._save()
        if name in self.online:
            await self.rcon(f"gamemode adventure {name}")
            await self.rcon(f"kick {name} Access removed")
        return f"{name} удалён"

    def dismiss(self, name):
        self.pending.pop(name, None)
        self.approve_on_join.discard(name)
        self._save()
        return f"Заявка {name} отклонена"

    async def set_open(self, value):
        self.open = bool(value)
        self._save()
        await self.rcon("whitelist off" if self.open else "whitelist on")
        return "Приём новых игроков открыт" if self.open else "Приём новых игроков закрыт"

    def state(self):
        return {
            "open": self.open,
            "online": sorted(self.online, key=str.lower),
            "approved": sorted(self.approved, key=str.lower),
            "pending": [{"name": n, **info, "approved": n in self.approve_on_join}
                        for n, info in sorted(self.pending.items(), key=lambda kv: kv[1]["since"])],
            "admins": sorted(self.admins),
        }
