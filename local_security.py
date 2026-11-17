#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 本地敏感文件权限工具。"""

import os
import subprocess
import uuid

CREATE_NO_WINDOW = 0x08000000


def restrict_private_file(path):
    """移除继承 ACL，仅保留当前用户访问。失败时返回 False。"""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False
    user = os.environ.get("USERNAME")
    if not user:
        return False
    domain = os.environ.get("USERDOMAIN")
    account = "%s\\%s" % (domain, user) if domain else user
    try:
        result = subprocess.run(
            ["icacls", os.path.abspath(path), "/inheritance:r",
             "/grant:r", "%s:F" % account],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW)
        return result.returncode == 0
    except Exception:
        return False


def write_private_text(path, text):
    """原子写入 UTF-8 文本，并限制为当前用户可读写。"""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex)
    try:
        # 先创建空文件并收紧权限，再写入敏感内容，避免写入期间短暂继承宽权限。
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        if not restrict_private_file(tmp):
            raise PermissionError("无法限制敏感文件权限")
        with open(tmp, "w", encoding="utf-8", buffering=1) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        restrict_private_file(path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path
