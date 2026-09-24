"""Запрос к модели через LiteLLM (OpenAI-совместимый API)."""
import json
import os
import re

import httpx

API_DOC = """Available functions (already defined, do not import them):
  block(x, y, z, material)                          # one block
  fill(x1, y1, z1, x2, y2, z2, material, hollow=False)  # box; hollow=True -> only the shell
  walls(x1, y1, z1, x2, y2, z2, material)           # 4 walls, no floor/roof
  sphere(cx, cy, cz, radius, material, hollow=False)
  cylinder(cx, y, cz, radius, height, material, hollow=False)  # vertical, base at y
  pyramid(cx, y, cz, size, material, hollow=False)  # size = half of base width
  line(x1, y1, z1, x2, y2, z2, material)
  say(text)                                         # chat message
Modules `math` and `random` are available. Nothing else can be imported. No files, no network.
Use "air" to carve doors, windows and interiors.
Materials are block ids that are the same in Java and Bedrock, e.g.: stone, cobblestone, stone_bricks, oak_planks, spruce_planks,
oak_log, glass, glass_pane, white_wool, red_wool, sandstone, quartz_block, gold_block, diamond_block,
bricks, water, lava, torch, oak_leaves, grass_block, dirt, sand, air.
"""

STYLE = """The script will be read by a 12-year-old who is learning to program:
- use clear variable names (height, width, radius...) at the top so they are easy to change;
- use loops and simple math where it makes sense;
- write short comments in Russian explaining each part;
- end with say("...") in Russian describing what was built.
"""

SYSTEM_PROMPT = f"""You write Python scripts that build structures in Minecraft (Java or Bedrock/Education edition).
Reply with ONE ```python code block and nothing else.

{API_DOC}
Coordinates are relative: (0, 0, 0) is at the player's feet level, a few blocks in front of the player.
y goes up, y = -1 is the ground, +z goes away from the player.
Build in the area x from -30 to 30, z from 0 to 40, y from -1 to 60. Keep it under 50000 blocks.

{STYLE}"""


class LLMError(Exception):
    pass


def missing_settings():
    """Какие обязательные переменные окружения не заданы."""
    return [name for name in ("LLM_BASE_URL", "LLM_MODEL") if not os.environ.get(name)]


def settings():
    missing = missing_settings()
    if missing:
        raise LLMError(f"Модель не настроена: не заданы {', '.join(missing)}")
    return {
        "base_url": os.environ["LLM_BASE_URL"].rstrip("/"),
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "model": os.environ["LLM_MODEL"],
        "timeout": float(os.environ.get("LLM_TIMEOUT", "120")),
    }


async def generate_code(request, previous_code=None, feedback=None, system=SYSTEM_PROMPT):
    """Попросить модель написать скрипт.
    previous_code + feedback — попросить исправить ошибку или изменить прошлый скрипт."""
    s = settings()
    messages = [{"role": "system", "content": system}]
    if previous_code and feedback:
        messages += [
            {"role": "user", "content": request + " /no_think"},
            {"role": "assistant", "content": f"```python\n{previous_code}\n```"},
            {"role": "user", "content": feedback + " /no_think"},
        ]
    else:
        messages.append({"role": "user", "content": request + " /no_think"})

    headers = {"Authorization": f"Bearer {s['api_key']}"} if s["api_key"] else {}
    async with httpx.AsyncClient(timeout=s["timeout"]) as client:
        try:
            resp = await client.post(f"{s['base_url']}/v1/chat/completions", headers=headers, json={
                "model": s["model"],
                "messages": messages,
                "temperature": 0.5,
                "max_tokens": 4000,
            })
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"Модель недоступна: {e}") from e
    content = resp.json()["choices"][0]["message"].get("content") or ""
    return extract_code(content)


async def run_agent(system, request, tools, call_tool, max_steps=8, on_tool=None):
    """Модель с инструментами: смотрит на мир через call_tool(имя, аргументы) -> текст,
    а в конце присылает скрипт. Возвращает код."""
    s = settings()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": request + " /no_think"}]
    headers = {"Authorization": f"Bearer {s['api_key']}"} if s["api_key"] else {}
    async with httpx.AsyncClient(timeout=s["timeout"]) as client:
        for step in range(max_steps):
            last = step == max_steps - 1
            if last:
                messages.append({"role": "user", "content": "Enough looking around. Write the final script now."})
            body = {"model": s["model"], "messages": messages, "temperature": 0.4, "max_tokens": 4000}
            if not last:
                body["tools"] = tools
            try:
                resp = await client.post(f"{s['base_url']}/v1/chat/completions", headers=headers, json=body)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise LLMError(f"Модель недоступна: {e}") from e
            msg = resp.json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return extract_code(msg.get("content") or "")
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                name, args = call["function"]["name"], {}
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    result = await call_tool(name, args)
                except Exception as e:     # ошибку показываем модели — пусть поправит вызов
                    result = f"Error: {e}"
                if on_tool:
                    await on_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
    raise LLMError("Модель так и не прислала скрипт")


def extract_code(text):
    """Вырезать <think>...</think> и достать код из ```python ... ```."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    if "(" in text:
        return text.strip() + "\n"
    raise LLMError("Модель не прислала код")
