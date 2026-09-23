"""Процесс-исполнитель: читает код из stdin, печатает JSON с результатом."""
import json
import math
import os
import random
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
except (ImportError, ValueError, OSError):
    pass  # на macOS лимит памяти может не поддерживаться — остаётся таймаут

from mcai.build_api import Builder, BuildError

SAFE_BUILTINS = {name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
                 for name in ("abs", "min", "max", "range", "len", "int", "float", "round", "list",
                              "dict", "tuple", "set", "enumerate", "zip", "str", "bool", "sorted",
                              "reversed", "sum", "any", "all", "isinstance", "print", "Exception",
                              "ValueError")}
ALLOWED_MODULES = {"math": math, "random": random}


def safe_import(name, *args, **kwargs):
    if name in ALLOWED_MODULES:
        return ALLOWED_MODULES[name]
    raise ImportError(f"можно импортировать только math и random, а не {name}")


def main():
    code = sys.stdin.read()
    builder = Builder()
    env = {"__builtins__": {**SAFE_BUILTINS, "__import__": safe_import},
           "math": math, "random": random, **builder.functions()}
    error = None
    try:
        sys.stdout = sys.stderr  # print() из скрипта не должен ломать JSON
        exec(compile(code, "script", "exec"), env)
    except BuildError as e:
        error = f"{_line()}{e}"
    except Exception as e:
        error = f"{_line()}{type(e).__name__}: {e}"
    finally:
        sys.stdout = sys.__stdout__
    print(json.dumps({"ops": builder.ops if not error else [], "messages": builder.messages,
                      "blocks": builder.blocks, "error": error}, ensure_ascii=False))


def _line():
    for frame in reversed(traceback.extract_tb(sys.exc_info()[2])):
        if frame.filename == "script":
            return f"Строка {frame.lineno}: "
    return ""


if __name__ == "__main__":
    main()
