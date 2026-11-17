#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站扫码登录（免开发者工具）。

两步：
    python bili_login.py --qr     # 生成二维码 qr.png，保存 qrcode_key
    python bili_login.py --poll   # 轮询扫码结果，成功后写入 cookie.txt

只用标准库 + 已安装的 qrcode（仅用其矩阵计算，PNG 自行编码）。
"""

import json
import os
import struct
import sys
import time
import urllib.request
import zlib
from urllib.parse import parse_qs, urlparse

import qrcode

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
KEY_FILE = os.path.join(HERE, "qr_key.txt")
QR_PNG = os.path.join(HERE, "qr.png")
COOKIE_FILE = os.path.join(HERE, "cookie.txt")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=%s"


def request(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = resp.read().decode("utf-8", "replace")
        set_cookie = resp.headers.get_all("Set-Cookie") or []
    return json.loads(body), set_cookie


def write_png(path, matrix, scale=8):
    n = len(matrix)
    size = n * scale
    lines = []
    for row in matrix:
        line = bytearray()
        for cell in row:
            line += (b"\x00\x00\x00" if cell else b"\xff\xff\xff") * scale
        lines.append(bytes(line))
    raw = b"".join(b"\x00" + ln for ln in lines for _ in range(scale))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    blob = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(blob)


def cmd_qr():
    data, _ = request(GENERATE)
    if data.get("code") != 0:
        print("生成二维码失败：%s" % data.get("message"))
        return 1
    info = data["data"]
    with open(KEY_FILE, "w", encoding="utf-8") as fh:
        fh.write(info["qrcode_key"])
    qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(info["url"])
    qr.make(fit=True)
    write_png(QR_PNG, qr.get_matrix(), scale=8)
    print("二维码已生成：%s" % QR_PNG)
    print("有效期内请用 B 站 App 扫码并在手机上确认。")
    return 0


def extract_cookie(poll_data, set_cookie):
    payload = poll_data.get("data") or {}
    qs = parse_qs(urlparse(payload.get("url") or "").query)
    pairs = {}
    for key in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
        if qs.get(key):
            pairs[key] = qs[key][0]
    for raw in set_cookie:
        head = raw.split(";", 1)[0]
        if "=" in head:
            k, v = head.split("=", 1)
            k = k.strip()
            if k in ("buvid3", "b_nut", "buvid4", "SESSDATA") and k not in pairs:
                pairs[k] = v.strip()
    return "; ".join("%s=%s" % (k, v) for k, v in pairs.items())


def cmd_poll(timeout=180):
    if not os.path.isfile(KEY_FILE):
        print("没有 qr_key.txt，请先运行 --qr。")
        return 1
    with open(KEY_FILE, "r", encoding="utf-8") as fh:
        key = fh.read().strip()
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        data, set_cookie = request(POLL % key)
        payload = data.get("data") or {}
        code = payload.get("code")
        if code == 0:
            cookie = extract_cookie(data, set_cookie)
            if "SESSDATA=" not in cookie:
                print("登录成功但未取到 SESSDATA，请重试。")
                return 1
            with open(COOKIE_FILE, "w", encoding="utf-8") as fh:
                fh.write(cookie)
            print("登录成功，已写入 %s" % COOKIE_FILE)
            return 0
        if code == 86038:
            print("二维码已过期，请重新运行 --qr。")
            return 1
        msg = {86101: "等待扫码", 86090: "已扫码，请在手机上确认"}.get(code, str(code))
        if msg != last:
            print("  %s" % msg)
            last = msg
        time.sleep(2)
    print("等待超时，请重新运行 --qr。")
    return 1


def main():
    argv = sys.argv[1:]
    if "--qr" in argv:
        return cmd_qr()
    if "--poll" in argv:
        return cmd_poll()
    print("用法：python bili_login.py --qr | --poll")
    return 2


if __name__ == "__main__":
    sys.exit(main())
