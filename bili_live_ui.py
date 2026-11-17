#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DD轨迹 · 本地互动记录控制台。

双击「启动控制台.vbs」或运行 python bili_live_ui.py 即可，浏览器会打开仪表盘。
界面提供：采集状态、我的弹幕/礼物/上舰/互动、直播间管理、启停采集、生成报告。
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
import ctypes
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HAD_STDOUT = sys.stdout is not None

import bili_live as L

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = L.HERE
PORT = int(os.environ.get("BILI_UI_PORT", "8765"))
PID_FILE = os.path.join(HERE, "collector.pid")
DISABLED_FILE = os.path.join(HERE, "collector.disabled")
CONSOLE_LOG = os.path.join(HERE, "console.log")
READY_FILE = os.path.join(HERE, "console.ready")
UI_MUTEX = "Local\\BiliLiveWatch.Console.v1"
_ui_mutex_handle = None
TZ = timezone(timedelta(hours=8))
PYTHONW = (sys.executable if getattr(sys, "frozen", False)
           else os.path.join(os.path.dirname(sys.executable), "pythonw.exe"))
if not os.path.exists(PYTHONW):
    PYTHONW = sys.executable

_cache = {"offset": 0, "events": []}
_lock = threading.Lock()
MAX_EVENTS = 50000
CREATE_NO_WINDOW = 0x08000000
_alive = {"pid": None, "at": 0.0, "ok": False}
_scan = {"at": 0.0, "pid": None}

if not _HAD_STDOUT:
    try:
        sys.stdout = open(CONSOLE_LOG, "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    except OSError:
        pass


def acquire_console_lock():
    """控制台只启动一份；重复启动时复用已经监听的本机页面。"""
    global _ui_mutex_handle
    if _ui_mutex_handle:
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, UI_MUTEX)
        if not handle:
            return False
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        _ui_mutex_handle = handle
        return True
    except Exception:
        return True


def console_port_open():
    import socket
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def write_ready():
    try:
        with open(READY_FILE, "w", encoding="utf-8") as fh:
            fh.write("%d %d\n" % (os.getpid(), int(time.time())))
    except OSError:
        pass


def ready_heartbeat():
    while True:
        write_ready()
        time.sleep(5)


def run_quiet(args, timeout=15):
    """执行外部命令但不弹出控制台窗口。"""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


def events_cached():
    """合并【实时采集 + 历史导入】两份数据；文件变动时才重新解析。"""
    paths = [L.HISTORY_FILE, L.DANMAKUS_V3_FILE, L.EVENTS_FILE]
    key = []
    for p in paths:
        try:
            key.append(os.path.getmtime(p))
        except OSError:
            key.append(0.0)
    key = tuple(key)
    with _lock:
        if key != _cache.get("key"):
            _cache["events"] = L.load_events()
            _cache["key"] = key
        return list(_cache["events"])


def read_pid():
    if not collector_alive():
        return None
    try:
        # 心跳文件里带着采集器真实 PID，比单独维护 PID 文件可靠
        with open(os.path.join(HERE, "collector.alive"), "r", encoding="utf-8") as fh:
            pid = int(fh.read().split()[0])
    except Exception:
        pid = None
    if pid:
        try:
            with open(PID_FILE, "w", encoding="utf-8") as fh:
                fh.write(str(pid))
        except OSError:
            pass
        return pid
    found = find_collector_pid()
    if found:
        try:
            with open(PID_FILE, "w", encoding="utf-8") as fh:
                fh.write(str(found))
        except OSError:
            pass
    return found


def collector_alive():
    """以心跳文件为准判断采集器是否真的在跑（PID 会被系统复用，不可靠）。"""
    try:
        age = time.time() - os.path.getmtime(os.path.join(HERE, "collector.alive"))
    except OSError:
        return False
    return age < 120


def pid_alive(pid):
    """纯 Python 存活判断，不启动外部进程（避免弹出控制台窗口）。"""
    now = time.time()
    if _alive["pid"] == pid and now - _alive["at"] < 10:
        return _alive["ok"]
    ok = False
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            ok = True
    except Exception:
        ok = False
    _alive.update(pid=pid, at=now, ok=ok)
    return ok


def find_collector_pid():
    """兜底：扫描正在运行的采集进程（结果缓存 60 秒，避免频繁起进程）。"""
    now = time.time()
    if now - _scan["at"] < 60:
        return _scan["pid"]
    pid = None
    try:
        out = run_quiet(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bili_live.py*collect*' }"
             " | Select-Object -First 1).ProcessId"], timeout=25).stdout.strip()
        candidate = int(out)
        pid = candidate if pid_alive(candidate) else None
    except Exception:
        pid = None
    _scan.update(at=now, pid=pid)
    return pid


def collector_start(args=None):
    try:
        os.remove(DISABLED_FILE)   # 解除「停止采集」标记，守护进程才会代管
    except OSError:
        pass
    if read_pid():
        return "已在运行"
    argv = ["collect", "--follow", "--interval", "60", "--cap", "25",
            "--rotate-mb", "64", "--keep", "2"]
    try:
        with open(os.path.join(HERE, "collector.args.json"), "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, list) and saved and saved[0] == "collect":
            argv = [str(x) for x in saved]
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        cmd = [sys.executable] + argv
    else:
        cmd = [PYTHONW, os.path.join(HERE, "bili_live.py")] + argv
    if args:
        cmd.extend(args)
    kwargs = {"cwd": HERE, "creationflags": CREATE_NO_WINDOW}
    log_fh = None
    try:
        log_fh = open(os.path.join(HERE, "collector.log"), "a", encoding="utf-8")
        kwargs["stdout"] = log_fh
        kwargs["stderr"] = subprocess.STDOUT
    except OSError:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, env=dict(os.environ, PYTHONUNBUFFERED="1"), **kwargs)
    finally:
        if log_fh:
            log_fh.close()
    with open(PID_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    return "已启动 (PID %d)" % proc.pid


def collector_stop():
    with open(DISABLED_FILE, "w", encoding="utf-8") as fh:
        fh.write("stopped by user\n")   # 置标记，避免守护进程立刻把它拉起来
    pid = read_pid()
    if not pid:
        return "未在运行"
    run_quiet(["taskkill", "/F", "/PID", str(pid)], timeout=15)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return "已停止"


def load_rooms():
    if not os.path.isfile(L.ROOMS_FILE):
        return []
    with open(L.ROOMS_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh).get("rooms", [])


def save_rooms(rooms):
    with open(L.ROOMS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"rooms": rooms}, fh, ensure_ascii=False, indent=2)


def room_names(rooms):
    return {r["roomid"]: r.get("uname") or "" for r in rooms}


def known_room_names(rooms):
    """合并房间清单、旧历史与 v3 历史中的频道名称。"""
    names = room_names(rooms)
    try:
        with open(os.path.join(HERE, "danmakus_history.json"), "r", encoding="utf-8") as fh:
            for channel in json.load(fh).get("channels", []):
                data = channel.get("channel") or {}
                room_id = data.get("roomId")
                if room_id and data.get("uName"):
                    names[room_id] = data["uName"]
    except Exception:
        pass
    try:
        with open(os.path.join(L.DATA_DIR, "danmakus_v3_snapshot.json"),
                  "r", encoding="utf-8") as fh:
            for item in (json.load(fh).get("items") or []):
                data = item.get("channel") or {}
                room_id = data.get("roomId")
                if room_id and data.get("uName"):
                    names[room_id] = data["uName"]
    except Exception:
        pass
    return names


KIND_MAP = {"DANMU_MSG": "弹幕", "SEND_GIFT": "礼物", "GUARD_BUY": "上舰",
            "SUPER_CHAT_MESSAGE": "醒目留言"}


def event_item(e, names):
    """把一条事件转成界面展示用的结构。"""
    dt = datetime.fromtimestamp((e.get("ts") or 0) / 1000.0, TZ)
    extra = ""
    if e["cmd"] == "SEND_GIFT":
        extra = "×%s" % e.get("num")
    elif e["cmd"] == "GUARD_BUY":
        extra = "%s 个月" % e.get("num")
    amount = L.yuan_of(e)
    return {
        "time": dt.strftime("%m-%d %H:%M:%S"),
        "room": "账号通知" if e.get("room") == 0 else (names.get(e.get("room")) or str(e.get("room"))),
        "kind": KIND_MAP.get(e["cmd"], "互动"),
        "text": (e.get("text") or e.get("gift") or e.get("guard") or e.get("interact")
                 or {"ENTRY_EFFECT": "进入直播间", "ENTRY_EFFECT_MUST_RECEIVE": "进入直播间",
                     "INTERACT_WORD": "进入直播间"}.get(e["cmd"], e["cmd"])),
        "extra": extra,
        "src": e.get("src") or "live",
        "amount": round(amount, 2),
    }


def events_payload(kind="all", limit=200):
    """按分类返回明细；kind 可选 all/danmu/gift/sc/guard/enter/manual。"""
    rooms = load_rooms()
    names = known_room_names(rooms)
    events = L.dedupe_events(events_cached())
    rules = {
        "danmu": lambda e: e["cmd"] == "DANMU_MSG",
        "gift": lambda e: e["cmd"] == "SEND_GIFT",
        "sc": lambda e: e["cmd"] == "SUPER_CHAT_MESSAGE",
        "guard": lambda e: e["cmd"] == "GUARD_BUY",
        "enter": lambda e: e["cmd"].startswith(("ENTRY_EFFECT", "INTERACT_WORD")),
        "manual": lambda e: e.get("src") == "manual",
    }
    sel = [e for e in events if rules.get(kind, lambda x: True)(e)]
    sel.sort(key=lambda e: -(e.get("ts") or 0))
    return {"total": len(sel), "kind": kind,
            "items": [event_item(e, names) for e in sel[:limit]]}


def build_state():
    rooms = load_rooms()
    names = known_room_names(rooms)
    mid = None
    try:
        mid = L.my_mid()
    except Exception:
        pass
    events = events_cached()
    src = L.src_compare(events)
    uniq = L.dedupe_events(events)
    mine = [e for e in uniq if mid and e.get("uid") == mid]
    kinds = {
        "danmu": sum(1 for e in mine if e["cmd"] == "DANMU_MSG"),
        "gift": sum(1 for e in mine if e["cmd"] == "SEND_GIFT"),
        "guard": sum(1 for e in mine if e["cmd"] == "GUARD_BUY"),
        "sc": sum(1 for e in mine if e["cmd"] == "SUPER_CHAT_MESSAGE"),
        "interact": sum(1 for e in mine
                        if (e["cmd"].startswith("INTERACT_WORD")
                            and e.get("interact") != "进入直播间")
                        or e["cmd"] in ("DANMU_MSG", "SEND_GIFT", "GUARD_BUY",
                                        "SUPER_CHAT_MESSAGE", "LIKE_INFO_V3_CLICK")),
        "enter": sum(1 for e in mine if e["cmd"].startswith(("INTERACT_WORD", "ENTRY_EFFECT"))),
    }
    now = time.time()
    hourly = [0] * 24
    daily = {}
    for e in uniq:
        ts = (e.get("ts") or 0) / 1000.0
        if not ts or now - ts > 30 * 86400:
            continue
        dt = datetime.fromtimestamp(ts, TZ)
        if (dt.date() == datetime.now(TZ).date()):
            hourly[dt.hour] += 1
        daily[dt.strftime("%m-%d")] = daily.get(dt.strftime("%m-%d"), 0) + 1
    by_room = {}
    for e in mine:
        if e["cmd"].startswith("MESSAGEBOX"):
            continue
        by_room[e.get("room")] = by_room.get(e.get("room"), 0) + 1
    assets = {}
    if os.path.isfile(L.ACCOUNT_FILE):
        try:
            with open(L.ACCOUNT_FILE, "r", encoding="utf-8") as fh:
                assets = json.load(fh)
        except Exception:
            assets = {}
    recent = [event_item(e, names) for e in sorted(mine, key=lambda x: -(x.get("ts") or 0))[:200]]
    pid = read_pid()
    w = assets.get("wealth_info") or {}
    return {
        "collector": {
            "running": bool(pid),
            "pid": pid,
            "log_tail": tail(os.path.join(HERE, "collector.log"), 6),
        },
        "assets": {
            "coin": assets.get("billCoin", "-"), "level": assets.get("user_level", "-"),
            "wealth": w.get("level", "-"), "wealth_total": w.get("level_total_score", "-"),
            "intimacy": assets.get("user_intimacy", "-"),
        },
        "counts": dict(kinds, total=len(mine), all_events=len(uniq)),
        "spend": L.spend_summary(mine),
        "src": src,
        "hourly": hourly,
        "daily": sorted(daily.items()),
        "by_room": sorted([(names.get(r) or str(r), c) for r, c in by_room.items()],
                          key=lambda x: -x[1])[:12],
        "rooms": [{"roomid": r["roomid"], "uname": r.get("uname") or "",
                   "watching": bool(r.get("watching")), "live": r.get("live_status") == 1,
                   "online": r.get("online") or 0} for r in rooms],
        "recent": recent,
        "updated": datetime.now(TZ).strftime("%H:%M:%S"),
    }


def tail(path, lines):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [x.rstrip() for x in fh.readlines()[-lines:]]
    except Exception:
        return []


def refresh_live_status():
    """后台线程：每 5 分钟刷新各直播间开播状态。"""
    while True:
        try:
            rooms = load_rooms()
            uids = [r["uid"] for r in rooms if r.get("uid")]
            if uids:
                st = L.resolve_rooms(uids, L.cookie())
                for r in rooms:
                    v = st.get(r.get("uid"))
                    if v:
                        r["live_status"] = v.get("live_status")
                        r["online"] = v.get("online") or 0
                        r["title"] = v.get("title") or r.get("title")
                save_rooms(rooms)
        except Exception:
            pass
        time.sleep(300)




PAGE = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DD轨迹 · 互动记录</title>
<style>
:root{--bg:#0e1119;--card:#171c28;--line:#29304a;--fg:#e9eefb;--dim:#8b9ac0;
--pink:#fb7299;--blue:#00aeec;--ok:#3ddc97;--warn:#ffb454}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
header{position:sticky;top:0;z-index:9;background:linear-gradient(180deg,#151a26,#12161f);
border-bottom:1px solid var(--line);padding:14px 22px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px}
.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.off{background:#5a6480}
button{background:#243050;color:var(--fg);border:1px solid var(--line);border-radius:8px;
padding:7px 13px;cursor:pointer;font-size:13px}
button:hover{background:#2e3d64}
button.primary{background:linear-gradient(90deg,var(--blue),var(--pink));border:0;color:#fff}
button:disabled{opacity:.45;cursor:default}
.chip{background:#11161e;border:1px solid var(--line);border-radius:999px;padding:4px 13px;
font-size:12px;color:var(--dim);cursor:pointer}
.chip.on{background:linear-gradient(90deg,var(--blue),var(--pink));color:#fff;border-color:transparent}
main{padding:20px 22px 60px;max-width:1240px}
h2{font-size:14px;color:var(--dim);text-transform:none;margin:26px 0 10px;font-weight:600;letter-spacing:.4px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:21px;font-weight:600;margin-top:3px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--dim);font-weight:500}
.wrap{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.bars{display:flex;align-items:flex-end;gap:3px;height:90px}
.bars div{flex:1;background:linear-gradient(180deg,var(--pink),var(--blue));border-radius:3px 3px 0 0;min-height:2px}
.row{display:grid;grid-template-columns:190px 1fr 54px;gap:10px;align-items:center;margin:5px 0}
.label{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{height:9px;background:#0d1119;border-radius:5px;overflow:hidden}
.fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--pink))}
.num{text-align:right;font-size:12px;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);color:var(--dim)}
.live{color:#fff;background:var(--pink);border:0}
.muted{color:var(--dim);font-size:12px}
pre{margin:0;font-size:11.5px;color:var(--dim);white-space:pre-wrap;max-height:110px;overflow:auto}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=text]{background:#0d1119;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:7px 11px;width:150px}
#toast{position:fixed;top:66px;right:22px;z-index:99;padding:10px 16px;border-radius:10px;
background:linear-gradient(90deg,var(--blue),var(--pink));color:#fff;font-size:13px;
opacity:0;transform:translateY(-10px);transition:all .25s ease;pointer-events:none;
box-shadow:0 8px 22px rgba(0,0,0,.4)}
#toast.err{background:linear-gradient(90deg,#e74c3c,#c0392b)}
#toast.show{opacity:1;transform:translateY(0)}
</style></head><body>
<div id="toast"></div>
<header>
  <h1>DD轨迹 · 互动记录</h1>
  <span id="status" class="muted"><span class="dot off"></span>加载中…</span>
</header>
<main>
  <div class="muted" style="margin-bottom:10px">数据每 3 秒自动刷新 · 最后更新 <span id="updated">-</span></div>
  <div class="cards" id="cards"></div>
  <h2>消费统计</h2>
  <div class="cards" id="spendcards"></div>
  <h2>我的直播行为</h2>
  <div class="cards" id="mycards"></div>
  <h2>今日事件分布（按小时）</h2>
  <div class="panel"><div class="bars" id="hourly"></div>
    <div class="muted" style="margin-top:6px">0 时 → 23 时</div></div>
  <h2>我参与最多的直播间</h2>
  <div class="panel" id="byroom"></div>
  <h2>我的记录 <span class="muted" id="recentCount"></span></h2>
  <div class="panel">
    <div class="toolbar" id="recentFilter" style="margin-bottom:10px">
      <button class="chip on" data-kind="all">全部</button>
      <button class="chip" data-kind="danmu">弹幕</button>
      <button class="chip" data-kind="gift">礼物</button>
      <button class="chip" data-kind="sc">醒目留言</button>
      <button class="chip" data-kind="guard">上舰</button>
      <button class="chip" data-kind="enter">进入</button>
      <button class="chip" data-kind="manual">手动补录</button>
    </div>
    <div class="wrap"><table id="recent"></table></div>
  </div>
  <h2>数据来源对照（查漏记）</h2>
  <div class="panel" id="src"></div>
  <h2>直播间管理</h2>
  <div class="panel">
    <div class="toolbar" style="margin-bottom:12px">
      <input type="text" id="roomInput" placeholder="直播间号">
      <button id="btnAdd">添加</button>
      <button id="btnDiscover">从关注列表重新发现</button>
      <span class="muted" id="roomCount"></span>
    </div>
    <div class="wrap"><table id="rooms"></table></div>
  </div>
  <h2>运行日志</h2>
  <div class="panel"><pre id="log"></pre></div>
</main>
<script>
const $=s=>document.querySelector(s);
let recentKind='all', lastCount=-1;
async function loadRecent(){
  const d=await api('/api/events?kind='+recentKind+'&limit=200');
  const label={danmu:'弹幕',gift:'礼物',sc:'醒目留言',guard:'上舰',enter:'进入',manual:'手动补录'}[recentKind]||'全部';
  $('#recentCount').textContent=`${label} · 共 ${d.total} 条`;
  $('#recent').innerHTML='<thead><tr><th>时间</th><th>直播间</th><th>类型</th><th>内容</th><th class="num">金额</th><th>来源</th></tr></thead><tbody>'+
    (d.items.map(e=>`<tr><td>${e.time}</td><td>${esc(e.room)}</td><td>${e.kind}</td>`+
     `<td>${esc(e.text)||'—'}${e.extra?' '+esc(e.extra):''}</td>`+
     `<td class="num">${e.amount>0?'¥'+e.amount.toFixed(2):'—'}</td>`+
     `<td class="muted">${e.src==='manual'?'手动':e.src==='backfill'?'回补':e.src==='danmakus'?'导入':'实时'}</td></tr>`).join('')||
     '<tr><td colspan="6" class="muted">该分类暂无记录</td></tr>')+'</tbody>';
}
document.querySelectorAll('#recentFilter .chip').forEach(b=>{
  b.onclick=()=>{document.querySelectorAll('#recentFilter .chip').forEach(x=>x.classList.remove('on'));
    b.classList.add('on'); recentKind=b.dataset.kind; loadRecent();};
});
async function api(path,opt){const r=await fetch(path,opt);return r.json();}
function cards(el,items){el.innerHTML=items.map(([k,v])=>
  `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');}
function bars(el,arr){const m=Math.max(1,...arr);el.innerHTML=arr.map((n,i)=>
  `<div style="height:${Math.round(n/m*100)}%" title="${i}时 · ${n} 条"></div>`).join('');}
function bars2(el,arr){const t=arr.reduce((a,b)=>a+b[1],0)||1;el.innerHTML=arr.map(([k,n])=>
  `<div class="row"><div class="label">${k}</div><div class="track"><div class="fill" style="width:${n/t*100}%"></div></div><div class="num">${n}</div></div>`).join('')||'<div class="muted">暂无数据</div>';}
async function refresh(){
  const s=await api('/api/state');
  $('#status').innerHTML=`<span class="dot ${s.collector.running?'on':'off'}"></span>`+
    (s.collector.running?`采集运行中 · PID ${s.collector.pid}`:'采集未运行');
  $('#updated').textContent=s.updated;
  cards($('#cards'),[
    ['B币',s.assets.coin],
    ['直播等级','LV'+s.assets.level],['财富等级','LV'+s.assets.wealth],
    ['财富累计分',s.assets.wealth_total],['亲密度',s.assets.intimacy],
    ['跟踪房间',s.rooms.filter(r=>r.watching).length],['已采集事件',s.counts.all_events]]);
  const y=n=>'¥'+Number(n||0).toFixed(2);
  cards($('#spendcards'),[
    ['总支出',y(s.spend.total)],['礼物支出',y(s.spend.gift)],['上舰支出',y(s.spend.guard)],
    ['SC 支出',y(s.spend.sc)],['免费礼物',s.spend.free_gift_count]]);
  cards($('#mycards'),[
    ['我的弹幕',s.counts.danmu],['我的礼物',s.counts.gift],['我的上舰',s.counts.guard],
    ['醒目留言',s.counts.sc],['进入直播间',s.counts.enter]]);
  bars($('#hourly'),s.hourly);
  bars2($('#byroom'),s.by_room);
  $('#src').innerHTML=`实时流弹幕 <b>${s.src.live}</b> ｜ 回补弹幕 <b>${s.src.backfill}</b> ｜ `+
    `两路重叠 <b>${s.src.both}</b> ｜ 仅回补有（实时流可能漏记） <b style="color:var(--warn)">${s.src.backfill_only}</b> ｜ `+
    `仅实时流有 <b>${s.src.live_only}</b>`;
  if (s.counts.all_events !== lastCount) { lastCount = s.counts.all_events; loadRecent(); }
  const rm=$('#rooms');rm.innerHTML='<thead><tr><th>状态</th><th>主播</th><th>直播间号</th><th>在线</th><th>跟踪</th><th></th></tr></thead><tbody>'+
    s.rooms.slice(0,80).map(x=>`<tr><td>${x.live?'<span class="pill live">直播中</span>':'<span class="pill">未开播</span>'}</td>`+
     `<td>${esc(x.uname)}</td><td>${x.roomid}</td><td>${x.online}</td><td>${x.watching?'是':'否'}</td>`+
     `<td><button onclick="toggle(${x.roomid})">${x.watching?'停跟':'跟踪'}</button></td></tr>`).join('')+'</tbody>';
  $('#roomCount').textContent=`共 ${s.rooms.length} 个，跟踪中 ${s.rooms.filter(x=>x.watching).length} 个`;
  $('#log').textContent=s.collector.log_tail.join('\n');
}
function esc(t){return String(t??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
let toastTimer=null;
function toast(msg,ok){const el=$('#toast');el.textContent=msg;
  el.className=(ok===false?'err ':'')+'show';
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>{el.className=(ok===false?'err':'')+' ';},2800);}
async function post(p,b){
  try{
    const r=await api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
    toast(r.msg||'完成', r.ok!==false);
    await refresh();
    return r;
  }catch(e){ toast('请求失败：'+e.message,false); }
}
window.toggle=id=>post('/api/rooms/toggle',{roomid:id});
$('#btnAdd').onclick=()=>{const v=$('#roomInput').value.trim();if(v)post('/api/rooms/add',{roomid:v});};
$('#btnDiscover').onclick=()=>post('/api/discover');
refresh();setInterval(refresh,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, json.dumps(build_state(), ensure_ascii=False))
        if path == "/api/manual":
            return self._send(200, json.dumps(
                L.manual_schema(), ensure_ascii=False))
        if path == "/api/events":
            q = parse_qs(urlparse(self.path).query)
            kind = (q.get("kind") or ["all"])[0]
            try:
                limit = max(1, min(2000, int((q.get("limit") or ["200"])[0])))
            except ValueError:
                limit = 200
            return self._send(200, json.dumps(events_payload(kind, limit), ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        result = {"ok": True}
        try:
            if path == "/api/manual":
                event = L.append_manual_event(payload)
                result.update(event=event, msg="手动补录已保存")
            elif path == "/api/collector":
                result["msg"] = collector_start() if payload.get("action") == "start" else collector_stop()
            elif path == "/api/assets":
                L.cmd_snapshot()
                result["msg"] = "资产已刷新"
            elif path == "/api/report":
                L.cmd_report()
                result["msg"] = "报告已生成"
            elif path == "/api/rooms/toggle":
                rooms = load_rooms()
                for r in rooms:
                    if r["roomid"] == payload.get("roomid"):
                        r["watching"] = not r.get("watching")
                save_rooms(rooms)
                result["msg"] = "已更新"
            elif path == "/api/rooms/add":
                result["msg"] = "已添加"
                L.cmd_add(int(payload.get("roomid")))
            elif path == "/api/discover":
                threading.Thread(target=L.cmd_discover, daemon=True).start()
                result["msg"] = "正在后台发现，稍后刷新"
            else:
                return self._send(404, json.dumps({"error": "not found"}))
        except Exception as exc:
            result = {"ok": False, "msg": str(exc)}
        return self._send(200, json.dumps(result, ensure_ascii=False))


def main():
    url = "http://127.0.0.1:%d/" % PORT
    if not acquire_console_lock():
        for _ in range(40):
            if console_port_open():
                break
            time.sleep(0.25)
        print("控制台已经在运行：%s" % url)
        if "--no-browser" not in sys.argv and console_port_open():
            webbrowser.open(url)
        return
    threading.Thread(target=refresh_live_status, daemon=True).start()
    # 采集器的生命周期由 bili_watchdog.py 独立守护；这里只是兜底，且尊重「停止采集」标记
    if not os.path.isfile(DISABLED_FILE) and not read_pid():
        collector_start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        srv.daemon_threads = True
    except OSError as exc:
        print("控制台端口 %d 启动失败：%s" % (PORT, exc))
        if console_port_open():
            print("已有控制台在监听，本次进程退出。")
            if "--no-browser" not in sys.argv:
                webbrowser.open(url)
            return
        raise
    write_ready()
    threading.Thread(target=ready_heartbeat, daemon=True).start()
    print("控制台已启动：%s" % url)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
        try:
            with open(READY_FILE, "r", encoding="utf-8") as fh:
                ready_pid = int(fh.read().split()[0])
            if ready_pid == os.getpid():
                os.remove(READY_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    main()
