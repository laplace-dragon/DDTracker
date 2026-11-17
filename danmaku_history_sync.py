#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 danmakus v3 用户历史与本地采集数据校准，只补本地缺失事件。"""

import hashlib
import json
import os
import ctypes
import time
import threading
import urllib.request
from collections import Counter, defaultdict
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime, timezone, timedelta

import bili_live as L

TZ = timezone(timedelta(hours=8))
API_BASE = "https://api.ukamnads.icu/api/v3/users/%d/history"
SNAPSHOT_FILE = os.path.join(L.DATA_DIR, "danmakus_v3_snapshot.json")
AUDIT_FILE = os.path.join(L.DATA_DIR, "danmakus_v3_sync_audit.json")
REMOTE_EVENTS_FILE = os.path.join(L.DATA_DIR, "danmakus_v3_remote_events.jsonl")
STATE_FILE = os.path.join(L.DATA_DIR, "danmakus_v3_sync_state.json")
PAGE_SIZE = 20
INCREMENTAL_LOOKBACK_MS = 24 * 60 * 60 * 1000
EVENT_CLOCK_SKEW_MS = 10 * 60 * 1000
MAX_INCREMENTAL_PAGES = 60
DAILY_SYNC_LIMIT = 5
QUOTA_FILE = os.path.join(L.HERE, "danmaku_sync_quota.json")
QUOTA_MUTEX = "Local\\BiliLiveWatch.DanmakuSyncQuota.v1"
SYNC_MUTEX = "Local\\BiliLiveWatch.DanmakuSync.v1"
_quota_thread_lock = threading.Lock()
_sync_thread_lock = threading.Lock()


class DailySyncLimitExceeded(RuntimeError):
    """当日同步次数已经用完。"""


def _named_mutex(name, thread_lock):
    @contextmanager
    def guard():
        with thread_lock:
            handle = None
            locked = False
            try:
                try:
                    kernel32 = ctypes.windll.kernel32
                    kernel32.CreateMutexW.argtypes = [
                        ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
                    kernel32.CreateMutexW.restype = wintypes.HANDLE
                    kernel32.WaitForSingleObject.argtypes = [
                        wintypes.HANDLE, wintypes.DWORD]
                    kernel32.WaitForSingleObject.restype = wintypes.DWORD
                    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
                    kernel32.ReleaseMutex.restype = wintypes.BOOL
                    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel32.CloseHandle.restype = wintypes.BOOL
                    handle = kernel32.CreateMutexW(None, False, name)
                    if handle:
                        result = kernel32.WaitForSingleObject(handle, 10000)
                        if result not in (0x00000000, 0x00000080):
                            raise TimeoutError("等待同步锁超时")
                        locked = True
                except AttributeError:
                    handle = None
                yield
            finally:
                if handle:
                    if locked:
                        kernel32.ReleaseMutex(handle)
                    kernel32.CloseHandle(handle)
    return guard()


def _quota_lock():
    """串行化同一进程和多进程对每日额度的读取与写入。"""
    return _named_mutex(QUOTA_MUTEX, _quota_thread_lock)


def _sync_lock():
    """同一时间只允许一份 danmaku 同步任务写入本地文件。"""
    return _named_mutex(SYNC_MUTEX, _sync_thread_lock)


def _consume_daily_quota(today=None):
    """在执行同步前占用一次额度；日期按香港时区计算。"""
    day = today or datetime.now(TZ).date().isoformat()
    with _quota_lock():
        try:
            with open(QUOTA_FILE, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except Exception:
            saved = {}
        used = 0
        if isinstance(saved, dict) and saved.get("date") == day:
            try:
                used = max(0, int(saved.get("used") or 0))
            except (TypeError, ValueError):
                used = 0
        if used >= DAILY_SYNC_LIMIT:
            raise DailySyncLimitExceeded("已到当日使用上限")
        used += 1
        _atomic_json(QUOTA_FILE, {"date": day, "used": used})
        return used


def _atomic_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_jsonl(path, events):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", buffering=1) as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _read_jsonl(path):
    events = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    except FileNotFoundError:
        pass
    return events


def _event_key(event):
    return str(event.get("remote_key") or "") or hashlib.sha1(json.dumps(
        [event.get("live_id"), event.get("ts"), event.get("cmd"),
         event.get("room"), event.get("text"), event.get("gift"),
         event.get("guard"), event.get("price"), event.get("total_coin")],
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _live_id(item):
    return str((item.get("live") or {}).get("liveId") or "")


def _live_start(item):
    try:
        return int((item.get("live") or {}).get("startDate") or 0)
    except (TypeError, ValueError):
        return 0


def _live_stop(item):
    try:
        return int((item.get("live") or {}).get("stopDate") or 0)
    except (TypeError, ValueError):
        return 0


def _item_key(item):
    live_id = _live_id(item)
    if live_id:
        return live_id
    live = item.get("live") or {}
    channel = item.get("channel") or {}
    return "%s:%s" % (channel.get("roomId"), live.get("startDate"))


def _merge_items(old_items, new_items):
    merged = {}
    for item in list(old_items or []) + list(new_items or []):
        key = _item_key(item)
        if not key:
            continue
        current = merged.get(key)
        if current is None or len(json.dumps(item, ensure_ascii=False)) >= len(
                json.dumps(current, ensure_ascii=False)):
            merged[key] = item
    return sorted(merged.values(), key=_live_start, reverse=True)


def _merge_events(old_events, new_events):
    merged = {}
    for event in list(old_events or []) + list(new_events or []):
        key = _event_key(event)
        merged[key] = event
    return sorted(merged.values(), key=lambda e: e.get("ts") or 0)


def _load_state(uid):
    state = _read_json(STATE_FILE, {})
    if isinstance(state, dict) and state.get("user_id") == uid:
        return state
    snapshot = _read_json(SNAPSHOT_FILE, {})
    audit = _read_json(AUDIT_FILE, {})
    for value in (audit.get("fetched_at"), snapshot.get("fetched_at")):
        if value:
            return {"user_id": uid, "last_sync_at": value,
                    "migrated_from_existing": True}
    return {}


def resolve_uid(uid=None):
    if uid not in (None, ""):
        return int(uid)
    try:
        return int(L.my_mid())
    except Exception:
        pass
    try:
        with open(L.COOKIE_FILE, "r", encoding="utf-8") as fh:
            for part in fh.read().split(";"):
                key, _, value = part.strip().partition("=")
                if key == "DedeUserID" and value:
                    return int(value)
    except Exception:
        pass
    raise RuntimeError("无法确定当前登录用户 UID，请先扫码登录。")


def _fetch_page(uid, page):
    url = "%s?page=%d&pageSize=%d&hideOnlyEnter=true" % (
        API_BASE % uid, page, PAGE_SIZE)
    last_error = None
    for attempt in range(1, 6):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Connection": "close"})
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
            data = json.loads(raw.decode("utf-8"))
            if data.get("code") != 200:
                raise RuntimeError(data.get("message") or "接口返回错误")
            return data["data"]
        except Exception as exc:
            last_error = exc
            time.sleep(attempt)
    raise RuntimeError("第 %d 页连续失败：%s" % (page, last_error))


def fetch_history(uid=None, progress=None):
    uid = resolve_uid(uid)
    items, seen_lives = [], set()
    api_total, page, has_more = None, 1, True
    while has_more:
        data = _fetch_page(uid, page)
        api_total = data.get("total", api_total)
        batch = data.get("items") or []
        for item in batch:
            live_id = (item.get("live") or {}).get("liveId")
            key = live_id or "%s:%s" % (
                (item.get("channel") or {}).get("roomId"),
                (item.get("live") or {}).get("startDate"))
            if key not in seen_lives:
                seen_lives.add(key)
                items.append(item)
        if progress:
            progress("历史分页 %d：本页 %d 场，累计 %d 场" %
                     (page, len(batch), len(items)))
        has_more = bool(data.get("hasMore")) and bool(batch)
        page += 1
        time.sleep(0.25)
    return items, {"api_total": api_total, "fetched_lives": len(items),
                   "pages": page - 1}


def fetch_history_incremental(uid, cutoff_ms, previous_items, progress=None):
    """列表按开播时间倒序；只翻到上次同步边界，并覆盖上次仍在直播的场次。"""
    items, seen_lives = [], set()
    ongoing = {
        _live_id(item) for item in previous_items
        if _live_id(item) and _live_stop(item) == 0
    }
    target_start = int(cutoff_ms) - INCREMENTAL_LOOKBACK_MS
    api_total, page = None, 1
    while page <= MAX_INCREMENTAL_PAGES:
        data = _fetch_page(uid, page)
        api_total = data.get("total", api_total)
        batch = data.get("items") or []
        for item in batch:
            key = _item_key(item)
            if key and key not in seen_lives:
                seen_lives.add(key)
                items.append(item)
        starts = [_live_start(item) for item in batch if _live_start(item)]
        oldest_start = min(starts) if starts else 0
        if progress:
            progress("增量分页 %d：本页 %d 场，累计 %d 场" %
                     (page, len(batch), len(items)))
        reached_boundary = bool(oldest_start and oldest_start < target_start)
        covered_ongoing = ongoing.issubset(seen_lives)
        if (reached_boundary and covered_ongoing) or not data.get("hasMore") or not batch:
            break
        page += 1
        time.sleep(0.25)
    return items, {
        "api_total": api_total,
        "fetched_lives": len(items),
        "pages": page,
        "incremental": True,
        "ongoing_expected": len(ongoing),
        "ongoing_covered": len(ongoing.intersection(seen_lives)),
    }


def _record_event(item, record, uid):
    channel = item.get("channel") or {}
    live = item.get("live") or {}
    payload = record.get("payload") or {}
    frame = item.get("danmakus") or {}
    actor = next((x for x in frame.get("actors") or []
                  if x.get("uid") == uid), {})
    base = {
        "ts": int(record.get("ts") or 0),
        "room": int(channel.get("roomId") or 0),
        "uid": uid,
        "uname": actor.get("name") or "",
        "src": "danmakus",
        "remote_v3": 1,
        "live_id": live.get("liveId"),
        "live_start": live.get("startDate"),
        "live_stop": live.get("stopDate"),
        "area": live.get("area") or "",
    }
    kind = int(record.get("type") or 0)
    if kind == 0:
        payload_kind = int(record.get("payloadKind") or 0)
        text = payload.get("rawText")
        if text is None and payload_kind == 2:
            text = "[直播间表情]"
        base.update(cmd="DANMU_MSG", text=text or "")
    elif kind == 1:
        price = float(payload.get("price") or 0)
        count = int(payload.get("count") or 1)
        total_coin = int(round(price * 1000 * count))
        base.update(
            cmd="SEND_GIFT", gift=payload.get("name") or "礼物",
            num=count, price=int(round(price * 1000)),
            total_coin=total_coin,
            coin_type="gold" if price > 0 else "silver",
            free=0 if price > 0 else 1)
        if payload.get("mysteryBoxName") or payload.get("mysteryBoxPrice"):
            base["mysteryBoxName"] = payload.get("mysteryBoxName")
            base["mysteryBoxPrice"] = payload.get("mysteryBoxPrice")
    elif kind == 2:
        base.update(
            cmd="GUARD_BUY", guard=payload.get("name") or "舰长",
            num=int(payload.get("count") or 1),
            price=int(round(float(payload.get("price") or 0) * 1000)))
    elif kind == 3:
        base.update(
            cmd="SUPER_CHAT_MESSAGE", text=payload.get("text") or "",
            price=float(payload.get("price") or 0))
    elif kind == 4:
        base.update(cmd="ENTRY_EFFECT", interact="进入直播间")
    else:
        return None
    digest = hashlib.sha1(json.dumps(
        [base.get("live_id"), record.get("ts"), record.get("type"),
         record.get("payloadKind"), payload],
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    base["remote_key"] = digest
    return base


def convert_history(items, uid=None):
    uid = resolve_uid(uid)
    events, unknown = [], Counter()
    for item in items:
        for record in (item.get("danmakus") or {}).get("records") or []:
            event = _record_event(item, record, uid)
            if event:
                events.append(event)
            else:
                unknown[(record.get("type"), record.get("payloadKind"))] += 1
    events = [e for e in events if e.get("ts") and e.get("room")]
    groups = defaultdict(list)
    for event in events:
        if event.get("cmd") != "SEND_GIFT":
            groups[("single", id(event))].append(event)
            continue
        groups[(
            event.get("live_id"), event.get("room"), event.get("gift"),
            event.get("num"), round(L.yuan_of(event), 2),
        )].append(event)
    deduped = []
    for group in groups.values():
        group.sort(key=lambda e: e.get("ts") or 0)
        last_ts = None
        for event in group:
            stamp = int(event.get("ts") or 0)
            if last_ts is None or stamp - last_ts > 10 * 1000:
                deduped.append(event)
            last_ts = stamp
    return L.dedupe_events(deduped), unknown


def _semantic(event):
    cmd = event.get("cmd") or ""
    if cmd.startswith("ENTRY_EFFECT") or (
            cmd.startswith("INTERACT_WORD")
            and event.get("interact") == "进入直播间"):
        return ("ENTRY", "")
    if cmd == "DANMU_MSG":
        return ("DANMU_MSG", " ".join(str(event.get("text") or "").split()))
    if cmd == "SEND_GIFT":
        return ("SEND_GIFT", " ".join(str(event.get("gift") or "").split()))
    if cmd == "GUARD_BUY":
        return ("GUARD_BUY", " ".join(str(event.get("guard") or "").split()))
    if cmd == "SUPER_CHAT_MESSAGE":
        return ("SUPER_CHAT_MESSAGE",
                " ".join(str(event.get("text") or "").split()))
    return (cmd, "")


def _match_key(event):
    cmd, content = _semantic(event)
    try:
        quantity = int(event.get("num") or 1)
    except (TypeError, ValueError):
        quantity = 1
    return (int(event.get("room") or 0), cmd, content,
            round(L.yuan_of(event), 2), quantity)


def _conflict_key(event):
    cmd, content = _semantic(event)
    try:
        quantity = int(event.get("num") or 1)
    except (TypeError, ValueError):
        quantity = 1
    return (int(event.get("room") or 0), cmd, content, quantity)


def _tolerance_ms(event):
    if _semantic(event)[0] == "ENTRY":
        return 90 * 1000
    if event.get("src") == "manual":
        return 30 * 60 * 1000 if event.get("estimated") else 2 * 60 * 1000
    return 10 * 1000


def compare(local_events, remote_events):
    buckets = defaultdict(list)
    conflict_buckets = defaultdict(list)
    for index, event in enumerate(local_events):
        buckets[_match_key(event)].append((int(event.get("ts") or 0), index))
        conflict_buckets[_conflict_key(event)].append(
            (int(event.get("ts") or 0), index))
    for values in buckets.values():
        values.sort()
    for values in conflict_buckets.values():
        values.sort()

    used = set()
    remote_only, matched = [], 0
    for event in sorted(remote_events, key=lambda x: x.get("ts") or 0):
        candidates = buckets.get(_match_key(event), [])
        stamp = int(event.get("ts") or 0)
        best, best_diff = None, None
        for local_stamp, index in candidates:
            if index in used:
                continue
            local_event = local_events[index]
            tolerance = _tolerance_ms(local_event)
            diff = abs(local_stamp - stamp)
            if diff <= tolerance and (best_diff is None or diff < best_diff):
                best, best_diff = index, diff
        if best is None and _semantic(event)[0] == "GUARD_BUY":
            for local_stamp, index in conflict_buckets.get(
                    _conflict_key(event), []):
                if index in used:
                    continue
                local_event = local_events[index]
                tolerance = _tolerance_ms(local_event)
                if local_event.get("src") != "manual":
                    tolerance = min(tolerance, 3000)
                diff = abs(local_stamp - stamp)
                if diff <= tolerance and (best_diff is None or diff < best_diff):
                    best, best_diff = index, diff
        if best is None:
            remote_only.append(event)
        else:
            used.add(best)
            matched += 1
    local_only = [event for index, event in enumerate(local_events)
                  if index not in used]
    return matched, remote_only, local_only


def _sync_history_unlocked(uid=None, progress=print, use_snapshot=False,
                           force_full=False):
    uid = resolve_uid(uid)
    started = datetime.now(TZ)
    started_at = started.isoformat()
    started_ms = int(started.timestamp() * 1000)
    state = _load_state(uid)
    snapshot = _read_json(SNAPSHOT_FILE, {})
    if snapshot.get("user_id") not in (None, uid):
        raise ValueError("本地历史快照的用户 ID 不匹配")
    previous_items = snapshot.get("items") or []
    previous_remote_events = _read_jsonl(REMOTE_EVENTS_FILE)
    if not previous_remote_events and previous_items:
        previous_remote_events, _ = convert_history(previous_items)
        _atomic_jsonl(REMOTE_EVENTS_FILE, previous_remote_events)

    mode = "incremental"
    incremental_since = state.get("last_sync_at")
    incremental_cutoff_ms = None
    unknown = Counter()
    if use_snapshot:
        if not previous_items:
            raise FileNotFoundError("本地历史快照不存在")
        items = previous_items
        fetch_meta = snapshot.get("meta") or {
            "api_total": None, "fetched_lives": len(items), "pages": None}
        batch_events = []
        mode = "cached"
    elif force_full or not previous_items or not state:
        items, fetch_meta = fetch_history(uid, progress)
        batch_events, unknown = convert_history(items)
        mode = "full"
        incremental_since = None
    else:
        try:
            cutoff = datetime.fromisoformat(incremental_since)
            incremental_cutoff_ms = int(cutoff.timestamp() * 1000)
        except Exception:
            incremental_cutoff_ms = started_ms - INCREMENTAL_LOOKBACK_MS
        fetched_items, fetch_meta = fetch_history_incremental(
            uid, incremental_cutoff_ms, previous_items, progress)
        items = _merge_items(previous_items, fetched_items)
        converted, unknown = convert_history(fetched_items)
        batch_events = [
            event for event in converted
            if int(event.get("ts") or 0) >= (
                incremental_cutoff_ms - EVENT_CLOCK_SKEW_MS)
        ]

    existing_remote_keys = {_event_key(event) for event in previous_remote_events}
    if mode == "full":
        remote_events = list(batch_events)
        new_remote_events = list(batch_events)
    else:
        new_remote_events = [
            event for event in batch_events
            if _event_key(event) not in existing_remote_keys
        ]
        remote_events = _merge_events(previous_remote_events, new_remote_events)

    base_events = [e for e in L.load_events() if not e.get("remote_v3")]
    local_events = L.dedupe_events(base_events)
    matched, remote_only, local_only = compare(local_events, new_remote_events)

    supplement = _read_jsonl(L.DANMAKUS_V3_FILE)
    supplement_by_key = {_event_key(event): event for event in supplement}
    newly_added_supplement = []
    for event in remote_only:
        key = _event_key(event)
        if key not in supplement_by_key:
            supplement_by_key[key] = event
            newly_added_supplement.append(event)
    supplement = sorted(supplement_by_key.values(),
                        key=lambda event: event.get("ts") or 0)

    if mode != "cached" or not os.path.isfile(SNAPSHOT_FILE):
        _atomic_json(SNAPSHOT_FILE, {
            "fetched_at": started_at,
            "user_id": uid,
            "meta": fetch_meta,
            "items": items,
        })
    if mode != "cached" or previous_remote_events:
        _atomic_jsonl(REMOTE_EVENTS_FILE, remote_events)
    _atomic_jsonl(L.DANMAKUS_V3_FILE, supplement)

    audit = {
        "fetched_at": started_at,
        "user_id": uid,
        "mode": mode,
        "incremental_since": incremental_since if mode == "incremental" else None,
        "incremental_cutoff_event_ms": incremental_cutoff_ms,
        "api_total_lives": fetch_meta.get("api_total"),
        "fetched_lives": fetch_meta.get("fetched_lives"),
        "pages": fetch_meta.get("pages"),
        "remote_batch_events_after_dedupe": len(batch_events),
        "new_remote_events": len(new_remote_events),
        "remote_events_saved_total": len(remote_events),
        "local_events_before_sync": len(local_events),
        "matched": matched,
        "remote_only_added": len(newly_added_supplement),
        "remote_only_in_batch": len(remote_only),
        "supplement_total": len(supplement),
        "local_only_in_batch": len(local_only),
        "remote_only_by_type": dict(Counter(
            e.get("cmd") for e in newly_added_supplement).most_common()),
        "supplement_by_type": dict(Counter(
            e.get("cmd") for e in supplement).most_common()),
        "unknown_types": {"%s/%s" % key: value
                          for key, value in sorted(unknown.items())},
        "snapshot_file": SNAPSHOT_FILE,
        "remote_events_file": REMOTE_EVENTS_FILE,
        "supplement_file": L.DANMAKUS_V3_FILE,
        "state_file": STATE_FILE,
    }
    _atomic_json(AUDIT_FILE, audit)
    if mode != "cached":
        last_event_ts = max(
            [int(event.get("ts") or 0) for event in remote_events] or [0])
        _atomic_json(STATE_FILE, {
            "user_id": uid,
            "last_sync_at": started_at,
            "last_mode": mode,
            "last_remote_event_ts": last_event_ts,
            "remote_events_saved_total": len(remote_events),
            "supplement_total": len(supplement),
        })
    elif not os.path.isfile(STATE_FILE) and incremental_since:
        _atomic_json(STATE_FILE, {
            "user_id": uid,
            "last_sync_at": incremental_since,
            "last_mode": "migrated",
            "remote_events_saved_total": len(remote_events),
            "supplement_total": len(supplement),
        })
    return audit


def sync_history(uid=None, progress=print, use_snapshot=False,
                 enforce_quota=True, force_full=False):
    if enforce_quota:
        _consume_daily_quota()
    with _sync_lock():
        return _sync_history_unlocked(uid, progress, use_snapshot,
                                      force_full=force_full)


def main():
    print("开始同步 danmakus 数据...")
    try:
        audit = sync_history(
            use_snapshot="--cached" in os.sys.argv[1:],
            force_full="--full" in os.sys.argv[1:])
    except DailySyncLimitExceeded as exc:
        print(str(exc))
        return 2
    print("远程直播场：%s（API 标记 %s）" %
          (audit["fetched_lives"], audit["api_total_lives"]))
    print("同步模式：%s" % audit["mode"])
    print("本次远程新事件：%d" % audit["new_remote_events"])
    print("与本地匹配：%d" % audit["matched"])
    print("本次新增本地缺失：%d" % audit["remote_only_added"])
    print("当前补充集：%d" % audit["supplement_total"])
    print("补充文件：%s" % audit["supplement_file"])
    print("校准报告：%s" % AUDIT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
