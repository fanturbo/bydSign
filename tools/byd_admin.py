"""比亚迪小程序签到 · 轻量管理页（纯标准库，无额外依赖）。

安全设计：
  - HTTP Basic 鉴权，账号密码存 data/byd_admin.json，首次启动自动生成并打印
  - 只绑 127.0.0.1，要公网访问请走 nginx 反代 + HTTPS

页面功能：
  - 表格展示每个账号的签到状态（已签到/今日已签/失败-疑似过期标红/异常/未签到）
  - 过期账号标红并提示「请重新抓包获取 session_id」
  - 「全部签到」「单账号重签」同步执行，完成后弹窗提示结果
    （今日已签 duplicate / 签到成功 / 失败及原因），页面不做常驻轮询，
    手动刷新（F5）看最新状态
  - 页面上直接增删改账号（写入 data/byd_accounts.json，无需 ssh 编辑）
  - 弹窗里整条粘贴抓包 curl，一键解出 session_id 自动填表

用法:
  python tools/byd_admin.py            # 默认 127.0.0.1:8098
  python tools/byd_admin.py --port 8097
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import byd_sign_all as signer  # noqa: E402
import byd_app_sign  # noqa: E402
import byd_get_session  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_CONF = os.path.join(BASE_DIR, "data", "byd_admin.json")

RUN_LOCK = threading.Lock()      # 同一时刻只允许一轮签到在跑
RUNNING = {"flag": False}


# ---------------------------------------------------------------- 配置
def load_conf():
    if not os.path.exists(ADMIN_CONF):
        conf = {"user": "admin", "password": secrets.token_urlsafe(9), "port": 8098}
        os.makedirs(os.path.dirname(ADMIN_CONF), exist_ok=True)
        with open(ADMIN_CONF, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=2)
        os.chmod(ADMIN_CONF, 0o600)
        print("=" * 60)
        print("首次启动，已生成比亚迪签到管理台账号（请立刻记下）：")
        print("   用户名: %s" % conf["user"])
        print("   密  码: %s" % conf["password"])
        print("   配  置: %s" % ADMIN_CONF)
        print("=" * 60)
    with open(ADMIN_CONF, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 数据
def merged_view():
    """把「账号配置」和「签到状态」合并成页面用的列表。

    配置里有、状态里没有 → 未签到；配置里没有的旧状态直接丢弃。
    """
    accounts = signer.load_accounts()
    state = signer.load_state()
    entries = state.get("accounts") or {}
    rows = []
    for acc in accounts:
        st = entries.get(acc["name"]) or {}
        rows.append({
            "name": acc["name"],
            "note": acc.get("note") or "",
            "session_id": acc["session_id"],
            "status": st.get("status") or "none",
            "date": st.get("date") or "",
            "last_run": st.get("last_run") or "",
            "duration_days": st.get("duration_days"),
            "integral": st.get("integral"),
            "msg": st.get("msg") or "",
            "expired": bool(st.get("expired")),
        })
    return {"updated_at": state.get("updated_at"), "running": RUNNING["flag"],
            "accounts": rows}


def sign_now(name=None):
    """同步跑一轮签到（页面按钮用），返回 (逐账号结果, 错误提示)。

    同步执行：单账号几秒、多账号每号约 1 秒，nginx 反代超时 300s 足够。
    结果直接带回给前端弹窗提示（今日已签 / 成功 / 失败），不再依赖轮询。
    """
    with RUN_LOCK:
        if RUNNING["flag"]:
            return None, "上一轮签到还在进行中"
        RUNNING["flag"] = True
    try:
        state = signer.run_all(name)
        entries = state.get("accounts") or {}
        if name:
            results = [{"name": name, **entries[name]}] if name in entries else []
        else:
            results = [{"name": a["name"], **entries.get(a["name"], {})}
                       for a in signer.load_accounts()]
        return results, None
    except SystemExit as exc:
        return None, str(exc)
    finally:
        with RUN_LOCK:
            RUNNING["flag"] = False


# ---------------------------------------------------------------- 账号管理
# 写配置文件的锁：增删改都是「读-改-写」，并发下不加锁会互相覆盖。
CONF_LOCK = threading.Lock()


def upsert_account(name: str, session_id: str, note: str,
                   old_name: str = None) -> Dict[str, Any]:
    """新增或更新一个账号。返回 {"ok": bool, "error": str}。

    old_name 为空 = 新增；非空 = 把 old_name 那条改成新值（改名也走这里）。
    """
    name = (name or "").strip()
    session_id = (session_id or "").strip()
    note = (note or "").strip()
    if not name:
        return {"ok": False, "error": "账号名不能为空"}
    if not session_id:
        return {"ok": False, "error": "session_id 不能为空"}

    with CONF_LOCK:
        accounts = signer.load_accounts_raw()
        # 重名检查：新增时不能撞已有名；改名时不能撞到别的账号
        for a in accounts:
            if str(a.get("name") or "").strip() == name and name != (old_name or ""):
                return {"ok": False, "error": f"已存在名为「{name}」的账号"}

        if old_name:
            idx = next((i for i, a in enumerate(accounts)
                        if str(a.get("name") or "").strip() == old_name), None)
            if idx is None:
                return {"ok": False, "error": f"找不到名为「{old_name}」的账号"}
            accounts[idx] = {"name": name, "session_id": session_id, "note": note}
            # 改名后把旧名的签到状态搬到新名下，页面历史不丢
            _migrate_state(old_name, name)
        else:
            accounts.append({"name": name, "session_id": session_id, "note": note})
        signer.save_accounts(accounts)
    return {"ok": True}


def delete_account(name: str) -> Dict[str, Any]:
    """删除一个账号（连同其签到状态）。"""
    name = (name or "").strip()
    with CONF_LOCK:
        accounts = signer.load_accounts_raw()
        kept = [a for a in accounts if str(a.get("name") or "").strip() != name]
        if len(kept) == len(accounts):
            return {"ok": False, "error": f"找不到名为「{name}」的账号"}
        signer.save_accounts(kept)
    # 状态文件里那条也清掉，避免残留
    state = signer.load_state()
    if name in (state.get("accounts") or {}):
        del state["accounts"][name]
        signer.save_state(state)
    return {"ok": True}


def _migrate_state(old_name: str, new_name: str) -> None:
    state = signer.load_state()
    entries = state.get("accounts") or {}
    if old_name in entries:
        entries[new_name] = entries.pop(old_name)
        signer.save_state(state)


def parse_session_id(text: str) -> str:
    """从整条抓包 curl 或裸密文 body 里解出 session_id。失败抛 ValueError。

    复用 tools/byd_get_session.py 的抠包 + tools/byd_app_sign.py 的 AES 解密。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("请先粘贴 curl 或密文 body")
    try:
        body = byd_get_session.extract_body_from_curl(text)
    except SystemExit:
        body = text  # 没抠到 -d 参数，就当裸密文 body 处理
    candidate = body.strip()
    if candidate.startswith('"') and candidate.endswith('"'):
        try:
            candidate = json.loads(candidate)
        except Exception:  # noqa: BLE001
            candidate = candidate.strip('"')
    try:
        obj = json.loads(byd_app_sign.aes_decrypt_b64(candidate))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"解密失败，确认粘的是发往 weixin90.bydauto.com.cn 的请求: {exc}") from exc
    sid = str(obj.get("session_id") or "").strip()
    if not sid:
        raise ValueError("解密成功，但明文里没有 session_id 字段")
    return sid


# ---------------------------------------------------------------- 页面
STATUS_UI = {
    "ok":        ("已签到",     "ok"),
    "duplicate": ("今日已签",   "dup"),
    "failed":    ("签到失败",   "bad"),
    "error":     ("异常",       "warn"),
    "none":      ("未签到",     "idle"),
}

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>比亚迪签到管理台</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--line:#e5e7eb;--txt:#1f2329;--sub:#8a9099;
--ok:#16a34a;--dup:#2563eb;--bad:#dc2626;--warn:#d97706;--idle:#9ca3af}
*{box-sizing:border-box}body{margin:0;font:14px/1.6 -apple-system,"PingFang SC",
"Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt)}
.wrap{max-width:960px;margin:24px auto;padding:0 16px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--sub);font-size:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:16px}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{border:0;border-radius:6px;padding:8px 16px;font-size:13px;cursor:pointer;
background:#2563eb;color:#fff}
button:hover{opacity:.9}button:disabled{background:#c3cbd6;cursor:not-allowed}
button.ghost{background:#fff;color:#2563eb;border:1px solid #2563eb;padding:4px 10px}
.stat{font-size:13px;color:var(--sub)}
.stat b{color:var(--txt)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--sub);font-weight:500;font-size:12px}
tr:last-child td{border-bottom:0}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;
color:#fff;white-space:nowrap}
.b-ok{background:var(--ok)}.b-dup{background:var(--dup)}.b-bad{background:var(--bad)}
.b-warn{background:var(--warn)}.b-idle{background:var(--idle)}
.hint{color:var(--bad);font-size:12px;margin-top:4px}
.msg{color:var(--sub);font-size:12px;margin-top:2px;word-break:break-all}
.sid{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--sub)}
.empty{color:var(--sub);padding:24px 0;text-align:center}
code{background:#eef0f3;border-radius:4px;padding:1px 6px;font-size:12px}
button.danger{background:#fff;color:var(--bad);border:1px solid var(--bad);padding:4px 10px}
.mask{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;
align-items:center;justify-content:center;z-index:10}
.mask.show{display:flex}
.dlg{background:#fff;border-radius:10px;padding:20px;width:420px;max-width:92vw}
.dlg h3{margin:0 0 12px;font-size:16px}
.dlg label{display:block;font-size:12px;color:var(--sub);margin:10px 0 4px}
.dlg input,.dlg textarea{width:100%;border:1px solid var(--line);border-radius:6px;
padding:8px 10px;font-size:13px;font-family:inherit}
.dlg textarea{font-family:ui-monospace,Menlo,monospace;font-size:12px;resize:vertical;
word-break:break-all}
.dlg .btns{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
.dlg button.cancel{background:#fff;color:var(--txt);border:1px solid var(--line)}
</style></head><body><div class="wrap">
<h1>比亚迪小程序 · 签到管理台</h1>
<div class="sub">账号在页面上直接增删改，保存即写入 <code>data/byd_accounts.json</code></div>
<div class="card bar">
  <button id="btnAll">全部签到</button>
  <button id="btnAdd" style="background:#16a34a">添加账号</button>
  <span class="stat" id="stat"></span>
  <span class="stat" style="margin-left:auto" id="upd"></span>
</div>
<div class="card"><table>
<thead><tr><th>账号</th><th>状态</th><th>连续签到</th><th>积分</th>
<th>最近执行</th><th>操作</th></tr></thead>
<tbody id="rows"></tbody></table>
<div class="empty" id="empty" style="display:none">
还没有账号，点上方「添加账号」开始</div>
</div>
</div>
<div class="mask" id="mask"><div class="dlg">
  <h3 id="dlgTitle">添加账号</h3>
  <label>账号名（唯一，用于展示和定时签到）</label>
  <input id="fName" placeholder="例如：我的比亚迪" maxlength="40">
  <label>抓包 curl（可选）：整条粘贴 → 点「解析填充」，自动解出 session_id</label>
  <textarea id="fCurl" rows="3" placeholder="curl -X POST 'https://weixin90.bydauto.com.cn?...' -H ... -d '&quot;密文&quot;'"></textarea>
  <div class="btns" style="justify-content:flex-start;margin-top:8px">
    <button class="ghost" onclick="parseCurl()">解析填充</button>
    <span id="parseTip" style="font-size:12px;color:var(--sub);align-self:center"></span>
  </div>
  <label>session_id（可由上面解析自动填充，或手动粘贴）</label>
  <input id="fSid" placeholder="32位大写十六进制，如 A1B2C3D4...">
  <label>备注（可选）</label>
  <input id="fNote" placeholder="例如：白色汉EV" maxlength="60">
  <div class="btns">
    <button class="cancel" onclick="closeDlg()">取消</button>
    <button onclick="saveAcc()">保存</button>
  </div>
</div></div>
<script>
// 页面可能挂在 /byd/ 这种子路径下（nginx 反代），请求必须按 BASE 拼，
// 写死 /api/... 会打到主站的 location /api/ 上去（见 DEPLOY.md §10.3）。
const BASE=location.pathname.replace(/[^/]*$/,'');
const S={ok:["已签到","ok"],duplicate:["今日已签","dup"],failed:["签到失败","bad"],
error:["异常","warn"],none:["未签到","idle"]};
function esc(s){return String(s??"").replace(/[&<>"']/g,
c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function sid(s){return s.length>12?s.slice(0,4)+"…"+s.slice(-4):s}
// 当前账号列表缓存（编辑弹窗回填用）
let ACCS=[];
async function refresh(){
  const r=await fetch(BASE+"api/status");const d=await r.json();
  const rows=document.getElementById("rows"),empty=document.getElementById("empty");
  const list=ACCS=d.accounts||[];
  empty.style.display=list.length?"none":"block";
  let ok=0,bad=0;
  rows.innerHTML=list.map((a,i)=>{
    const[txt,cls]=S[a.status]||S.none;
    if(a.status==="ok"||a.status==="duplicate")ok++;
    if(a.expired)bad++;
    const hint=a.expired?
      '<div class="hint">⚠ session_id 可能已过期，点「编辑」更新新的 session_id</div>':"";
    const msg=a.msg?'<div class="msg">'+esc(a.msg)+"</div>":"";
    const note=a.note?'<div class="msg">'+esc(a.note)+"</div>":"";
    return "<tr><td><b>"+esc(a.name)+"</b>"+note+
      '<div class="sid">'+esc(sid(a.session_id))+"</div></td>"+
      '<td><span class="badge b-'+cls+'">'+txt+"</span>"+hint+"</td>"+
      "<td>"+(a.duration_days??"-")+" 天</td><td>"+(a.integral??"-")+"</td>"+
      "<td>"+esc(a.last_run||"-")+(a.date?'<div class="msg">'+esc(a.date)+"</div>":"")+
      "</td><td style='white-space:nowrap'>"+
      "<button class='ghost' onclick='signIdx("+i+")'>重签</button> "+
      "<button class='ghost' onclick='editIdx("+i+")'>编辑</button> "+
      "<button class='danger' onclick='delIdx("+i+")'>删除</button></td></tr>";
  }).join("");
  document.getElementById("stat").innerHTML=
    "共 <b>"+list.length+"</b> 个账号 · 已签 <b>"+ok+"</b>"+
    (bad?" · <span style='color:var(--bad)'>疑似过期 <b>"+bad+"</b></span>":"");
  document.getElementById("upd").textContent=
    (d.running?"签到进行中… ":"")+"状态更新: "+(d.updated_at||"-");
  document.getElementById("btnAll").disabled=!!d.running;
}
async function post(url,body){
  const opt={method:"POST"};
  if(body!==undefined){opt.headers={"Content-Type":"application/json"};
    opt.body=JSON.stringify(body)}
  const r=await fetch(BASE+url.replace(/^\\//,''),opt);
  const d=await r.json();if(!r.ok||d.error){alert(d.error||"请求失败");return false}
  return d}
function signIdx(i){
  post("api/sign?name="+encodeURIComponent(ACCS[i].name))
    .then(d=>{if(d){alert(signMsg(d.results));refresh()}})}
function delIdx(i){const a=ACCS[i];
  if(!confirm("确定删除账号「"+a.name+"」？"))return;
  post("api/account/delete",{name:a.name}).then(ok=>{if(ok)refresh()})}
// 全部签到：按钮转圈防连点，完成后弹结果
async function signAll(){
  const btn=document.getElementById("btnAll");
  btn.disabled=true;btn.textContent="签到中…";
  const d=await post("api/sign");
  btn.disabled=false;btn.textContent="全部签到";
  if(d){alert(signMsg(d.results));refresh()}}
document.getElementById("btnAll").onclick=signAll;
// 签到结果 → 弹窗文案。重点：duplicate（今日已签）要明确告知，别让人以为没签上
function signMsg(list){
  return (list||[]).map(r=>{
    if(r.status==="ok")return "✓「"+r.name+"」签到成功，连续 "+(r.duration_days??"?")+" 天";
    if(r.status==="duplicate")return "「"+r.name+"」今日已签到，无需重复（连续 "+
      (r.duration_days??"?")+" 天）";
    if(r.status==="failed")return "✗「"+r.name+"」签到失败："+(r.msg||"")+
      "\\n  session_id 可能过期，请点「编辑」更新";
    return "✗「"+r.name+"」异常："+(r.msg||"未知");
  }).join("\\n")||"没有账号可签"}
// 不做常驻轮询：手动刷新页面(F5)看最新状态；点签到后上面会主动刷一次
refresh();
// ---- 添加/编辑弹窗
let EDITING=null;   // null=新增；否则存被编辑账号的原名
function openDlg(title){document.getElementById("dlgTitle").textContent=title;
  document.getElementById("mask").classList.add("show")}
function closeDlg(){document.getElementById("mask").classList.remove("show");EDITING=null}
function setTip(t,color){const el=document.getElementById("parseTip");
  el.textContent=t;el.style.color=color||"var(--sub)"}
document.getElementById("btnAdd").onclick=()=>{EDITING=null;
  document.getElementById("fName").value="";
  document.getElementById("fSid").value="";
  document.getElementById("fNote").value="";
  document.getElementById("fCurl").value="";setTip("");
  openDlg("添加账号")};
function editIdx(i){const a=ACCS[i];EDITING=a.name;
  document.getElementById("fName").value=a.name;
  document.getElementById("fSid").value=a.session_id;
  document.getElementById("fNote").value=a.note||"";
  document.getElementById("fCurl").value="";setTip("");
  openDlg("编辑账号："+a.name)}
// ---- 抓包 curl 一键解析
async function parseCurl(){
  const text=document.getElementById("fCurl").value;
  if(!text.trim()){setTip("请先粘贴 curl","var(--bad)");return}
  setTip("解析中…");
  try{
    const r=await fetch(BASE+"api/session/parse",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
    const d=await r.json();
    if(d.ok){document.getElementById("fSid").value=d.session_id;setTip("✓ 已填充","var(--ok)")}
    else setTip(d.error||"解析失败","var(--bad)");
  }catch(e){setTip("网络错误","var(--bad)")}}
async function saveAcc(){
  const body={name:document.getElementById("fName").value,
    session_id:document.getElementById("fSid").value,
    note:document.getElementById("fNote").value};
  if(EDITING)body.old_name=EDITING;
  const ok=await post("api/account/save",body);
  if(ok){closeDlg();refresh()}}
</script></body></html>"""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "byd-admin"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _auth_ok(self):
        conf = self.server.conf
        raw = self.headers.get("Authorization", "")
        if not raw.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(raw[6:]).decode("utf-8").partition(":")
        except Exception:  # noqa: BLE001
            return False
        return user == conf["user"] and pwd == conf["password"]

    def _need_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="byd-sign"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("需要登录".encode())

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, text):
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return None

    def do_GET(self):
        if not self._auth_ok():
            return self._need_auth()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._html(PAGE)
        if path == "/api/status":
            return self._json(merged_view())
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth_ok():
            return self._need_auth()
        path = urlparse(self.path).path

        if path == "/api/sign":
            name = (parse_qs(urlparse(self.path).query).get("name") or [None])[0]
            results, err = sign_now(name)
            if err:
                # 「还在进行中」是并发冲突 → 409；「没有名为」是参数错 → 400
                return self._json({"error": err}, 409 if "还在进行中" in err else 400)
            return self._json({"ok": True, "results": results})

        if path == "/api/session/parse":
            body = self._body()
            if body is None:
                return self._json({"error": "请求体不是合法 JSON"}, 400)
            try:
                sid = parse_session_id(body.get("text") or "")
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": f"解析失败: {exc}"}, 400)
            return self._json({"ok": True, "session_id": sid})

        if path == "/api/account/save":
            body = self._body()
            if body is None:
                return self._json({"error": "请求体不是合法 JSON"}, 400)
            res = upsert_account(body.get("name"), body.get("session_id"),
                                 body.get("note"), body.get("old_name"))
            return self._json(res, 200 if res["ok"] else 400)

        if path == "/api/account/delete":
            body = self._body()
            if body is None:
                return self._json({"error": "请求体不是合法 JSON"}, 400)
            res = delete_account(body.get("name"))
            return self._json(res, 200 if res["ok"] else 400)

        self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="比亚迪签到管理页")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    conf = load_conf()
    port = args.port or conf.get("port") or 8098
    srv = ThreadingHTTPServer((args.host, port), Handler)
    srv.conf = conf
    print(f"比亚迪签到管理台: http://{args.host}:{port}  "
          f"(用户 {conf['user']} / 密码 {conf['password']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
