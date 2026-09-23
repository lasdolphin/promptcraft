"""Запуск скрипта в отдельном процессе с ограничениями.

Защита рассчитана на ошибки и шалости, а не на опытного взломщика:
- только белый список встроенных функций, импорт только math и random;
- запрещены имена с двойным подчёркиванием (через них обычно выбираются из песочниц);
- отдельный процесс с лимитом времени и памяти.
Запускайте мост в контейнере — это ещё один слой защиты.
"""
import ast
import json
import os
import subprocess
import sys

TIMEOUT_SECONDS = 10


def check_code(code):
    """Статическая проверка. Возвращает текст ошибки или None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Синтаксическая ошибка в строке {e.lineno}: {e.msg}"
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        if name and "__" in name:
            return f"Строка {node.lineno}: имена с '__' запрещены ({name})"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            for mod in modules:
                if mod not in ("math", "random"):
                    return f"Строка {node.lineno}: можно импортировать только math и random"
    return None


def run(code):
    """Выполнить скрипт. Возвращает dict: ops, messages, blocks, error."""
    error = check_code(code)
    if error:
        return {"ops": [], "messages": [], "blocks": 0, "error": error}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", os.path.join(_package_root(), "mcai", "_runner.py")],
            input=code, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"ops": [], "messages": [], "blocks": 0,
                "error": f"Скрипт работал дольше {TIMEOUT_SECONDS} секунд (может, бесконечный цикл?)"}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ops": [], "messages": [], "blocks": 0,
                "error": "Скрипт упал: " + (proc.stderr.strip().splitlines() or ["?"])[-1]}


def _package_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    # python -m mcai.sandbox scripts/tower.py — проверить скрипт без игры
    with open(sys.argv[1], encoding="utf-8") as f:
        result = run(f.read())
    print(f"Операций: {len(result['ops'])}, блоков: {result['blocks']}")
    for m in result["messages"]:
        print("say:", m)
    if result["error"]:
        print("Ошибка:", result["error"])
