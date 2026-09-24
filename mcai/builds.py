"""Постройки как проекты: у каждой номер (#7) и имя, к ней можно вернуться, доработать, удалить.

Каждая постройка — файл <папка>/<номер>.json: кто и что просил, код, где стоит (точка и направление)
и какие блоки поставлены (ops) — чтобы убрать её или перестроить на том же месте.
"""
import json
import os
import re
import threading
import time

NAME_RE = re.compile(r"^[\w\-]{1,32}$")   # одно слово: буквы (и русские), цифры, _ и -
STOP_WORDS = {"a", "an", "the", "with", "and", "of", "из", "с", "и", "на", "в", "для", "по", "мне", "построй", "сделай", "build", "make", "me"}


class BuildError(Exception):
    pass


def name_from_request(request):
    """'маленький домик с окнами' -> 'маленький-домик'"""
    words = [w for w in re.findall(r"[\w]+", request.lower()) if w not in STOP_WORDS and not w.isdigit()]
    return "-".join(words[:2])[:32] or "постройка"


class BuildStore:
    def __init__(self, folder):
        self.folder = folder
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        self.builds = {}
        for fname in os.listdir(folder):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(folder, fname), encoding="utf-8") as f:
                        b = json.load(f)
                    self.builds[b["id"]] = b
                except (OSError, ValueError, KeyError):
                    pass

    def _write(self, b):
        path = os.path.join(self.folder, f"{b['id']}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(b, f, ensure_ascii=False)
        os.replace(tmp, path)

    def _unique_name(self, name, skip_id=None):
        taken = {b["name"].lower() for b in self.builds.values() if b["id"] != skip_id}
        if name.lower() not in taken:
            return name
        n = 2
        while f"{name}-{n}".lower() in taken:
            n += 1
        return f"{name}-{n}"

    def add(self, owner, request, script, code, ops, blocks, origin, facing, parent=None):
        """parent — номер постройки, для которой это задача (расчистка, дорожка...)."""
        with self.lock:
            build_id = max(self.builds, default=0) + 1
            now = time.time()
            b = {"id": build_id, "name": self._unique_name(name_from_request(request)), "owner": owner,
                 "request": request, "script": script, "code": code, "ops": ops, "blocks": blocks,
                 "origin": list(origin), "facing": facing, "created": now, "updated": now,
                 "history": [request], "parent": parent}
            self.builds[build_id] = b
            self._write(b)
            return b

    def update(self, build_id, **fields):
        with self.lock:
            b = self.builds[build_id]
            b.update(fields, updated=time.time())
            self._write(b)
            return b

    def rename(self, build_id, name):
        name = name.strip().lstrip("#")
        if not NAME_RE.match(name) or name.isdigit():
            raise BuildError("Имя — одно слово: буквы, цифры, _ и - (не только цифры)")
        with self.lock:
            if any(b["name"].lower() == name.lower() and b["id"] != build_id for b in self.builds.values()):
                raise BuildError(f"Имя «{name}» уже занято")
            b = self.builds[build_id]
            b["name"], b["updated"] = name, time.time()
            self._write(b)
            return b

    def delete(self, build_id):
        with self.lock:
            b = self.builds.pop(build_id)
            try:
                os.remove(os.path.join(self.folder, f"{build_id}.json"))
            except OSError:
                pass
            return b

    def find(self, ref):
        """'#7', '7' или имя -> постройка (или None)."""
        ref = (ref or "").strip()
        if ref.lstrip("#").isdigit():
            return self.builds.get(int(ref.lstrip("#")))
        for b in self.builds.values():
            if b["name"].lower() == ref.lower():
                return b
        return None

    def last(self, owner=None):
        mine = [b for b in self.builds.values() if owner is None or b["owner"] == owner]
        return max(mine, key=lambda b: b["updated"], default=None)

    def listing(self, limit=None):
        items = sorted(self.builds.values(), key=lambda b: b["id"], reverse=True)
        return items[:limit] if limit else items


def summary(b):
    """Без ops — для таблиц и админки."""
    return {k: b[k] for k in ("id", "name", "owner", "request", "script", "blocks", "created", "updated")} | {
        "versions": len(b.get("history", [])), "parent": b.get("parent")}


def line(b):
    date = time.strftime("%d.%m %H:%M", time.localtime(b["updated"]))
    task = f" (задача для #{b['parent']})" if b.get("parent") else ""
    return f"#{b['id']} {b['name']}{task} — {b['owner']}, {b['blocks']} бл., {date}"
