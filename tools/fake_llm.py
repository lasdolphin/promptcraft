"""Поддельный OpenAI-совместимый сервер для тестов: всегда отвечает скриптом домика.
Первый ответ специально с ошибкой — чтобы проверить автоисправление.

  python tools/fake_llm.py   # слушает :4999
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOUSE = (Path(__file__).parent.parent / "scripts" / "house.py").read_text(encoding="utf-8")
calls = 0


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global calls
        calls += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last = body["messages"][-1]["content"]
        code = "fill(0, 0, 0, 5, 5, 5, 'stone'\n" if calls == 1 else HOUSE
        text = f"<think>hmm</think>Вот код:\n```python\n{code}```"
        print(f"request #{calls}: {last[:80]!r}")
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


HTTPServer(("127.0.0.1", 4999), Handler).serve_forever()
