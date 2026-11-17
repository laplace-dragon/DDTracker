#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 danmakus 的互动画像结果导入成本地事件格式。

用法：python import_danmakus.py _api/10_result.bin
产出：
  danmakus_history.json   原始解析结果（存档，便于复查）
  history_events.jsonl    转换后的事件，供报告与界面合并使用
"""

import json
import os
import sys

import msgpack_min as M

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_EVENTS = os.path.join(HERE, "history_events.jsonl")
OUT_RAW = os.path.join(HERE, "danmakus_history.json")

TYPE_MAP = {0: "DANMU_MSG", 1: "SEND_GIFT", 2: "GUARD_BUY",
            3: "SUPER_CHAT_MESSAGE", 4: "ENTRY_EFFECT"}


def to_event(rec, channel, live):
    kind = TYPE_MAP.get(rec.get("type"))
    if not kind:
        return None
    ev = {
        "ts": rec.get("sendDate"),
        "room": channel.get("roomId"),
        "cmd": kind,
        "uid": rec.get("uId"),
        "uname": rec.get("uName"),
        "src": "danmakus",
        "live_id": live.get("liveId"),
        "area": live.get("area"),
    }
    price = rec.get("price") or 0
    count = rec.get("count") or 1
    if kind == "DANMU_MSG":
        ev["text"] = rec.get("message") or ""
    elif kind == "SEND_GIFT":
        ev.update(gift=rec.get("message"), num=count, price=int(round(price * 1000)),
                  coin_type="gold", free=0, total_coin=int(round(price * 1000)) * int(count))
    elif kind == "GUARD_BUY":
        ev.update(guard=rec.get("message"), num=count, price=int(round(price * 1000)))
    elif kind == "SUPER_CHAT_MESSAGE":
        ev.update(text=rec.get("message") or "", price=price)
    elif kind == "ENTRY_EFFECT":
        ev["interact"] = "进入直播间"
    return ev


def build_events(data):
    """把 danmakus 的解析结果转换成统一事件列表。"""
    events = []
    for ch in data.get("channels", []):
        channel = ch.get("channel") or {}
        for lv in ch.get("lives") or []:
            live = lv.get("live") or {}
            for rec in lv.get("danmakus") or []:
                ev = to_event(rec, channel, live)
                if ev and ev.get("ts"):
                    events.append(ev)
    events.sort(key=lambda e: e["ts"])
    return events


def save(data, events, prefix=""):
    """写出原始结果与事件文件，返回统计。"""
    # 先写临时文件再原子替换，避免同步中途失败把现有数据写坏
    tmp_raw = OUT_RAW + ".tmp"
    with open(tmp_raw, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp_raw, OUT_RAW)
    tmp_ev = OUT_EVENTS + ".tmp"
    with open(tmp_ev, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    os.replace(tmp_ev, OUT_EVENTS)
    c = {}
    for e in events:
        c[e["cmd"]] = c.get(e["cmd"], 0) + 1
    money = sum((e.get("total_coin") or 0) / 1000.0 for e in events if e["cmd"] == "SEND_GIFT")
    money += sum((e.get("price") or 0) / 1000.0 for e in events if e["cmd"] == "GUARD_BUY")
    money += sum(e.get("price") or 0 for e in events if e["cmd"] == "SUPER_CHAT_MESSAGE")
    return {
        "count": len(events),
        "kinds": c,
        "money": round(money, 2),
        "channels": len(data.get("channels", [])),
        "lives": sum(len(c_.get("lives") or []) for c_ in data.get("channels", [])),
    }


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "_api", "10_result.bin")
    data = M.loads(open(src, "rb").read())
    events = build_events(data)
    st = save(data, events)
    print("导入 %d 条：%s" % (st["count"], json.dumps(st["kinds"], ensure_ascii=False)))
    print("折算总支出 ¥%.2f ｜ 频道 %d 个 ｜ 场次 %d 场"
          % (st["money"], st["channels"], st["lives"]))
    print("已写入 %s 与 %s" % (OUT_EVENTS, OUT_RAW))
    return 0


if __name__ == "__main__":
    sys.exit(main())
