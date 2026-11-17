#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 msgpack 解码器（只用标准库），用于解开 danmakus 分析结果的 msgpack 负载。"""

import struct


def _arr(b, i, n):
    out = []
    for _ in range(n):
        v, i = unpack(b, i)
        out.append(v)
    return out, i


def _map(b, i, n):
    out = {}
    for _ in range(n):
        k, i = unpack(b, i)
        v, i = unpack(b, i)
        out[k] = v
    return out, i


def _str(b, i, n):
    return b[i:i + n].decode("utf-8", "replace"), i + n


def unpack(b, i=0):
    c = b[i]
    i += 1
    if c <= 0x7F:
        return c, i
    if c >= 0xE0:
        return c - 256, i
    if 0x80 <= c <= 0x8F:
        return _map(b, i, c & 0x0F)
    if 0x90 <= c <= 0x9F:
        return _arr(b, i, c & 0x0F)
    if 0xA0 <= c <= 0xBF:
        return _str(b, i, c & 0x1F)
    if c == 0xC0:
        return None, i
    if c == 0xC2:
        return False, i
    if c == 0xC3:
        return True, i
    if c in (0xC4, 0xC5, 0xC6):
        w = {0xC4: 1, 0xC5: 2, 0xC6: 4}[c]
        n = int.from_bytes(b[i:i + w], "big")
        i += w
        return b[i:i + n], i + n
    if c in (0xC7, 0xC8, 0xC9):
        w = {0xC7: 1, 0xC8: 2, 0xC9: 4}[c]
        n = int.from_bytes(b[i:i + w], "big")
        i += w + 1                      # 跳过 1 字节类型标记
        return b[i:i + n], i + n
    if c == 0xCA:
        return struct.unpack_from(">f", b, i)[0], i + 4
    if c == 0xCB:
        return struct.unpack_from(">d", b, i)[0], i + 8
    if c == 0xCC:
        return b[i], i + 1
    if c == 0xCD:
        return int.from_bytes(b[i:i + 2], "big"), i + 2
    if c == 0xCE:
        return int.from_bytes(b[i:i + 4], "big"), i + 4
    if c == 0xCF:
        return int.from_bytes(b[i:i + 8], "big"), i + 8
    if c in (0xD0, 0xD1, 0xD2, 0xD3):
        w = {0xD0: 1, 0xD1: 2, 0xD2: 4, 0xD3: 8}[c]
        return int.from_bytes(b[i:i + w], "big", signed=True), i + w
    if c == 0xD9:
        n = b[i]
        return _str(b, i + 1, n)
    if c == 0xDA:
        n = int.from_bytes(b[i:i + 2], "big")
        return _str(b, i + 2, n)
    if c == 0xDB:
        n = int.from_bytes(b[i:i + 4], "big")
        return _str(b, i + 4, n)
    if c == 0xDC:
        return _arr(b, i + 2, int.from_bytes(b[i:i + 2], "big"))
    if c == 0xDD:
        return _arr(b, i + 4, int.from_bytes(b[i:i + 4], "big"))
    if c == 0xDE:
        return _map(b, i + 2, int.from_bytes(b[i:i + 2], "big"))
    if c == 0xDF:
        return _map(b, i + 4, int.from_bytes(b[i:i + 4], "big"))
    raise ValueError("unknown msgpack byte 0x%02x at %d" % (c, i - 1))


def loads(data):
    return unpack(data, 0)[0]
