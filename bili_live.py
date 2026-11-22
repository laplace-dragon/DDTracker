#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站直播弹幕采集与自查（只采集公开直播间弹幕流，用于分析本人行为）。

命令：
    python bili_live.py snapshot     # 拉取直播个人中心资产/消费数据
    python bili_live.py discover     # 从关注列表找出正在直播的房间，写入 rooms.json
    python bili_live.py add 5050     # 手动加入一个直播间
    python bili_live.py collect      # 开始采集（Ctrl+C 停止），写入 live_events.jsonl
    python bili_live.py report       # 由已采集数据生成 live_report.html
    python bili_live.py sync-danmakus # 与 danmakus v3 历史校准并补缺
    python bili_live.py verify       # 实时流与回补数据交叉校验（查漏记）

说明：
- 默认只保留登录用户本人的事件，别人的弹幕/礼物一概不落盘（加 --all 才保留全场）。
- 弹幕流是实时流，只能从开始采集那一刻往后记录，无法回溯历史。
- 只连接公开直播间、只记录公开广播的事件；不做验证码/风控绕过。
"""

import asyncio
import base64
import ctypes
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import brotli
import websockets

import bili_pb
import local_security
import live_report_view

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    # 输出被重定向到文件/管道时也逐行落盘，日志不滞后
    sys.stdout.reconfigure(line_buffering=True)
    if sys.stderr is not None:
        sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

HERE = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(HERE, "settings.json")
COOKIE_FILE = os.path.join(HERE, "cookie.txt")
DATA_FILE = os.path.join(HERE, "data.json")
ROOMS_FILE = os.path.join(HERE, "rooms.json")
ACCOUNT_FILE = os.path.join(HERE, "live_account.json")
REPORT_FILE = os.path.join(HERE, "live_report.html")
ALIVE_FILE = os.path.join(HERE, "collector.alive")
HISTORY_FILE = os.path.join(HERE, "history_events.jsonl")
DEFAULT_DATA_DIR = os.path.join(HERE, "data")
COLLECTOR_MUTEX = "Local\\BiliLiveWatch.Collector.v1"
_collector_mutex_handle = None


def resolve_data_dir(value=None):
    configured = value
    if value is None:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                configured = (json.load(fh) or {}).get("data_dir")
        except Exception:
            configured = None
    path = str(configured or DEFAULT_DATA_DIR).strip() or DEFAULT_DATA_DIR
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    path = os.path.abspath(path)
    os.makedirs(path, exist_ok=True)
    if not configured and os.path.normcase(path) == os.path.normcase(DEFAULT_DATA_DIR):
        # 旧版本把采集文件放在程序根目录；首次升级时复制到专用 data 目录。
        for source in glob.glob(os.path.join(HERE, "live_events*.jsonl")):
            target = os.path.join(path, os.path.basename(source))
            if not os.path.exists(target):
                shutil.copy2(source, target)
    return path


DATA_DIR = resolve_data_dir()
EVENTS_FILE = os.path.join(DATA_DIR, "live_events.jsonl")
DANMAKUS_V3_FILE = os.path.join(DATA_DIR, "danmakus_v3_events.jsonl")
DANMAKUS_V3_PRICE_FIX_FILE = os.path.join(
    DATA_DIR, "danmakus_v3_guard_price_fixes.jsonl")


def configure_data_paths(value=None):
    """重新解析采集数据目录，供客户端切换路径后立即使用。"""
    global DATA_DIR, EVENTS_FILE, DANMAKUS_V3_FILE, DANMAKUS_V3_PRICE_FIX_FILE
    DATA_DIR = resolve_data_dir(value)
    EVENTS_FILE = os.path.join(DATA_DIR, "live_events.jsonl")
    DANMAKUS_V3_FILE = os.path.join(DATA_DIR, "danmakus_v3_events.jsonl")
    DANMAKUS_V3_PRICE_FIX_FILE = os.path.join(
        DATA_DIR, "danmakus_v3_guard_price_fixes.jsonl")
    return EVENTS_FILE

if sys.stdout is None:  # pythonw.exe 无控制台时兜底
    sys.stdout = open(os.path.join(HERE, "collector.log"), "a", encoding="utf-8", buffering=1)
if sys.stderr is None:  # 否则崩溃时连回溯都留不下来
    sys.stderr = sys.stdout

TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
         33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
         26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
         20, 34, 44, 52]
GUARD = {1: "总督", 2: "提督", 3: "舰长"}
INTERACT = {1: "进入直播间", 2: "关注了主播", 3: "分享了直播间", 4: "特别关注", 5: "互相关注"}

# 运行健康度：守护进程只能兜底「进程已退出」，这里兜底「进程活着但收不到消息」的假活状态。
_health = {"start": time.time(), "cycles": 0, "live": 0, "last_frame": time.time()}

# 账号级通知（MESSAGEBOX 等）会被广播到每一条连接：同内容短时间内的副本只记一次。
_PERSONAL_WINDOW = 10.0
_personal_seen = {}
_ENTRY_DEDUPE_MS = 15000
_entry_seen = {}
_manual_append_lock = threading.Lock()


def acquire_collector_lock():
    """保证同一台电脑只运行一个采集器，避免重复写入同一批事件。"""
    global _collector_mutex_handle
    if _collector_mutex_handle:
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, COLLECTOR_MUTEX)
        if not handle:
            return False
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _collector_mutex_handle = handle
        return True
    except Exception:
        # 非 Windows 调试环境没有互斥量；保留原有的心跳兜底逻辑
        return True


def _personal_dup(ev):
    """同一条账号级通知在 10 秒内重复到达时返回 True（只保留第一份）。"""
    if not ev.get("personal"):
        return False
    key = (ev.get("uid"), ev.get("cmd"), ev.get("text") or ev.get("detail"))
    now = time.time()
    last = _personal_seen.get(key)
    _personal_seen[key] = now
    if len(_personal_seen) > 500:
        for k, t in list(_personal_seen.items()):
            if now - t > 300:
                _personal_seen.pop(k, None)
    return last is not None and now - last < _PERSONAL_WINDOW


def _is_entry_event(ev):
    cmd = str(ev.get("cmd") or "")
    return (cmd.startswith("ENTRY_EFFECT")
            or (cmd.startswith("INTERACT_WORD")
                and ev.get("interact") == "进入直播间"))


def _entry_dup(ev):
    """B 站会把一次进入推送成两条相近事件，15 秒内同房间只保留一条。"""
    if not _is_entry_event(ev):
        return False
    key = (ev.get("room"), ev.get("uid"))
    stamp = int(ev.get("ts") or time.time() * 1000)
    last = _entry_seen.get(key)
    _entry_seen[key] = stamp
    if len(_entry_seen) > 2000:
        cutoff = stamp - 60000
        for k, t in list(_entry_seen.items()):
            if t < cutoff:
                _entry_seen.pop(k, None)
    return last is not None and 0 <= stamp - last <= _ENTRY_DEDUPE_MS


def cookie():
    if not os.path.isfile(COOKIE_FILE):
        print("缺少 cookie.txt，请先运行 bili_login.py --qr / --poll。")
        sys.exit(2)
    with open(COOKIE_FILE, "r", encoding="utf-8") as fh:
        value = fh.read().strip()
    local_security.restrict_private_file(COOKIE_FILE)
    return value


def cookie_uid():
    try:
        for part in cookie().split(";"):
            key, _, value = part.strip().partition("=")
            if key == "DedeUserID" and value:
                return int(value)
    except Exception:
        pass
    return None


def my_mid():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            mid = (json.load(fh).get("nav") or {}).get("mid")
        if mid:
            return int(mid)
    except Exception:
        pass
    mid = cookie_uid()
    if mid:
        return mid
    raise RuntimeError("无法确定当前登录用户 UID，请重新扫码登录。")


def mixin_key():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            w = (json.load(fh).get("nav") or {}).get("wbi_img")
    except Exception:
        w = None
    if not w:
        payload = get("https://api.bilibili.com/x/web-interface/nav", cookie(),
                      "https://www.bilibili.com/")
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "无法获取 WBI 签名信息")
        w = (payload.get("data") or {}).get("wbi_img") or {}
    if not w.get("img_url") or not w.get("sub_url"):
        raise RuntimeError("无法获取 WBI 签名信息")
    stem = lambda u: u.rsplit("/", 1)[-1].split(".")[0]
    raw = stem(w["img_url"]) + stem(w["sub_url"])
    return "".join(raw[i] for i in MIXIN)[:32]


def sign(params):
    params = dict(params)
    params["wts"] = int(time.time())
    clean = [(k, "".join(c for c in str(v) if c not in "!'()*")) for k, v in sorted(params.items())]
    qs = urllib.parse.urlencode(clean)
    params["w_rid"] = hashlib.md5((qs + mixin_key()).encode()).hexdigest()
    return urllib.parse.urlencode(params)


def get(url, cookie_str, referer="https://live.bilibili.com/"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer,
        "Origin": "https://live.bilibili.com", "Cookie": cookie_str,
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def live_api(path, params, cookie_str):
    return get("https://api.live.bilibili.com%s?%s" % (path, sign(params)), cookie_str)


def cmd_snapshot():
    ck = cookie()
    mid = my_mid()
    info = (get("https://api.live.bilibili.com/xlive/web-ucenter/user/get_user_info", ck).get("data") or {})
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)
    w = info.get("wealth_info") or {}
    print("直播资产：B币 %s" % info.get("billCoin"))
    print("直播等级 LV%s ｜ 亲密度 %s ｜ 财富等级 LV%s（累计 %s 分，当前 %s）"
          % (info.get("user_level"), info.get("user_intimacy"), w.get("level"),
             w.get("level_total_score"), w.get("cur_score")))
    print("已写入 %s" % ACCOUNT_FILE)
    return 0


def resolve_rooms(uids, ck):
    """批量查询 uid 对应的直播间与开播状态。"""
    out = {}
    pending = [uids[i:i + 50] for i in range(0, len(uids), 50)]
    while pending:
        chunk = pending.pop(0)
        q = "&".join("uids[]=%d" % u for u in chunk)
        try:
            j = get("https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids?%s" % q, ck)
        except Exception as exc:
            if len(chunk) > 1:
                middle = len(chunk) // 2
                pending[0:0] = [chunk[:middle], chunk[middle:]]
                print("  [批量查询失败，拆分 %d 个 UID 重试] %s"
                      % (len(chunk), exc))
                time.sleep(0.3)
            else:
                print("  [房间状态查询失败] UID %s：%s" % (chunk[0], exc))
            continue
        if j.get("code") not in (None, 0):
            if len(chunk) > 1:
                middle = len(chunk) // 2
                pending[0:0] = [chunk[:middle], chunk[middle:]]
                print("  [批量查询返回 %s，拆分 %d 个 UID 重试]"
                      % (j.get("code"), len(chunk)))
                time.sleep(0.3)
            else:
                print("  [房间状态查询返回 %s] UID %s"
                      % (j.get("code"), chunk[0]))
            continue
        data = j.get("data") or {}
        for uid_s, v in data.items():
            if isinstance(v, dict) and v.get("room_id"):
                out[int(uid_s)] = v
        if len(pending) and len(chunk) == 50:
            time.sleep(0.15)
    return out


def cmd_discover():
    ck = cookie()
    mid = my_mid()
    existing_rooms = []
    try:
        with open(ROOMS_FILE, "r", encoding="utf-8") as fh:
            existing_rooms = json.load(fh).get("rooms", [])
    except Exception:
        pass
    existing_by_room = {
        int(room["roomid"]): room for room in existing_rooms
        if room.get("roomid")
    }
    uids = []
    for pn in range(1, 12):
        j = get("https://api.bilibili.com/x/relation/followings?vmid=%d&pn=%d&ps=50&order=desc" % (mid, pn),
                ck, "https://space.bilibili.com/")
        lst = (j.get("data") or {}).get("list") or []
        if not lst:
            break
        uids.extend(x["mid"] for x in lst)
        if len(lst) < 50:
            break
        time.sleep(0.3)
    print("关注列表：%d 人" % len(uids))
    status = resolve_rooms(uids, ck)
    print("其中开通直播间的：%d" % len(status))
    live = [v for v in status.values() if v.get("live_status") == 1]
    print("当前正在直播：%d" % len(live))
    rooms_by_id = {}
    for v in sorted(status.values(), key=lambda x: -(x.get("online") or 0)):
        room_id = int(v["room_id"])
        rooms_by_id[room_id] = {
            "roomid": room_id,
            "uid": v.get("uid"),
            "uname": v.get("uname"),
            "live_status": v.get("live_status"),
            "title": v.get("title"),
            "online": v.get("online"),
        }
    for room_id, previous in existing_by_room.items():
        if room_id not in rooms_by_id:
            retained = dict(previous)
            retained.pop("watching", None)
            # 本次状态查询没有返回时，旧的开播状态已经失效。
            retained["live_status"] = 0
            retained["online"] = 0
            rooms_by_id[room_id] = retained
    rooms = sorted(
        rooms_by_id.values(),
        key=lambda x: (
            0 if x.get("live_status") == 1 else 1,
            -(x.get("online") or 0),
            str(x.get("uname") or ""),
        ))
    with open(ROOMS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"rooms": rooms}, fh, ensure_ascii=False, indent=2)
    for r in rooms[:12]:
        print("  %s  room=%-11s online=%-8s %s" % ("LIVE" if r["live_status"] == 1 else "    ",
                                                   r["roomid"], r["online"], (r["uname"] or "")[:16]))
    print("已写入 %s（可用 add 命令补充）" % ROOMS_FILE)
    return 0


def cmd_add(rid):
    ck = cookie()
    try:
        info = live_api("/xlive/web-room/v1/index/getInfoByRoom", {"room_id": rid}, ck).get("data") or {}
        room = info.get("room_info") or {}
        name = (info.get("anchor_info") or {}).get("base_info", {}).get("uname") or str(rid)
    except Exception:
        room, name = {}, str(rid)
    rooms = []
    if os.path.isfile(ROOMS_FILE):
        with open(ROOMS_FILE, "r", encoding="utf-8") as fh:
            rooms = json.load(fh).get("rooms", [])
    if any(r.get("roomid") == rid for r in rooms):
        print("已存在：%s" % rid)
        return 0
    rooms.append({"roomid": rid, "uid": room.get("uid"), "uname": name,
                  "live_status": room.get("live_status"), "title": room.get("title"),
                  "online": room.get("online")})
    with open(ROOMS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"rooms": rooms}, fh, ensure_ascii=False, indent=2)
    print("已加入 %s（%s），共 %d 个房间" % (rid, name, len(rooms)))
    return 0


def pack(op, body=b"", proto=1):
    return struct.pack(">IHHII", 16 + len(body), 16, proto, op, 1) + body


def unpack(data):
    out, off = [], 0
    while off + 16 <= len(data):
        plen, hlen, proto, op, _ = struct.unpack(">IHHII", data[off:off + 16])
        if plen <= 0:
            break
        body = data[off + hlen:off + plen]
        off += plen
        if proto in (2, 3):
            try:
                raw = zlib.decompress(body) if proto == 2 else brotli.decompress(body)
            except Exception:
                continue
            out.extend(unpack(raw))
        else:
            out.append((op, body))
    return out


def keep_event(ev, mid):
    """默认只保留登录用户本人的事件，别人的数据一概不落盘（--all 才保留全场）。"""
    if ev.get("cmd") == "GIFT_POPUP":
        # 账号级礼物广告/展示推送，不是本人互动或消费记录。
        return False
    if _limit["all"]:
        return True
    return ev.get("uid") == mid


def pick(fields, num, kind, text=False):
    """从 protobuf 解析结果里取指定字段号的值。"""
    for field, fkind, val in fields:
        if field == num and fkind == kind:
            if text:
                try:
                    return val.decode("utf-8", "replace")
                except Exception:
                    return None
            return val
    return None


_WARNED_V2 = set()
_ROOM_WIDE = {
    "ONLINE_RANK_V3", "ONLINE_RANK_COUNT", "ONLINE_RANK_TOP3", "WATCHED_CHANGE",
    "STOP_LIVE_ROOM_LIST", "RANK_CHANGED_V2", "LIKE_INFO_V3_UPDATE", "DM_INTERACTION",
    "ROOM_REAL_TIME_MESSAGE_UPDATE", "WIDGET_BANNER", "COMMON_NOTICE_DANMAKU",
}


def warn_unparsed(cmd, data):
    """遇到没做字段映射的 protobuf 事件时报警一次，避免静默漏采。"""
    if (cmd not in _ROOM_WIDE and isinstance(data, dict) and data.get("pb")
            and cmd not in _WARNED_V2):
        _WARNED_V2.add(cmd)
        print("[警告] 未解析的 protobuf 事件类型：%s（可能需要补字段映射）" % cmd)


def apply_medal_fields(ev, medal):
    """统一提取礼物、SC、上舰、互动中的粉丝牌信息。"""
    if not isinstance(medal, dict):
        return ev
    level = medal.get("medal_level", medal.get("level"))
    name = (medal.get("medal_name") or medal.get("name")
            or medal.get("medalName"))
    anchor = (medal.get("anchor_uname") or medal.get("anchor_name")
              or medal.get("anchorName"))
    roomid = (medal.get("anchor_roomid") or medal.get("roomid")
              or medal.get("anchorRoomid"))
    if level is not None:
        ev["medal"] = level
        ev["medal_level"] = level
    if name:
        ev["medal_name"] = name
    if anchor:
        ev["medal_anchor"] = anchor
    if roomid:
        try:
            ev["medal_room"] = int(roomid)
        except (TypeError, ValueError):
            pass
    return ev


def normalize(msg, rid):
    cmd = msg.get("cmd") or ""
    data = msg.get("data") or {}
    ev = {"ts": int(time.time() * 1000), "room": rid, "cmd": cmd}
    try:
        if cmd == "DANMU_MSG":
            info = msg["info"]
            medal_info = info[3] or []
            ev.update(uid=info[2][0], uname=info[2][1], text=info[1],
                      ts=info[0][4],
                      medal=medal_info[0] if medal_info else None,
                      medal_level=medal_info[0] if medal_info else None,
                      medal_name=(medal_info[1] if len(medal_info) > 1 else ""),
                      medal_anchor=(medal_info[2] if len(medal_info) > 2 else ""),
                      medal_room=(medal_info[3] if len(medal_info) > 3 else None))
        elif cmd == "SEND_GIFT":
            ev.update(uid=data.get("uid"), uname=data.get("uname"), gift=data.get("giftName"),
                      num=data.get("num"), price=data.get("price"),
                      total_coin=data.get("total_coin"), coin_type=data.get("coin_type"),
                      free=1 if data.get("coin_type") == "silver" else 0,
                      ts=int((data.get("timestamp") or 0) * 1000) or ev["ts"])
            apply_medal_fields(ev, data.get("medal_info"))
        elif cmd == "GUARD_BUY":
            ev.update(uid=data.get("uid"), uname=data.get("username"),
                      guard=GUARD.get(data.get("guard_level"), data.get("guard_level")),
                      num=data.get("num"), price=data.get("price"),
                      payflow_id=data.get("payflow_id"))
            apply_medal_fields(ev, data.get("medal_info"))
        elif cmd == "USER_TOAST_MSG":
            guard_level = data.get("guard_level")
            ev.update(
                uid=data.get("uid"), uname=data.get("uname"),
                guard=GUARD.get(guard_level) or data.get("role_name") or guard_level,
                num=data.get("num") or 1,
                guard_paid_price=data.get("price"),
                payflow_id=data.get("payflow_id"),
                detail=json.dumps(data, ensure_ascii=False)[:2000])
        elif cmd in ("SUPER_CHAT_MESSAGE", "SUPER_CHAT_MESSAGE_JPN"):
            # 日文副本与主事件是同一笔，统一成同一个 cmd，避免重复计数
            ev["cmd"] = "SUPER_CHAT_MESSAGE"
            ev["jpn"] = 1 if cmd.endswith("_JPN") else 0
            ui = data.get("user_info") or {}
            ev.update(uid=data.get("uid"), uname=ui.get("uname"), text=data.get("message"),
                      price=data.get("price"))
            apply_medal_fields(ev, ui.get("medal"))
        elif cmd.startswith("INTERACT_WORD"):
            d = data.get("pb_decoded") if isinstance(data.get("pb_decoded"), dict) else data
            ev.update(uid=d.get("uid"), uname=d.get("uname"),
                      interact=INTERACT.get(d.get("msg_type"), d.get("msg_type")))
            apply_medal_fields(
                ev, d.get("fans_medal") or d.get("medal_info") or d.get("medal"))
        elif cmd.startswith("LIKE_INFO"):
            ev.update(uid=data.get("uid"), uname=data.get("uname"), text="点赞")
        elif cmd.startswith("LIKE_GUIDE"):
            # 账号级「主播@你」引导：同样广播到所有连接，不能归到某个房间
            ev["room"] = 0
            ev["personal"] = 1
            ev.update(uid=data.get("uid"), uname=data.get("uname"),
                      text=data.get("like_text") or "",
                      detail=json.dumps(data, ensure_ascii=False)[:300])
        elif cmd.startswith("ENTRY_EFFECT"):
            ev["cmd"] = "ENTRY_EFFECT"  # 与 MUST_RECEIVE 变体合并，避免重复计数
            ev.update(uid=data.get("uid"), interact="进入直播间")
        elif cmd.startswith("MESSAGEBOX"):
            # 账号级通知（勋章变化等）：由服务器广播到你所有连接，
            # 不带任何房间含义，归零避免被误读成「进过这些直播间」
            ev["room"] = 0
            ev["personal"] = 1
            ev.update(uid=data.get("uid"), uname=data.get("uname"),
                      text=data.get("upper_bound_content") or data.get("msg") or "",
                      detail=json.dumps(data, ensure_ascii=False)[:300])
        elif cmd == "SEND_GIFT_V2":
            # 部分直播间把礼物升级成 protobuf 格式，需要按字段位置解析
            ev["cmd"] = "SEND_GIFT"
            raw = base64.b64decode(data.get("pb") or "")
            root = bili_pb.dump(raw)
            ev["uid"] = pick(root, 1, "varint")
            ev["uname"] = pick(root, 2, "bytes", text=True)
            gift = bili_pb.dump(pick(root, 10, "bytes") or b"")
            ev["gift"] = pick(gift, 2, "bytes", text=True)
            ev["num"] = pick(gift, 3, "varint") or 1
            ev["price"] = pick(gift, 5, "varint") or 0
            ev["coin_type"] = pick(gift, 8, "bytes", text=True)
            ev["free"] = 1 if ev["coin_type"] == "silver" else 0
            ev["total_coin"] = int(ev["price"]) * int(ev["num"])
            stamp = pick(gift, 10, "varint")
            if stamp:
                ev["ts"] = int(stamp) * 1000
        else:
            # 其它类型也留一手原始内容，便于事后确认到底发生了什么
            warn_unparsed(cmd, data)
            ev.update(uid=data.get("uid"), uname=data.get("uname"),
                      detail=json.dumps(data, ensure_ascii=False)[:400] if data else "")
    except Exception:
        pass
    ev["src"] = "live"
    return ev


def event_key(ev):
    body = ev.get("text") or ev.get("gift") or ev.get("guard") or ev.get("interact") or ""
    try:
        quantity = int(ev.get("num") or 1)
    except (TypeError, ValueError):
        quantity = 1
    return "%s|%s|%s|%s|%s|%.2f|%d|%s" % (
        ev.get("room"), ev.get("uid"), ev.get("cmd"),
        int((ev.get("ts") or 0) // 60000), " ".join(str(body).split()),
        yuan_of(ev), quantity, ev.get("coin_type") or "")


def yuan_of(ev):
    """把一条事件折算成人民币支出（元）。规则：
    SC 的 price 本身就是元；上舰/金瓜子礼物按 1000 金瓜子 = 1 元；银瓜子礼物为免费。"""
    cmd = ev.get("cmd") or ""
    price = float(ev.get("price") or 0)
    if cmd == "SUPER_CHAT_MESSAGE":
        return price
    if cmd == "GUARD_BUY":
        # 手动补录保存的是总金额；实时与 danmakus 的 price 是单月单价。
        if ev.get("src") == "manual":
            return price / 1000.0
        # B 站实时 GUARD_BUY.data.price 可能是原价；danmakus 的 Guard.priceMilli
        # 是实际单价。跨来源匹配后，优先使用确认过的实付单价。
        if ev.get("paid_price") is not None:
            price = float(ev.get("paid_price") or 0)
        return price * float(ev.get("num") or 1) / 1000.0
    if cmd == "SEND_GIFT":
        if ev.get("free"):
            return 0.0
        coin = float(ev.get("total_coin") or 0) or price * float(ev.get("num") or 1)
        return coin / 1000.0
    return 0.0


def spend_summary(events):
    out = {"total": 0.0, "gift": 0.0, "guard": 0.0, "sc": 0.0, "free_gift_count": 0}
    for e in events:
        cmd = e.get("cmd") or ""
        amount = yuan_of(e)
        out["total"] += amount
        if cmd == "SEND_GIFT":
            out["gift"] += amount
            if e.get("free"):
                out["free_gift_count"] += 1
        elif cmd == "GUARD_BUY":
            out["guard"] += amount
        elif cmd == "SUPER_CHAT_MESSAGE":
            out["sc"] += amount
    return out


_PERSONAL_DEDUPE_MS = 10000


def _collapse_sc(events, window_ms=120000):
    """醒目留言可能是同一笔的重复推送：同房间+金额+文本、120 秒内只留一条。
    主消息优先于日文副本；同为非日文时保留较晚的时间（与消费账单一致）。"""
    rest, kept = [], {}
    for e in sorted(events, key=lambda x: x.get("ts") or 0):
        if e.get("cmd") != "SUPER_CHAT_MESSAGE":
            rest.append(e)
            continue
        text = "".join((e.get("text") or "").split())
        key = (e.get("room"), e.get("uid"), round(float(e.get("price") or 0), 2), text)
        bucket = kept.setdefault(key, [])
        if bucket and (e.get("ts") or 0) - (bucket[-1].get("ts") or 0) <= window_ms:
            prev = bucket[-1]
            if prev.get("jpn") and not e.get("jpn"):
                bucket[-1] = e
            elif not prev.get("jpn") and e.get("jpn"):
                pass
            else:
                bucket[-1] = e
        else:
            bucket.append(e)
    out = rest + [x for b in kept.values() for x in b]
    out.sort(key=lambda e: e.get("ts") or 0)
    return out


def _collapse_personal(events):
    """把账号级通知的广播副本折叠成一条：同 uid+cmd+内容、10 秒内的只留最早一份。"""
    out, seen = [], {}
    for src in events:
        e = src
        if (e.get("cmd") or "").startswith("LIKE_GUIDE"):
            # 旧数据里这类账号通知被记成了具体房间，读取时一并纠正
            e = dict(e)
            e["room"] = 0
            e["personal"] = 1
            if not e.get("text"):
                try:
                    e["text"] = json.loads(e.get("detail") or "{}").get("like_text") or ""
                except Exception:
                    e["text"] = ""
        if e.get("personal"):
            key = (e.get("uid"), e.get("cmd"), e.get("text") or e.get("detail"))
            ts = e.get("ts") or 0
            last = seen.get(key)
            if last is not None and abs(ts - last) <= _PERSONAL_DEDUPE_MS:
                continue
            seen[key] = ts
        out.append(e)
    return out


def _collapse_exact(events):
    """折叠时间戳和语义完全相同的跨来源/同来源重复记录。"""
    groups = defaultdict(list)
    for event in events:
        cmd = str(event.get("cmd") or "")
        if _is_entry_event(event):
            cmd = "ENTRY_EFFECT"
            body = "进入直播间"
        else:
            body = " ".join(str(
                event.get("text") or event.get("gift") or event.get("guard")
                or event.get("interact") or "").split())
        try:
            quantity = int(event.get("num") or 1)
        except (TypeError, ValueError):
            quantity = 1
        key = (
            event.get("room"), event.get("uid"), cmd,
            int(event.get("ts") or 0), body, round(yuan_of(event), 2),
            quantity, event.get("coin_type") or "",
            event.get("manual_id") if event.get("src") == "manual" else "",
        )
        groups[key].append(event)
    priority = {"manual": 0, "live": 1, "backfill": 2, "danmakus": 3}
    out = []
    for items in groups.values():
        items.sort(key=lambda e: priority.get(e.get("src") or "live", 9))
        out.append(items[0])
    out.sort(key=lambda e: e.get("ts") or 0)
    return out


def _collapse_entries(events):
    """折叠 B 站同一次进入产生的 15 秒双推送。"""
    out, groups = [], defaultdict(list)
    for e in events:
        if e.get("src") == "manual" or not _is_entry_event(e):
            out.append(e)
            continue
        key = (e.get("src") or "live", e.get("collector_run") or "",
               e.get("room"), e.get("uid"))
        groups[key].append(e)
    for items in groups.values():
        items.sort(key=lambda e: e.get("ts") or 0)
        clusters, cur = [], [items[0]]
        for e in items[1:]:
            if (e.get("ts") or 0) - (cur[-1].get("ts") or 0) <= _ENTRY_DEDUPE_MS:
                cur.append(e)
            else:
                clusters.append(cur)
                cur = [e]
        clusters.append(cur)
        for cluster in clusters:
            effects = [
                e for e in cluster
                if str(e.get("cmd") or "").startswith("ENTRY_EFFECT")
            ]
            out.append((effects or cluster)[0])
    out.sort(key=lambda e: e.get("ts") or 0)
    return out


def _guard_toast_info(event):
    if event.get("cmd") != "USER_TOAST_MSG":
        return None
    detail = {}
    raw_detail = str(event.get("detail") or "")
    try:
        parsed = json.loads(raw_detail)
        if isinstance(parsed, dict):
            detail = parsed
    except Exception:
        pass

    def number(name):
        value = event.get(name)
        if name == "price" and value is None:
            value = event.get("guard_paid_price")
        if value is None:
            value = detail.get(name)
        if value is None:
            match = re.search(r'"%s"\s*:\s*"?(-?\d+(?:\.\d+)?)' % re.escape(name),
                              raw_detail)
            value = match.group(1) if match else None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    level = number("guard_level")
    paid_price = number("price")
    count = number("num")
    role_name = detail.get("role_name")
    if not role_name:
        match = re.search(r'"role_name"\s*:\s*"([^"]+)"', raw_detail)
        role_name = match.group(1) if match else ""
    guard = GUARD.get(int(level)) if level else None
    guard = guard or role_name or event.get("guard")
    if not guard or paid_price is None or paid_price <= 0:
        return None
    return {
        "room": event.get("room"),
        "uid": event.get("uid"),
        "collector_run": event.get("collector_run") or "",
        "guard": guard,
        "num": max(1, int(count or 1)),
        "paid_price": paid_price,
        "payflow_id": event.get("payflow_id") or detail.get("payflow_id") or "",
        "ts": int(event.get("ts") or 0),
    }


def _apply_local_guard_toast_prices(events):
    """把本地 USER_TOAST_MSG 携带的实际上舰价格配对回 GUARD_BUY。"""
    toasts = defaultdict(list)
    for event in events:
        info = _guard_toast_info(event)
        if not info:
            continue
        key = (info["room"], info["uid"], info["collector_run"],
               info["guard"], info["num"])
        toasts[key].append(info)
    for values in toasts.values():
        values.sort(key=lambda item: item["ts"])

    used = set()
    out = []
    for event in events:
        if (event.get("cmd") != "GUARD_BUY"
                or event.get("src") != "live"
                or event.get("paid_price") is not None):
            out.append(event)
            continue
        try:
            quantity = max(1, int(event.get("num") or 1))
        except (TypeError, ValueError):
            quantity = 1
        key = (event.get("room"), event.get("uid"),
               event.get("collector_run") or "", event.get("guard"), quantity)
        stamp = int(event.get("ts") or 0)
        match, best_diff = None, None
        for info in toasts.get(key, []):
            identity = (key, info["ts"], info["paid_price"], info["payflow_id"])
            if identity in used:
                continue
            diff = abs(info["ts"] - stamp)
            if diff <= 3000 and (best_diff is None or diff < best_diff):
                match, best_diff = info, diff
        if not match:
            out.append(event)
            continue
        identity = (key, match["ts"], match["paid_price"], match["payflow_id"])
        used.add(identity)
        merged = dict(event)
        merged["gross_price"] = float(event.get("price") or 0)
        merged["paid_price"] = match["paid_price"]
        merged["paid_price_src"] = "USER_TOAST_MSG"
        if match["payflow_id"]:
            merged["payflow_id"] = match["payflow_id"]
        out.append(merged)
    return out


def dedupe_events(events):
    """跨来源去重：同一条事件可能被实时流与回补/导入各记一次，只保留一份；
    但同一来源内的重复记录是真实发生的（例如同分钟送两次同样的礼物），必须保留。
    例外：账号级通知（personal）是广播到所有连接的副本，10 秒内同内容只算一条。"""
    events = [event for event in events if event.get("cmd") != "GIFT_POPUP"]
    events = _apply_local_guard_toast_prices(events)
    events = _collapse_exact(events)
    events = _collapse_sc(events)
    events = _collapse_personal(events)
    events = _collapse_entries(events)
    return _merge_observations(events)


def _observation_identity(event):
    cmd = str(event.get("cmd") or "")
    if _is_entry_event(event):
        cmd, body = "ENTRY_EFFECT", "进入直播间"
    else:
        body = " ".join(str(
            event.get("text") or event.get("gift") or event.get("guard")
            or event.get("interact") or "").split())
    try:
        quantity = int(event.get("num") or 1)
    except (TypeError, ValueError):
        quantity = 1
    return (
        event.get("room"), event.get("uid"), cmd, body,
        round(yuan_of(event), 2), quantity, event.get("coin_type") or "",
    )


def _observation_tolerance(a, b):
    if "manual" in (a.get("src"), b.get("src")):
        manual = a if a.get("src") == "manual" else b
        return 30 * 60 * 1000 if manual.get("estimated") else 2 * 60 * 1000
    if _is_entry_event(a) and _is_entry_event(b):
        return _ENTRY_DEDUPE_MS
    return 10000


def _observation_conflict_core(event):
    cmd = str(event.get("cmd") or "")
    body = " ".join(str(
        event.get("text") or event.get("gift") or event.get("guard")
        or event.get("interact") or "").split())
    try:
        quantity = int(event.get("num") or 1)
    except (TypeError, ValueError):
        quantity = 1
    return (event.get("room"), event.get("uid"), cmd, body,
            quantity, event.get("coin_type") or "")


def _with_confirmed_guard_price(local_event, remote_event):
    """保留本地观测，同时采用 danmakus 确认的实际上舰单价。"""
    if (local_event.get("cmd") != "GUARD_BUY"
            or remote_event.get("cmd") != "GUARD_BUY"
            or remote_event.get("src") != "danmakus"):
        return local_event
    local_price = float(local_event.get("price") or 0)
    paid_price = float(remote_event.get("price") or 0)
    if local_price <= 0 or paid_price <= 0:
        return local_event
    if round(yuan_of(local_event), 2) == round(yuan_of(remote_event), 2):
        return local_event
    merged = dict(local_event)
    merged["gross_price"] = local_price
    merged["paid_price"] = paid_price
    merged["paid_price_src"] = "danmakus"
    return merged


def _merge_source_batch(canonical, incoming):
    """把一路采集结果与已选事件一对一匹配，未匹配的重复事件完整保留。"""
    used = set()
    additions = defaultdict(list)
    conflict_index = defaultdict(list)
    for identity, pairs in canonical.items():
        for index, existing in pairs:
            conflict_index[_observation_conflict_core(existing)].append(
                (identity, index, existing))
    for event in sorted(incoming, key=lambda e: e.get("ts") or 0):
        stamp = int(event.get("ts") or 0)
        identity = _observation_identity(event)
        best_index, best_diff = None, None
        best_existing = None
        for index, existing in canonical.get(identity, []):
            if (identity, index) in used:
                continue
            diff = abs(int(existing.get("ts") or 0) - stamp)
            if diff <= _observation_tolerance(existing, event):
                if best_diff is None or diff < best_diff:
                    best_index, best_diff = index, diff
        best_identity = identity
        if best_index is None and event.get("src") != "manual":
            for existing_identity, index, existing in conflict_index.get(
                    _observation_conflict_core(event), []):
                if (existing_identity, index) in used or existing.get("src") == "manual":
                    continue
                # 只有上舰存在“原价字段 vs 实际成交价”的同笔冲突。
                # 普通礼物价格不同就是不同交易，必须分别保留。
                if str(event.get("cmd") or "") != "GUARD_BUY":
                    continue
                diff = abs(int(existing.get("ts") or 0) - stamp)
                if diff <= 3000 and (best_diff is None or diff < best_diff):
                    best_identity, best_index, best_diff = (
                        existing_identity, index, diff)
                    best_existing = existing
        if best_index is None:
            additions[identity].append(event)
        else:
            used.add((best_identity, best_index))
            if best_existing is not None:
                corrected = _with_confirmed_guard_price(best_existing, event)
                if corrected is not best_existing:
                    for position, (index, _) in enumerate(canonical[best_identity]):
                        if index == best_index:
                            canonical[best_identity][position] = (index, corrected)
                            break
    for identity, events in additions.items():
        canonical.setdefault(identity, [])
        for event in events:
            canonical[identity].append((len(canonical[identity]), event))


def _merge_observations(events):
    """按来源优先级合并重复观测，但不同金额、数量或时间的事件不互相覆盖。"""
    by_src = defaultdict(list)
    for event in events:
        by_src[event.get("src") or "live"].append(event)

    batches = [list(by_src.pop("manual", []))]
    for src in ("live", "backfill"):
        sessions = defaultdict(list)
        for event in by_src.pop(src, []):
            sessions[event.get("collector_run") or ""].append(event)
        batches.extend(sorted(sessions.values(), key=len, reverse=True))
    batches.append(list(by_src.pop("danmakus", [])))
    for source in sorted(by_src):
        batches.append(list(by_src[source]))

    canonical = defaultdict(list)
    for batch in batches:
        if not batch:
            continue
        _merge_source_batch(canonical, batch)
    accepted = [event for pairs in canonical.values() for _, event in pairs]
    accepted.sort(key=lambda e: e.get("ts") or 0)
    return accepted


def parse_timeline(s):
    now = datetime.now(TZ)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
        try:
            dt = datetime.strptime(str(s).strip(), fmt)
        except Exception:
            continue
        if fmt == "%H:%M:%S":
            dt = now.replace(hour=dt.hour, minute=dt.minute, second=dt.second, microsecond=0)
        else:
            dt = dt.replace(tzinfo=TZ)
        return int(dt.timestamp() * 1000)
    return int(time.time() * 1000)


def _manual_timestamp(value):
    if value is None or str(value).strip() == "":
        return int(time.time() * 1000)
    if isinstance(value, (int, float)):
        stamp = float(value)
        return int(stamp if stamp > 100000000000 else stamp * 1000)
    text = str(value).strip()
    try:
        stamp = float(text)
        return int(stamp if stamp > 100000000000 else stamp * 1000)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("时间格式应为 YYYY-MM-DD HH:MM:SS 或 ISO 8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp() * 1000)


def _manual_amount(value, default=0.0):
    if value is None or str(value).strip() == "":
        return float(default)
    text = str(value).replace("¥", "").replace("￥", "").replace(",", "").strip()
    try:
        amount = float(text)
    except ValueError as exc:
        raise ValueError("金额必须是数字，单位为元") from exc
    if amount < 0:
        raise ValueError("金额不能为负数")
    return round(amount, 3)


class PossibleDuplicateError(ValueError):
    """手动补录与已有记录高度相似，需要用户确认。"""

    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__("发现疑似重复记录，请确认后再保存。")


def _event_body(event):
    return " ".join(str(
        event.get("text") or event.get("gift") or event.get("guard")
        or event.get("interact") or "").split())


def find_manual_duplicates(candidate, events=None):
    """返回与待补录事件高度相似的已有记录，不自动删除或合并。"""
    if events is None:
        events = dedupe_events(load_events())
    try:
        candidate_uid = int(candidate.get("uid") or 0)
    except (TypeError, ValueError):
        candidate_uid = 0
    cmd = str(candidate.get("cmd") or "")
    body = _event_body(candidate)
    amount = round(yuan_of(candidate), 2)
    stamp = int(candidate.get("ts") or 0)
    matches = []
    for event in events:
        if event.get("cmd") != cmd or event.get("room") != candidate.get("room"):
            continue
        try:
            event_uid = int(event.get("uid") or 0)
        except (TypeError, ValueError):
            event_uid = 0
        if candidate_uid and event_uid and candidate_uid != event_uid:
            continue
        if _event_body(event) != body:
            continue
        if abs(round(yuan_of(event), 2) - amount) > 0.01:
            continue
        estimated = bool(candidate.get("estimated") or event.get("estimated"))
        if cmd == "GUARD_BUY":
            tolerance = 30 * 60 * 1000 if estimated else 2 * 60 * 1000
        elif cmd in ("SEND_GIFT", "SUPER_CHAT_MESSAGE"):
            tolerance = 30 * 60 * 1000 if estimated else 2 * 60 * 1000
        else:
            tolerance = 30 * 60 * 1000 if estimated else 10 * 1000
        diff = abs(int(event.get("ts") or 0) - stamp)
        if diff > tolerance:
            continue
        matches.append({
            "ts": event.get("ts"),
            "src": event.get("src") or "live",
            "num": event.get("num"),
            "amount": round(yuan_of(event), 2),
            "note": event.get("note") or "",
            "diff_ms": diff,
        })
    matches.sort(key=lambda item: item["diff_ms"])
    return matches[:10]


def append_manual_event(payload, confirm_duplicate=False):
    """校验并追加一条本人手动补录，供客户端和本地 HTTP API 共用。"""
    if not isinstance(payload, dict):
        raise ValueError("补录内容必须是 JSON 对象")
    room = payload.get("room", payload.get("roomid"))
    try:
        room = int(room)
    except (TypeError, ValueError) as exc:
        raise ValueError("room 必须是有效的直播间号") from exc
    if room <= 0:
        raise ValueError("room 必须大于 0")

    kind = str(payload.get("type") or payload.get("event_type") or "").strip().lower()
    aliases = {
        "弹幕": "danmu", "礼物": "gift", "醒目留言": "sc", "上舰": "guard",
        "进入": "entry", "进入直播间": "entry",
        "super_chat": "sc", "superchat": "sc",
    }
    kind = aliases.get(kind, kind)
    cmd_map = {
        "DANMU_MSG": "danmu", "SEND_GIFT": "gift",
        "SUPER_CHAT_MESSAGE": "sc", "GUARD_BUY": "guard",
        "INTERACT_WORD": "entry", "ENTRY_EFFECT": "entry",
    }
    cmd = str(payload.get("cmd") or "").upper()
    kind = cmd_map.get(cmd, kind)
    if kind not in ("danmu", "gift", "sc", "guard", "entry"):
        raise ValueError("type 必须是 danmu/gift/sc/guard/entry 或对应中文名称")

    content = payload.get("content")
    if content is None:
        content = (payload.get("text") or payload.get("gift")
                   or payload.get("guard") or payload.get("interact"))
    content = str(content or "").strip()
    if kind in ("danmu", "gift", "sc") and not content:
        raise ValueError("弹幕、礼物和醒目留言必须填写内容或名称")
    if kind == "guard":
        content = content or "舰长"
        if content not in GUARD.values():
            raise ValueError("上舰类型必须是舰长、提督或总督")
    if kind == "entry":
        content = content or "进入直播间"

    quantity = payload.get("quantity", payload.get("num", 1))
    try:
        quantity = int(quantity)
    except (TypeError, ValueError) as exc:
        raise ValueError("数量必须是正整数") from exc
    if quantity <= 0:
        raise ValueError("数量必须大于 0")

    defaults = {"gift": 0.0, "sc": 0.0, "guard": 138.0, "danmu": 0.0, "entry": 0.0}
    amount = _manual_amount(payload.get("amount", payload.get("price")),
                            defaults[kind])
    mid = my_mid()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            nav = (json.load(fh).get("nav") or {})
        uname = nav.get("uname") or ""
    except Exception:
        uname = ""

    ev = {
        "ts": _manual_timestamp(payload.get("time", payload.get("ts"))),
        "room": room,
        "uid": mid,
        "uname": uname,
        "src": "manual",
        "estimated": int(bool(payload.get("estimated", 0))),
        "manual_id": uuid.uuid4().hex,
        "note": str(payload.get("note") or "客户端手动补录")[:500],
    }
    if kind == "danmu":
        ev.update(cmd="DANMU_MSG", text=content)
    elif kind == "gift":
        total_coin = round(amount * 1000)
        ev.update(cmd="SEND_GIFT", gift=content, num=quantity,
                  price=round(total_coin / quantity), total_coin=total_coin,
                  coin_type="gold", free=0)
    elif kind == "sc":
        ev.update(cmd="SUPER_CHAT_MESSAGE", text=content, price=amount)
    elif kind == "guard":
        ev.update(cmd="GUARD_BUY", guard=content, num=quantity,
                  price=round(amount * 1000))
    else:
        ev.update(cmd="INTERACT_WORD", interact=content)

    if not confirm_duplicate:
        duplicates = find_manual_duplicates(ev)
        if duplicates:
            raise PossibleDuplicateError(duplicates)

    line = json.dumps(ev, ensure_ascii=False) + "\n"
    with _manual_append_lock:
        with open(EVENTS_FILE, "a", encoding="utf-8", buffering=1) as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    return ev


def manual_schema():
    return {
        "endpoint": "/api/manual",
        "method": "POST",
        "content_type": "application/json",
        "types": ["danmu", "gift", "sc", "guard", "entry"],
        "fields": {
            "time": "YYYY-MM-DD HH:MM:SS，默认当前时间",
            "room": "直播间号，必填",
            "type": "danmu/gift/sc/guard/entry，也可写中文",
            "content": "弹幕内容、礼物名称、SC 内容或舰长类型",
            "amount": "金额（元），物品按总额填写",
            "quantity": "数量，默认 1",
            "note": "补录备注",
            "confirm_duplicate": "发现疑似重复后，确认仍要保存时传 true",
        },
        "example": {
            "time": "2026-11-05 20:30:00",
            "room": 80397,
            "type": "gift",
            "content": "粉丝团灯牌",
            "amount": 0.1,
            "quantity": 1,
            "note": "手机账单补录",
        },
    }


def fetch_backfill(rid, ck, mid):
    """回补直播间最近约 10 分钟的弹幕，返回（本人事件，是否成功）。"""
    events = []
    ok = True
    try:
        j = get("https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory?roomid=%d" % rid, ck)
        if j.get("code") not in (None, 0):
            raise RuntimeError("接口返回 %s：%s" % (
                j.get("code"), j.get("message") or "未知错误"))
        data = j.get("data") or {}
        items = (data.get("room") or []) + (data.get("admin") or [])
    except Exception as exc:
        print("[%s] 最近弹幕回补失败：%s" % (rid, exc))
        print("[%s] 断线期间的礼物、SC、上舰需要等待 danmakus 增量同步补齐。" % rid)
        ok = False
        items = []
    for it in items:
        text = (it.get("text") or "").strip()
        if not text or it.get("uid") != mid:
            continue
        events.append({"ts": parse_timeline(it.get("timeline")), "room": rid, "cmd": "DANMU_MSG",
                       "uid": it.get("uid"), "uname": it.get("nickname"), "text": text,
                       "src": "backfill"})
    return events, ok


async def watch(rid, ck, mid, out, stop):
    host_index = 0
    fail_attempt = 0
    while not stop["done"]:
        try:
            info = (live_api("/xlive/web-room/v1/index/getDanmuInfo", {"id": rid, "type": 0}, ck).get("data") or {})
            hosts = info.get("host_list") or []
            token = info.get("token")
            if not hosts or not token:
                await asyncio.sleep(30)
                continue
            h = hosts[host_index % len(hosts)]
            url = "wss://%s:%s/sub" % (h["host"], h["wss_port"])
            # 关闭客户端库自带的 ping 保活：B 站用协议内的 op=2 心跳，
            # 库的 ping 常被判超时而误重连，造成采集盲区。
            async with websockets.connect(url, max_size=None, open_timeout=20,
                                          ping_interval=None, close_timeout=5) as ws:
                auth = {"uid": mid, "roomid": rid, "protover": 2, "platform": "web",
                        "type": 2, "key": token}
                await ws.send(pack(7, json.dumps(auth).encode()))
                fail_attempt = 0
                print("[%s] 已连接弹幕流" % rid)
                backfill_events, backfill_ok = await asyncio.to_thread(
                    fetch_backfill, rid, ck, mid)
                for ev in backfill_events:
                    out.write(ev)
                print("[%s] %s" % (
                    rid, "已回补最近弹幕" if backfill_ok
                    else "最近弹幕回补失败，已记录原因"))
                last_hb = time.time()
                received = mine_count = others = 0
                last_stat = time.time()
                session_start = time.time()
                while not stop["done"]:
                    if time.time() - session_start > 1800:  # 每 30 分钟主动换一次连接
                        host_index += 1
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw:
                        _health["last_frame"] = time.time()
                        for op, body in unpack(raw):
                            if op != 5:
                                others += 1
                                continue
                            try:
                                msg = json.loads(body.decode("utf-8", "replace"))
                            except Exception:
                                continue
                            received += 1
                            ev = normalize(msg, rid)
                            if (ev.get("cmd") and keep_event(ev, mid)
                                    and not _personal_dup(ev)
                                    and not _entry_dup(ev)):
                                out.write(ev)
                                mine_count += 1
                                body = (ev.get("text") or ev.get("gift") or ev.get("guard")
                                        or ev.get("interact") or "")
                                print("[我] room=%s %s %s" % (rid, ev["cmd"], body))
                    if time.time() - last_stat > 60:
                        print("[%s] 近 60 秒收到广播 %d 条（其他帧 %d），其中本人 %d 条"
                              % (rid, received, others, mine_count))
                        received = mine_count = 0
                        last_stat = time.time()
                    if time.time() - last_hb > 25:
                        await ws.send(pack(2))
                        last_hb = time.time()
        except Exception as exc:
            if not stop["done"]:
                host_index += 1
                fail_attempt += 1
                delay = min(30, 2 ** min(fail_attempt, 5))
                print("[%s] 连接异常，%d 秒后切换节点重连：%s"
                      % (rid, delay, exc))
                await asyncio.sleep(delay)


async def watch_dynamic(rooms, ck, mid, out, stop, interval, cap):
    """定期刷新开播状态，只连接正在直播的房间。"""
    uids = [r["uid"] for r in rooms if r.get("uid")]
    tasks = {}
    while not stop["done"]:
        status = {}
        try:
            status = await asyncio.to_thread(resolve_rooms, uids, ck)
        except Exception as exc:
            print("刷新开播状态失败：%s" % exc)
        live = sorted([v for v in status.values() if v.get("live_status") == 1],
                      key=lambda x: -(x.get("online") or 0))
        if cap > 0:
            live = live[:cap]
        want = {v["room_id"]: v for v in live}
        for rid in list(tasks):
            if rid not in want:
                tasks.pop(rid).cancel()
        for rid, v in want.items():
            if rid not in tasks:
                print("开播中：%s(room %s) 在线 %s" % (v.get("uname"), rid, v.get("online")))
                tasks[rid] = asyncio.create_task(watch(rid, ck, mid, out, stop))
                _health["last_frame"] = time.time()  # 新连接宽限期，避免健康检查误判
        _health["live"] = len(tasks)
        _health["cycles"] += 1
        print("当前跟踪 %d 个直播间，%d 秒后刷新。" % (len(tasks), interval))
        await asyncio.sleep(interval)
    for t in tasks.values():
        t.cancel()


async def run_collect(rooms, ck, mid):
    stop = {"done": False}
    out = Sink(EVENTS_FILE, _limit["rotate_mb"], _limit["keep"])
    try:
        tasks = [asyncio.create_task(
            watch_dynamic(rooms, ck, mid, out, stop,
                          _limit["interval"], _limit["cap"]))]
        tasks.append(asyncio.create_task(heartbeat(stop)))
        try:
            seconds = _limit["seconds"]
            if seconds:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=seconds)
                except asyncio.TimeoutError:
                    print("已到 %s 秒，停止采集。" % seconds)
            else:
                await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        stop["done"] = True
        for t in tasks:
            t.cancel()
    finally:
        out.close()


def _health_problem():
    """返回需要主动退出重启的原因；健康时返回 None。"""
    now = time.time()
    if now - _health["start"] > 600 and _health["cycles"] == 0:
        return "启动 10 分钟仍未完成第一轮刷新（疑似卡死在网络调用）"
    if _health["live"] > 0 and now - _health["last_frame"] > 300:
        return "有 %d 个在线房间，却已连续 5 分钟收不到任何服务器消息（疑似连接失效）" % _health["live"]
    return None


async def heartbeat(stop):
    """每 30 秒刷新心跳文件（内容：本进程 PID + 时间戳）。
    界面据此判断采集器是否真的活着，并直接读出正确 PID（系统会复用 PID，不能只靠进程号）。
    同时做健康自检：假活状态主动退出，交由守护进程重启。"""
    pid = os.getpid()
    while not stop["done"]:
        reason = _health_problem()
        if reason:
            print("[健康检查] %s，主动退出，等待守护进程重启。" % reason)
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os._exit(3)
        try:
            with open(ALIVE_FILE, "w", encoding="utf-8") as fh:
                fh.write("%d %d\n" % (pid, int(time.time())))
        except OSError:
            pass
        await asyncio.sleep(30)


_limit = {"seconds": 0, "follow": False, "rooms": [], "interval": 60, "cap": 0,
          "all": False,
          "rotate_mb": 256, "keep": 0}


def cmd_collect():
    if not acquire_collector_lock():
        print("已有采集器正在运行，本进程退出，避免重复写入数据。")
        return 0
    argv = sys.argv[1:]
    if "--seconds" in argv:
        _limit["seconds"] = int(argv[argv.index("--seconds") + 1])
    if "--follow" in argv:
        _limit["follow"] = True
    if "--rooms" in argv:
        value = argv[argv.index("--rooms") + 1]
        _limit["rooms"] = [
            int(part) for part in value.split(",") if part.strip().isdigit()]
    if "--interval" in argv:
        _limit["interval"] = int(argv[argv.index("--interval") + 1])
    if "--cap" in argv:
        _limit["cap"] = int(argv[argv.index("--cap") + 1])
    if "--all" in argv:
        _limit["all"] = True
    if "--rotate-mb" in argv:
        _limit["rotate_mb"] = int(argv[argv.index("--rotate-mb") + 1])
    if "--keep" in argv:
        _limit["keep"] = int(argv[argv.index("--keep") + 1])
    ck = cookie()
    mid = my_mid()
    if not os.path.isfile(ROOMS_FILE):
        print("没有 rooms.json，请先运行 discover 或 add。")
        return 2
    with open(ROOMS_FILE, "r", encoding="utf-8") as fh:
        all_rooms = json.load(fh).get("rooms", [])
    if _limit["rooms"]:
        selected = set(_limit["rooms"])
        rooms = [r for r in all_rooms if int(r.get("roomid") or 0) in selected]
    elif _limit["follow"]:
        rooms = [r for r in all_rooms if r.get("uid")]
    else:
        print("没有指定采集模式：请使用 --follow 或 --rooms。")
        return 2
    if not rooms:
        print("没有找到可采集的直播间。")
        return 2
    if _limit["rooms"]:
        print("手动采集 %d 个直播间（识别开播后连接）：%s"
              % (len(rooms), ", ".join(str(r["roomid"]) for r in rooms)))
    else:
        print("开始跟踪关注列表中的 %d 个直播间（自动识别开播），Ctrl+C 停止。" % len(rooms))
    try:
        asyncio.run(run_collect(rooms, ck, mid))
    except KeyboardInterrupt:
        pass
    print("已停止，数据在 %s" % EVENTS_FILE)
    return 0


CSS = """
:root{--bg:#12141c;--card:#1b1f2b;--line:#2b3145;--fg:#e9eefb;--dim:#8f9dbb;--pink:#fb7299;--blue:#00aeec;}
*{box-sizing:border-box}
body{margin:0;padding:26px;background:var(--bg);color:var(--fg);
font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
h1{margin:0 0 4px;font-size:23px}
h2{margin:30px 0 10px;font-size:16px;border-left:3px solid var(--pink);padding-left:10px}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:19px;font-weight:600;margin-top:4px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-top:12px}
.row{display:grid;grid-template-columns:210px 1fr 60px;gap:10px;align-items:center;margin:6px 0}
.row .label{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{height:10px;background:#0d1017;border-radius:6px;overflow:hidden}
.fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--pink))}
.num{text-align:right;font-size:12px;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
thead th{color:var(--dim);font-weight:500}
.wrap{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.note{color:var(--dim);font-size:12px;margin-top:10px}
"""


def bars(counter, top=15):
    if not counter:
        return '<div class="note">无数据</div>'
    total = sum(counter.values())
    rows = []
    for name, n in counter.most_common(top):
        rows.append('<div class="row"><div class="label">%s</div><div class="track">'
                    '<div class="fill" style="width:%.1f%%"></div></div><div class="num">%d</div></div>'
                    % (str(name).replace("<", "&lt;"), n * 100.0 / total, n))
    return "".join(rows)


class Sink:
    """事件落盘 + 按大小归档；历史分片只增不删。"""

    def __init__(self, path, limit_mb, keep=0):
        self.path = path
        self.limit = limit_mb * 1024 * 1024
        # 保留参数用于旧配置兼容；数据分片不再自动删除。
        self.keep = keep
        # buffering=1 行缓冲：每条事件立即落盘。
        # 否则进程被强杀时，缓冲区里未 flush 的记录会直接丢失。
        self.fh = open(path, "a", encoding="utf-8", buffering=1)
        self.checked = 0.0
        self.run_id = uuid.uuid4().hex[:12]

    def write(self, ev):
        now = time.time()
        if self.limit and now - self.checked > 60:
            self.checked = now
            self._maybe_rotate()
        if (ev.get("src") or "live") in ("live", "backfill") and not ev.get("collector_run"):
            ev = dict(ev)
            ev["collector_run"] = self.run_id
        self.fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def _base(self):
        return self.path[:-6] if self.path.endswith(".jsonl") else self.path

    def _maybe_rotate(self):
        try:
            self.fh.flush()
            if os.path.getsize(self.path) < self.limit:
                return
        except OSError:
            return
        try:
            self.fh.close()
            stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
            target = "%s.%s.jsonl" % (self._base(), stamp)
            suffix = 1
            while os.path.exists(target):
                target = "%s.%s-%d.jsonl" % (self._base(), stamp, suffix)
                suffix += 1
            os.replace(self.path, target)
            self.fh = open(self.path, "a", encoding="utf-8", buffering=1)
            print("数据分片已归档 -> %s" % target)
        except Exception as exc:
            print("数据分片归档失败：%s" % exc)
            try:
                if self.fh.closed:
                    self.fh = open(self.path, "a", encoding="utf-8", buffering=1)
            except Exception as reopen_exc:
                print("数据分片重新打开失败：%s" % reopen_exc)

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


def load_events():
    events = []
    import glob
    base = EVENTS_FILE[:-6] if EVENTS_FILE.endswith(".jsonl") else EVENTS_FILE
    paths = [HISTORY_FILE, DANMAKUS_V3_FILE, DANMAKUS_V3_PRICE_FIX_FILE]
    paths += sorted(glob.glob("%s.jsonl" % base)) + sorted(glob.glob("%s.*.jsonl" % base))
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("cmd") != "GIFT_POPUP":
                        events.append(event)
                except Exception:
                    continue
    return events


def src_compare(events):
    """实时流与回补数据的交叉校验：谁多谁少、重叠多少。"""
    live_keys, back_keys = set(), set()
    for e in events:
        k = event_key(e)
        (back_keys if e.get("src") == "backfill" else live_keys).add(k)
    return {
        "live": len(live_keys),
        "backfill": len(back_keys),
        "both": len(live_keys & back_keys),
        "backfill_only": len(back_keys - live_keys),
        "live_only": len(live_keys - back_keys),
    }


def cmd_verify():
    events = load_events()
    s = src_compare(events)
    print("事件记录总数（未去重）：%d" % len(events))
    print("实时流弹幕唯一键：%d" % s["live"])
    print("回补弹幕唯一键：%d" % s["backfill"])
    print("两路重叠：%d" % s["both"])
    print("仅回补有（实时流可能漏记）：%d" % s["backfill_only"])
    print("仅实时流有（超出回补的最近窗口）：%d" % s["live_only"])
    return 0


def _legacy_report():
    mid = my_mid()
    acct = {}
    if os.path.isfile(ACCOUNT_FILE):
        with open(ACCOUNT_FILE, "r", encoding="utf-8") as fh:
            acct = json.load(fh)
    events = load_events()
    src = src_compare(events)
    seen, unique = set(), []
    for e in events:
        k = event_key(e)
        if k in seen:
            continue
        seen.add(k)
        unique.append(e)
    events = unique
    mine = [e for e in events if e.get("uid") == mid]
    my_danmu = [e for e in mine if e["cmd"] == "DANMU_MSG"]
    my_gift = [e for e in mine if e["cmd"] == "SEND_GIFT"]
    my_guard = [e for e in mine if e["cmd"] == "GUARD_BUY"]
    my_sc = [e for e in mine if e["cmd"] == "SUPER_CHAT_MESSAGE"]
    my_inter = [e for e in mine if e["cmd"].startswith(("INTERACT_WORD", "ENTRY_EFFECT"))]
    by_room = Counter(e.get("room") for e in mine if not e["cmd"].startswith("MESSAGEBOX"))
    all_by_room = Counter(e.get("room") for e in events)
    sp = spend_summary(mine)
    names = {}
    if os.path.isfile(ROOMS_FILE):
        with open(ROOMS_FILE, "r", encoding="utf-8") as fh:
            for r in json.load(fh).get("rooms", []):
                names[r["roomid"]] = r.get("uname") or ""
    rn = lambda rid: "账号通知" if rid == 0 else "%s(%s)" % (names.get(rid) or "房间", rid)
    w = acct.get("wealth_info") or {}
    cards = "".join(
        '<div class="card"><div class="k">%s</div><div class="v">%s</div></div>' % (k, v)
        for k, v in [
            ("采集事件总数", len(events)),
            ("我的事件", len(mine)),
            ("我的弹幕", len(my_danmu)),
            ("我的礼物", len(my_gift)),
            ("我的上舰", len(my_guard)),
            ("醒目留言", len(my_sc)),
            ("我的进房/互动", len(my_inter)),
            ("覆盖直播间", len(all_by_room)),
            ("B币", acct.get("billCoin", "-")),
            ("直播等级", "LV%s" % acct.get("user_level", "-")),
            ("财富等级", "LV%s" % w.get("level", "-")),
            ("财富累计分", w.get("level_total_score", "-")),
            ("总支出(元)", "%.2f" % sp["total"]),
            ("礼物支出", "%.2f" % sp["gift"]),
            ("上舰支出", "%.2f" % sp["guard"]),
            ("SC 支出", "%.2f" % sp["sc"]),
            ("免费礼物", sp["free_gift_count"]),
            ("实时流弹幕", src["live"]),
            ("回补弹幕", src["backfill"]),
            ("两路重叠", src["both"]),
            ("仅回补有(可能漏记)", src["backfill_only"]),
        ]
    )
    rows = []
    for e in sorted(mine, key=lambda x: -x.get("ts", 0))[:200]:
        t = datetime.fromtimestamp(e.get("ts", 0) / 1000.0, TZ).strftime("%m-%d %H:%M:%S")
        what = e.get("text") or e.get("gift") or e.get("guard") or e.get("interact") or e["cmd"]
        extra = ""
        if e["cmd"] == "SEND_GIFT":
            extra = "x%s" % e.get("num")
        elif e["cmd"] == "GUARD_BUY":
            extra = "%s 月" % e.get("num")
        amount = yuan_of(e)
        if amount > 0:
            extra += "  ¥%.2f" % amount
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (t, rn(e.get("room")), e["cmd"], str(what).replace("<", "&lt;") + extra))
    html_out = (
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>直播行为自查报告</title><style>%s</style></head><body>"
        "<h1>直播行为自查报告 · %s</h1>"
        "<div class='sub'>UID %s · 生成于 %s · 数据来自本人实时采集的公开弹幕流</div>"
        "<div class='cards'>%s</div>"
        "<h2>我参与最多的直播间</h2><div class='panel'>%s</div>"
        "<h2>全部事件分布（按直播间）</h2><div class='panel'>%s</div>"
        "<h2>数据来源对照</h2><div class='panel'>"
        "实时流弹幕 %d ｜ 回补弹幕 %d ｜ 两路重叠 %d ｜ 仅回补有（实时流可能漏记）%d ｜ "
        "仅实时流有（回补窗口之外）%d</div>"
        "<h2>我的明细</h2><div class='wrap'><table><thead><tr><th>时间</th><th>直播间</th>"
        "<th>类型</th><th>内容</th></tr></thead><tbody>%s</tbody></table></div>"
        "<div class='note'>弹幕流为实时数据，仅覆盖开始采集之后的时间段。</div>"
        "</body></html>" % (CSS, acct.get("uname", ""), mid,
                            datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), cards,
                            bars(by_room, 15), bars(all_by_room, 15),
                            src["live"], src["backfill"], src["both"], src["backfill_only"],
                            src["live_only"], "".join(rows))
    )
    with open(REPORT_FILE, "w", encoding="utf-8") as fh:
        fh.write(html_out)
    print("采集事件 %d 条，其中我的 %d 条（弹幕 %d / 礼物 %d / 上舰 %d）"
          % (len(events), len(mine), len(my_danmu), len(my_gift), len(my_guard)))
    print("已生成 %s" % REPORT_FILE)
    print("总支出 ¥%.2f（礼物 %.2f / 上舰 %.2f / SC %.2f）"
          % (sp["total"], sp["gift"], sp["guard"], sp["sc"]))
    return 0


def cmd_report():
    """生成互动画像报告（版式参照 danmakus 的分析页）。"""
    mid = my_mid()
    acct = {}
    if os.path.isfile(ACCOUNT_FILE):
        with open(ACCOUNT_FILE, "r", encoding="utf-8") as fh:
            acct = json.load(fh)
    avatar_path = os.path.join(HERE, "account_avatar_%s_96.png" % mid)
    if os.path.isfile(avatar_path):
        try:
            import base64
            with open(avatar_path, "rb") as fh:
                acct["_avatar_data_uri"] = (
                    "data:image/png;base64," +
                    base64.b64encode(fh.read()).decode("ascii"))
        except Exception:
            pass
    uniq = dedupe_events(load_events())
    for e in uniq:
        e["_yuan"] = yuan_of(e)
    names = {}
    room_uids = {}
    if os.path.isfile(ROOMS_FILE):
        with open(ROOMS_FILE, "r", encoding="utf-8") as fh:
            for r in json.load(fh).get("rooms", []):
                names[r["roomid"]] = r.get("uname") or ("房间 %s" % r["roomid"])
                if r.get("uid"):
                    room_uids[r["roomid"]] = int(r["uid"])
    # 历史数据里带着完整的频道名，补上关注列表之外的直播间
    hist = os.path.join(HERE, "danmakus_history.json")
    if os.path.isfile(hist):
        try:
            with open(hist, "r", encoding="utf-8") as fh:
                for ch in json.load(fh).get("channels", []):
                    c = ch.get("channel") or {}
                    if c.get("roomId") and c.get("uName"):
                        names[c["roomId"]] = c["uName"]
        except Exception:
            pass
    snapshot_path = os.path.join(DATA_DIR, "danmakus_v3_snapshot.json")
    if os.path.isfile(snapshot_path):
        try:
            with open(snapshot_path, "r", encoding="utf-8") as fh:
                for item in json.load(fh).get("items", []):
                    c = item.get("channel") or {}
                    if c.get("roomId") and c.get("uName"):
                        names[c["roomId"]] = c["uName"]
        except Exception:
            pass
    medal_by_uid = {}
    for event in uniq:
        if not str(event.get("cmd") or "").startswith("MESSAGEBOX"):
            continue
        detail = str(event.get("detail") or "")
        uid_match = re.search(r'"up_uid"\s*:\s*(\d+)', detail)
        name_match = re.search(r'"medal_name"\s*:\s*"([^"]+)"', detail)
        if uid_match and name_match:
            medal_by_uid[int(uid_match.group(1))] = name_match.group(1)
    medal_names = {
        roomid: medal_by_uid[uid]
        for roomid, uid in room_uids.items() if uid in medal_by_uid
    }
    pages = live_report_view.render_pages(
        uniq, acct, names, mid, medal_names=medal_names)
    report_paths = []
    for filename, html in pages.items():
        path = os.path.join(HERE, filename)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        report_paths.append(path)
    sp = spend_summary(uniq)
    print("记录 %d 条（弹幕 %d / 礼物 %d / 上舰 %d / SC %d）"
          % (len(uniq),
             sum(1 for e in uniq if e["cmd"] == "DANMU_MSG"),
             sum(1 for e in uniq if e["cmd"] == "SEND_GIFT"),
             sum(1 for e in uniq if e["cmd"] == "GUARD_BUY"),
             sum(1 for e in uniq if e["cmd"] == "SUPER_CHAT_MESSAGE")))
    print("已生成 %s" % REPORT_FILE)
    print("时间分类页面：月 / 年 / 生涯已生成。")
    print("总支出 ¥%.2f" % sp["total"])
    try:
        if webbrowser.open(Path(REPORT_FILE).as_uri()):
            print("已在浏览器中打开报告。")
    except Exception as exc:
        print("自动打开报告失败：%s" % exc)
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "snapshot":
        return cmd_snapshot()
    if cmd == "discover":
        return cmd_discover()
    if cmd == "add" and len(argv) > 1:
        return cmd_add(int(argv[1]))
    if cmd == "collect":
        return cmd_collect()
    if cmd == "report":
        return cmd_report()
    if cmd == "sync-danmakus":
        import danmaku_history_sync
        return danmaku_history_sync.main()
    if cmd == "verify":
        return cmd_verify()
    print("未知命令：%s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
