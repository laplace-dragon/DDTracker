#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直播间消息探针：统计所有消息类型，并打印礼物/上舰/SC 等关注类型的明细。

用法：python bili_probe.py <roomid> [秒数]
"""

import asyncio
import json
import sys
import time
from collections import Counter

import websockets

import bili_live as L

WATCH = ("SEND_GIFT", "COMBO_SEND", "GUARD_BUY", "SUPER_CHAT_MESSAGE",
         "USER_TOAST_MSG", "MESSAGEBOX", "GUARD_LOTTERY_START", "BLIND_GIFT")


async def main():
    rid = int(sys.argv[1])
    dur = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    ck = L.cookie()
    mid = L.my_mid()
    info = L.live_api("/xlive/web-room/v1/index/getDanmuInfo", {"id": rid, "type": 0}, ck).get("data") or {}
    h = info["host_list"][0]
    url = "wss://%s:%s/sub" % (h["host"], h["wss_port"])
    cmds = Counter()
    hits = 0
    print("连接 room=%s  uid=%s  观察 %d 秒" % (rid, mid, dur))
    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        auth = {"uid": mid, "roomid": rid, "protover": 2, "platform": "web",
                "type": 2, "key": info["token"]}
        await ws.send(L.pack(7, json.dumps(auth).encode()))
        end = time.time() + dur
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                await ws.send(L.pack(2))
                continue
            for op, body in L.unpack(raw):
                if op != 5:
                    continue
                try:
                    msg = json.loads(body.decode("utf-8", "replace"))
                except Exception:
                    continue
                cmd = msg.get("cmd", "?")
                cmds[cmd] += 1
                if cmd.startswith(WATCH):
                    hits += 1
                    ev = L.normalize(msg, rid)
                    mark = "★本人" if ev.get("uid") == mid else "     "
                    info_txt = {k: v for k, v in ev.items() if k not in ("ts", "room", "src")}
                    print("%s %-22s %s" % (mark, cmd, json.dumps(info_txt, ensure_ascii=False)[:220]))
    print("\n--- %d 秒内消息类型统计（共 %d 条）---" % (dur, sum(cmds.values())))
    for k, v in cmds.most_common(40):
        print("  %-34s %d" % (k, v))
    print("其中礼物/上舰/SC 类: %d 条" % hits)


asyncio.run(main())
