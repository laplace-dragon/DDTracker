#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集器守护进程。

独立于控制台界面运行：负责启动采集器，并在它崩溃/退出后自动拉起。
心跳文件 collector.alive 由采集器每 30 秒刷新；本进程据此判断存活。
存在 collector.disabled 标记文件时暂停守护（对应界面上的「停止采集」）。
"""

import ctypes
import json
import os
import subprocess
import sys
import time

HERE = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
ALIVE = os.path.join(HERE, "collector.alive")
DISABLED = os.path.join(HERE, "collector.disabled")
PID_FILE = os.path.join(HERE, "collector.pid")
WATCHDOG_PID = os.path.join(HERE, "watchdog.pid")
WATCHDOG_ALIVE = os.path.join(HERE, "watchdog.alive")
COLLECTOR_LOG = os.path.join(HERE, "collector.log")
WATCH_LOG = os.path.join(HERE, "watchdog.log")
ARGS_FILE = os.path.join(HERE, "collector.args.json")
PYTHONW = (sys.executable if getattr(sys, "frozen", False)
           else os.path.join(os.path.dirname(sys.executable), "pythonw.exe"))
if not os.path.exists(PYTHONW):
    PYTHONW = sys.executable
CREATE_NO_WINDOW = 0x08000000
ARGS = ["collect", "--follow", "--interval", "60", "--cap", "25",
        "--rotate-mb", "64", "--keep", "0"]
WATCHDOG_MUTEX = "Local\\BiliLiveWatch.Watchdog.v1"
_watchdog_mutex_handle = None


def acquire_watchdog_lock():
    """同一时刻只保留一个守护进程，避免它重复拉起采集器。"""
    global _watchdog_mutex_handle
    if _watchdog_mutex_handle:
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, WATCHDOG_MUTEX)
        if not handle:
            return False
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        _watchdog_mutex_handle = handle
        return True
    except Exception:
        return True


def load_args():
    try:
        with open(ARGS_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, list) and saved and saved[0] == "collect":
            return [str(x) for x in saved]
    except Exception:
        pass
    return list(ARGS)


def log(msg):
    try:
        with open(WATCH_LOG, "a", encoding="utf-8", buffering=1) as fh:
            fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def alive():
    try:
        return time.time() - os.path.getmtime(ALIVE) < 120
    except OSError:
        return False


def heartbeat():
    try:
        with open(WATCHDOG_ALIVE, "w", encoding="utf-8") as fh:
            fh.write("%d %d\n" % (os.getpid(), int(time.time())))
        with open(WATCHDOG_PID, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def start():
    """启动采集器；stdout/stderr 接到 collector.log，避免日志盲区。"""
    kwargs = {"cwd": HERE, "creationflags": CREATE_NO_WINDOW}
    log_fh = None
    try:
        log_fh = open(COLLECTOR_LOG, "a", encoding="utf-8")
        kwargs["stdout"] = log_fh
        kwargs["stderr"] = subprocess.STDOUT
    except OSError:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    try:
        argv = load_args()
        if getattr(sys, "frozen", False):
            cmd = [sys.executable] + argv
        else:
            cmd = [PYTHONW, os.path.join(HERE, "bili_live.py")] + argv
        proc = subprocess.Popen(cmd, env=dict(os.environ, PYTHONUNBUFFERED="1"), **kwargs)
    finally:
        if log_fh:
            log_fh.close()
    try:
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
    except OSError:
        pass
    log("已启动采集器（PID %d）" % proc.pid)
    return proc


def main():
    if not acquire_watchdog_lock():
        log("检测到已有守护进程，本次重复启动已退出")
        return
    heartbeat()
    log("守护进程启动（PID %d）" % os.getpid())
    proc = None
    try:
        while True:
            heartbeat()
            try:
                running = proc is not None and proc.poll() is None
                if os.path.isfile(DISABLED):
                    if running:
                        proc.kill()
                        log("收到停止标记，已结束采集器")
                    proc = None
                elif not running:
                    if proc is None and alive():
                        pass  # 别人（例如界面）启动的采集器在跑，先不插手
                    else:
                        if proc is not None:
                            hint = "（健康自检）" if proc.returncode == 3 else ""
                            log("采集器已退出（code=%s）%s，自动重启" % (proc.returncode, hint))
                        proc = start()
            except Exception as exc:
                log("异常：%s" % exc)
            time.sleep(5)
    finally:
        try:
            with open(WATCHDOG_ALIVE, "r", encoding="utf-8") as fh:
                alive_pid = int(fh.read().split()[0])
            if alive_pid == os.getpid():
                os.remove(WATCHDOG_ALIVE)
        except Exception:
            pass


if __name__ == "__main__":
    main()
