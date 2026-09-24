"""Админка сервера Java: игроки (кто ждёт одобрения, кто одобрен, приём вкл/выкл) и постройки.

Страница открывается по https://promptcraft-admin.antfarm.dev (порт 8080 моста), пароль — переменная ADMIN_TOKEN.
"""
import hmac
import logging

from aiohttp import web

from mcai.builds import BuildError, summary

log = logging.getLogger("admin")

COOKIE = "promptcraft_admin"


def make_app(game, token):
    app = web.Application()
    access = game.access

    def full_state():
        return access.state() | {"builds": [summary(b) for b in game.builds.listing()]}

    def authorized(request):
        return bool(token) and hmac.compare_digest(request.cookies.get(COOKIE, ""), token)

    async def index(request):
        if not authorized(request):
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        return web.Response(text=ADMIN_PAGE, content_type="text/html")

    async def login(request):
        form = await request.post()
        if token and hmac.compare_digest(str(form.get("password", "")), token):
            resp = web.HTTPFound("/")
            https = request.secure or request.headers.get("X-Forwarded-Proto") == "https"
            resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", secure=https,
                            max_age=90 * 24 * 3600)
            return resp
        return web.Response(text=LOGIN_PAGE.replace("<!--error-->", "<p class=err>Неверный пароль</p>"),
                            content_type="text/html", status=401)

    def api(handler):
        async def wrapped(request):
            # свой заголовок — защита от отправки формы с чужого сайта
            if not authorized(request) or (request.method == "POST" and request.headers.get("X-Admin") != "1"):
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                return web.json_response(await handler(request))
            except Exception as e:
                log.exception("admin action failed")
                return web.json_response({"error": str(e)}, status=500)
        return wrapped

    async def state(request):
        return full_state()

    async def action(request):
        body = await request.json()
        name = access.resolve(body.get("name", "")) or body.get("name", "")
        kind = request.match_info["action"]
        if kind in ("build-rename", "build-delete"):
            b = game.builds.builds.get(int(body.get("id", 0)))
            if not b:
                return {"message": "Постройка не найдена", "state": full_state()}
            if kind == "build-rename":
                try:
                    message = f"Теперь #{b['id']} называется {game.builds.rename(b['id'], body.get('name', ''))['name']}"
                except BuildError as e:
                    message = str(e)
            else:
                await game.remove_build(b)
                message = f"Удалена #{b['id']} {b['name']}"
        elif kind == "approve":
            message = await access.approve(name)
        elif kind == "remove":
            message = await access.remove(name)
        elif kind == "dismiss":
            message = access.dismiss(name)
        elif kind == "open":
            message = await access.set_open(body.get("open"))
        else:
            raise web.HTTPNotFound()
        log.info("admin: %s %s -> %s", kind, name or body.get("id"), message)
        return {"message": message, "state": full_state()}

    app.router.add_get("/", index)
    app.router.add_post("/login", login)
    app.router.add_get("/api/state", api(state))
    app.router.add_post("/api/{action}", api(action))
    return app


async def start(game, token, port):
    if not token:
        log.warning("ADMIN_TOKEN not set: admin page is disabled")
        return
    runner = web.AppRunner(make_app(game, token), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("admin page on :%d", port)


STYLE = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#6b7385; --line:#e3e6ec;
        --accent:#2f7d4f; --accent-text:#fff; --danger:#b3261e; --chip:#eef1f5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14171c; --card:#1d2128; --text:#e7eaf0; --muted:#9aa3b2; --line:#2c323c;
          --accent:#4caf7a; --accent-text:#0d1a12; --danger:#ef7a70; --chip:#262b33; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.45 -apple-system, system-ui, sans-serif }
main { max-width:720px; margin:0 auto; padding:24px 16px 48px }
h1 { font-size:22px; margin:0 0 16px } h2 { font-size:14px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:28px 0 8px }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:4px 16px }
.row { display:flex; align-items:center; gap:10px; padding:10px 0; border-top:1px solid var(--line) }
.row:first-child { border-top:0 } .name { flex:1; font-weight:600; word-break:break-all }
.chip { font-size:12px; font-weight:500; background:var(--chip); color:var(--muted); border-radius:99px;
  padding:2px 8px; margin-left:6px } .dot { color:var(--accent) }
button { font:inherit; font-size:14px; border-radius:8px; border:1px solid var(--line); background:var(--card);
  color:var(--text); padding:6px 12px; cursor:pointer } button.go { background:var(--accent);
  color:var(--accent-text); border-color:var(--accent) } button.bad { color:var(--danger) }
.empty { color:var(--muted); padding:12px 0 } .toggle { display:flex; justify-content:space-between;
  align-items:center; gap:12px; padding:14px 0 } .hint { color:var(--muted); font-size:13px }
#toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:var(--text);
  color:var(--bg); padding:8px 14px; border-radius:8px; opacity:0; transition:opacity .2s }
input { font:inherit; padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:var(--card);
  color:var(--text); width:100% } .err { color:var(--danger) }
"""

LOGIN_PAGE = f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Promptcraft admin</title>
<style>{STYLE}</style></head><body><main style="max-width:360px">
<h1>Promptcraft</h1><form method=post action=/login class=card style="padding:16px">
<p style="margin-top:0">Пароль админки</p><input type=password name=password autofocus>
<!--error--><p><button class=go type=submit>Войти</button></p></form></main></body></html>"""

ADMIN_PAGE = f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Promptcraft admin</title>
<style>{STYLE}</style></head><body><main>
<h1>Promptcraft — игроки</h1>
<div class=card><div class=toggle><div><b>Приём новых игроков</b>
<div class=hint id=openHint></div></div><button id=openBtn></button></div></div>
<h2>Ждут одобрения</h2><div class=card id=pending></div>
<h2>Одобрены</h2><div class=card id=approved></div>
<h2>Постройки</h2><div class=card id=builds></div>
<div id=toast></div>
<script>
let S = null;
const $ = id => document.getElementById(id);
const esc = s => s.replace(/[&<>"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c]);
function toast(t) {{ $("toast").textContent = t; $("toast").style.opacity = 1;
  clearTimeout(toast.t); toast.t = setTimeout(() => $("toast").style.opacity = 0, 2500); }}
async function call(action, body) {{
  const r = await fetch("/api/" + action, {{method:"POST", headers:{{"Content-Type":"application/json","X-Admin":"1"}},
    body: JSON.stringify(body)}});
  const d = await r.json(); if (d.error) {{ toast("Ошибка: " + d.error); return; }}
  toast(d.message); render(d.state); }}
function row(name, chips, buttons) {{
  return `<div class=row><div class=name>${{esc(name)}}${{chips}}</div>${{buttons}}</div>`; }}
function btn(label, action, name, cls) {{
  return `<button class="${{cls||""}}" onclick='call("${{action}}", {{name: ${{JSON.stringify(name)}}}})'>${{label}}</button>`; }}
function render(s) {{
  S = s; const on = new Set(s.online);
  $("openBtn").textContent = s.open ? "Закрыть" : "Открыть";
  $("openBtn").className = s.open ? "" : "go";
  $("openHint").textContent = s.open
    ? "Открыт: новички заходят гостями (без строительства) и ждут одобрения"
    : "Закрыт: заходят только одобренные";
  $("pending").innerHTML = s.pending.length ? s.pending.map(p => row(p.name,
      (p.online ? "<span class=chip><span class=dot>●</span> онлайн</span>" : "<span class=chip>не в игре</span>") +
      (p.approved ? "<span class=chip>одобрится при входе</span>" : ""),
      (p.approved ? "" : btn("Одобрить", "approve", p.name, "go")) + " " + btn("Отклонить", "dismiss", p.name))).join("")
    : "<div class=empty>Никто не ждёт</div>";
  $("approved").innerHTML = s.approved.length ? s.approved.map(n => row(n,
      (on.has(n) ? "<span class=chip><span class=dot>●</span> онлайн</span>" : "") +
      (s.admins.includes(n.toLowerCase()) ? "<span class=chip>админ</span>" : ""),
      btn("Удалить", "remove", n, "bad"))).join("")
    : "<div class=empty>Пока никого</div>";
  renderBuilds(s.builds); }}
// строка постройки: обычная, «переименовать» (поле ввода) или «удалить?» (подтверждение)
let editing = null;   // {{id, mode: "rename" | "delete"}}
function renderBuilds(list) {{
  $("builds").innerHTML = list.length ? list.map(b => {{
    const date = new Date(b.updated * 1000).toLocaleString("ru", {{day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"}});
    const meta = `<div class=hint>${{esc(b.owner)}} · ${{b.blocks}} бл. · ${{date}}${{b.versions > 1 ? " · версий: " + b.versions : ""}}</div>
      <div class=hint title="${{esc(b.request)}}">«${{esc(b.request.length > 70 ? b.request.slice(0, 70) + "…" : b.request)}}»</div>`;
    let actions;
    if (editing && editing.id === b.id && editing.mode === "rename") {{
      actions = `<input id=newName value="${{esc(b.name)}}" style="width:140px">
        <button class=go onclick="doRename(${{b.id}})">OK</button> <button onclick="stopEdit()">Отмена</button>`;
    }} else if (editing && editing.id === b.id && editing.mode === "delete") {{
      actions = `<span class=hint>Удалить из мира?</span> <button class=bad onclick="doDelete(${{b.id}})">Да, удалить</button>
        <button onclick="stopEdit()">Нет</button>`;
    }} else {{
      actions = `<button onclick="startEdit(${{b.id}}, 'rename')">Имя</button>
        <button class=bad onclick="startEdit(${{b.id}}, 'delete')">Удалить</button>`;
    }}
    return `<div class=row><div class=name>#${{b.id}} ${{esc(b.name)}}${{meta}}</div><div>${{actions}}</div></div>`;
  }}).join("") : "<div class=empty>Построек пока нет</div>"; }}
function startEdit(id, mode) {{ editing = {{id, mode}}; renderBuilds(S.builds);
  if (mode === "rename") {{ const i = $("newName"); i.focus(); i.select();
    i.onkeydown = e => {{ if (e.key === "Enter") doRename(id); if (e.key === "Escape") stopEdit(); }}; }} }}
function stopEdit() {{ editing = null; renderBuilds(S.builds); }}
async function doRename(id) {{ const name = $("newName").value; editing = null; await call("build-rename", {{id, name}}); }}
async function doDelete(id) {{ editing = null; await call("build-delete", {{id}}); }}
$("openBtn").onclick = () => call("open", {{open: !S.open}});
async function refresh() {{ if (editing) return; const r = await fetch("/api/state");
  if (r.status === 401) {{ location.reload(); return; }} render(await r.json()); }}
refresh(); setInterval(refresh, 5000);
</script></main></body></html>"""
