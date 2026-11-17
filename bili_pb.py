#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 protobuf 字段解析（无需 .proto），用于解开 SEND_GIFT_V2 等 protobuf 负载。"""

import struct


def read_varint(buf, i):
    shift = 0
    val = 0
    while i < len(buf):
        byte = buf[i]
        i += 1
        val |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return val, i
        shift += 7
        if shift > 70:
            break
    raise ValueError("varint overflow")


def dump(buf, depth=0, maxdepth=4):
    """把 protobuf 字节流解析成 [(字段号, 类型, 值)] 的扁平列表。"""
    out = []
    i = 0
    while i < len(buf):
        try:
            key, i = read_varint(buf, i)
        except (ValueError, IndexError):
            break
        field, wire = key >> 3, key & 7
        try:
            if wire == 0:
                val, i = read_varint(buf, i)
                out.append((field, "varint", val))
            elif wire == 1:
                val = struct.unpack_from("<q", buf, i)[0]
                i += 8
                out.append((field, "int64", val))
            elif wire == 2:
                ln, i = read_varint(buf, i)
                out.append((field, "bytes", buf[i:i + ln]))
                i += ln
            elif wire == 5:
                val = struct.unpack_from("<i", buf, i)[0]
                i += 4
                out.append((field, "int32", val))
            else:
                break
        except (IndexError, struct.error):
            break
    return out


def pretty(buf, depth=0, maxdepth=4):
    """递归展开，字符串尽量按 UTF-8 显示，便于人工辨认字段含义。"""
    pad = "  " * depth
    lines = []
    for field, kind, val in dump(buf, depth, maxdepth):
        if kind == "bytes":
            try:
                text = val.decode("utf-8")
                if text and text.isprintable() and len(text) < 100:
                    lines.append("%s#%d str  %s" % (pad, field, text))
                    continue
            except UnicodeDecodeError:
                pass
            if depth < maxdepth and len(val) > 1:
                sub = pretty(val, depth + 1, maxdepth)
                if sub:
                    lines.append("%s#%d msg  (%d 字节)" % (pad, field, len(val)))
                    lines.extend(sub)
                    continue
            lines.append("%s#%d raw  %s" % (pad, field, val[:20].hex()))
        else:
            lines.append("%s#%d %s %s" % (pad, field, kind, val))
    return lines
