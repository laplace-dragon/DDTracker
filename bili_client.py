#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DD轨迹客户端（桌面 EXE 入口）。

默认打开 Tkinter 客户端；也作为守护进程/采集器/控制台的统一入口：
    DD轨迹.exe --watchdog
    DD轨迹.exe --ui --no-browser
    DD轨迹.exe collect ...
    DD轨迹.exe report
"""

import ctypes
import glob
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from ctypes import wintypes

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
           else os.path.dirname(os.path.abspath(__file__)))
CREATE_NO_WINDOW = 0x08000000

SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
ARGS_FILE = os.path.join(APP_DIR, "collector.args.json")
ALIVE_FILE = os.path.join(APP_DIR, "collector.alive")
PID_FILE = os.path.join(APP_DIR, "collector.pid")
DISABLED_FILE = os.path.join(APP_DIR, "collector.disabled")
WATCHDOG_PID_FILE = os.path.join(APP_DIR, "watchdog.pid")
WATCHDOG_ALIVE_FILE = os.path.join(APP_DIR, "watchdog.alive")
REPORT_FILE = os.path.join(APP_DIR, "live_report.html")
CONSOLE_LOG = os.path.join(APP_DIR, "console.log")
CONSOLE_READY = os.path.join(APP_DIR, "console.ready")
ACCOUNT_IDENTITY_FILE = os.path.join(APP_DIR, "account_identity.json")
ICON_FILE = os.path.join(APP_DIR, "assets", "app-icon.ico")
DEFAULT_DATA_DIR = os.path.join(APP_DIR, "data")
CLIENT_TITLE = "DD轨迹"
CLIENT_MUTEX = "Local\\BiliLiveWatch.Client.v1"
_client_mutex_handle = None

STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
STARTUP_FILE = os.path.join(STARTUP_DIR, "DD轨迹自启.vbs")
LEGACY_STARTUP_FILE = os.path.join(STARTUP_DIR, "BiliLiveWatchdog.vbs")

PYTHONW = (sys.executable if FROZEN
           else os.path.join(os.path.dirname(sys.executable), "pythonw.exe"))
if not os.path.exists(PYTHONW):
    PYTHONW = sys.executable

DEFAULT_SETTINGS = {
    "follow": True, "interval": 60, "cap": 25, "rotate_mb": 64, "keep": 2,
    "close_behavior": "tray", "minimize_behavior": "tray",
    "data_dir": DEFAULT_DATA_DIR,
}
URL = "http://127.0.0.1:8765/"


def acquire_client_lock():
    """避免重复打开多个客户端窗口；重复启动时聚焦已有窗口。"""
    global _client_mutex_handle
    if _client_mutex_handle:
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, CLIENT_MUTEX)
        if not handle:
            return False
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        _client_mutex_handle = handle
        return True
    except Exception:
        return True


def focus_existing_client():
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, CLIENT_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def pid_alive(pid):
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def read_heartbeat():
    try:
        with open(ALIVE_FILE, "r", encoding="utf-8") as fh:
            pid = int(fh.read().split()[0])
        age = time.time() - os.path.getmtime(ALIVE_FILE)
        return pid, age
    except Exception:
        return None, float("inf")


def read_watchdog_pid():
    for path in (WATCHDOG_ALIVE_FILE, WATCHDOG_PID_FILE):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                pid = int(fh.read().split()[0])
            if time.time() - os.path.getmtime(path) < 30:
                return pid
        except Exception:
            continue
    return None


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            data.update({k: saved[k] for k in data if k in saved})
    except Exception:
        pass
    return data


def logged_in():
    try:
        with open(os.path.join(APP_DIR, "cookie.txt"), "r", encoding="utf-8") as fh:
            return "SESSDATA=" in fh.read()
    except Exception:
        return False


def cookie_value(name):
    try:
        with open(os.path.join(APP_DIR, "cookie.txt"), "r", encoding="utf-8") as fh:
            for part in fh.read().split(";"):
                key, _, value = part.strip().partition("=")
                if key == name:
                    return value
    except Exception:
        pass
    return ""


def save_account_identity(uname, uid, face=""):
    if not uname or not uid:
        return
    with open(ACCOUNT_IDENTITY_FILE, "w", encoding="utf-8") as fh:
        json.dump({"uname": str(uname), "uid": str(uid), "face": str(face or "")},
                  fh, ensure_ascii=False, indent=2)


def load_account_identity():
    """读取本机缓存、账号快照或 data.json 中的登录身份。"""
    cookie_uid = cookie_value("DedeUserID")
    sources = []
    for path in (ACCOUNT_IDENTITY_FILE,
                 os.path.join(APP_DIR, "data.json"),
                 os.path.join(APP_DIR, "live_account.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh) or {}
            nav = payload.get("nav") if isinstance(payload, dict) else None
            if isinstance(nav, dict):
                sources.append((nav.get("uname"), nav.get("mid"), nav.get("face")))
            if isinstance(payload, dict):
                sources.append((payload.get("uname"), payload.get("uid"),
                                payload.get("face")))
        except Exception:
            continue
    for uname, uid, face in sources:
        uid = str(uid or "")
        if uname and (not cookie_uid or not uid or uid == cookie_uid):
            matching_face = next(
                (str(f) for _, u, f in sources
                 if f and (not uid or str(u or "") == uid)), "")
            return {"uname": str(uname), "uid": uid or cookie_uid,
                    "face": matching_face or str(face or "")}
    return {"uname": "", "uid": cookie_uid, "face": ""}


def fetch_account_identity():
    """扫码后从 B 站 nav 接口刷新昵称和 UID。"""
    try:
        import bili_live as live
        with open(os.path.join(APP_DIR, "cookie.txt"), "r", encoding="utf-8") as fh:
            ck = fh.read().strip()
        payload = live.get("https://api.bilibili.com/x/web-interface/nav", ck,
                           "https://www.bilibili.com/")
        nav = payload.get("data") or {}
        if payload.get("code") == 0 and nav.get("isLogin") and nav.get("uname"):
            identity = {"uname": str(nav["uname"]),
                        "uid": str(nav.get("mid") or ""),
                        "face": str(nav.get("face") or "")}
            save_account_identity(identity["uname"], identity["uid"], identity["face"])
            data_file = os.path.join(APP_DIR, "data.json")
            merged = {}
            try:
                with open(data_file, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    merged = loaded
            except Exception:
                pass
            merged["nav"] = nav
            tmp = data_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, data_file)
            return identity
    except Exception:
        pass
    return load_account_identity()


def avatar_remote_url(face, size=52):
    base = str(face or "").split("@", 1)[0]
    if not base:
        return ""
    return "%s@%dw_%dh_1c.png" % (base, size, size)


def avatar_cache_path(uid, size=96):
    safe_uid = "".join(ch for ch in str(uid or "current") if ch.isdigit()) or "current"
    return os.path.join(APP_DIR, "account_avatar_%s_%d.png" % (safe_uid, size))


def download_account_avatar(face, uid, size=96):
    url = avatar_remote_url(face, size)
    if not url:
        return ""
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0",
                      "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("头像接口未返回 PNG")
    path = avatar_cache_path(uid)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def clear_login_files(base_dir=None):
    """删除登录凭证、身份缓存和头像缓存，不删除采集数据。"""
    base_dir = os.path.abspath(base_dir or APP_DIR)
    targets = [
        os.path.join(base_dir, "cookie.txt"),
        os.path.join(base_dir, "account_identity.json"),
    ]
    targets.extend(glob.glob(os.path.join(base_dir, "account_avatar_*.png")))
    removed = []
    for path in targets:
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
        except FileNotFoundError:
            pass
    return removed


def build_args(settings):
    args = ["collect"]
    if settings.get("follow", True):
        args.append("--follow")
    args += ["--interval", str(int(settings.get("interval", 60))),
             "--cap", str(int(settings.get("cap", 25))),
             "--rotate-mb", str(int(settings.get("rotate_mb", 64))),
             "--keep", str(int(settings.get("keep", 2)))]
    return args


def save_settings(settings):
    merged = load_settings()
    merged.update(settings or {})
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    with open(ARGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(build_args(merged), fh, ensure_ascii=False, indent=1)


def autostart_enabled():
    return os.path.isfile(STARTUP_FILE) or os.path.isfile(LEGACY_STARTUP_FILE)


def read_startup_script(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    encodings = (("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
                 else ("utf-8-sig", "mbcs"))
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def set_autostart(enabled):
    if not enabled:
        for path in (STARTUP_FILE, LEGACY_STARTUP_FILE):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return
    os.makedirs(STARTUP_DIR, exist_ok=True)
    if FROZEN:
        run_expr = '"""%s"" --autostart"' % sys.executable
    else:
        run_expr = '"""%s"" """%s""" --autostart' % (
            PYTHONW, os.path.join(APP_DIR, "bili_client.py"))
    content = (
        "' DD轨迹 autostart - starts hidden in the system tray\n"
        "Option Explicit\n"
        "Dim sh\n"
        "Set sh = CreateObject(\"WScript.Shell\")\n"
        "sh.CurrentDirectory = \"%s\"\n"
        "sh.Run %s, 0, False\n" % (APP_DIR, run_expr)
    )
    # Windows Script Host 对 UTF-8 中文路径兼容性差，使用带 BOM 的 UTF-16。
    with open(STARTUP_FILE, "w", encoding="utf-16") as fh:
        fh.write(content)
    try:
        os.remove(LEGACY_STARTUP_FILE)
    except FileNotFoundError:
        pass


def _spawn(cmd):
    return subprocess.Popen(cmd, cwd=APP_DIR, creationflags=CREATE_NO_WINDOW,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True)


def _spawn_console():
    log_fh = None
    try:
        log_fh = open(CONSOLE_LOG, "a", encoding="utf-8", buffering=1)
        log_fh.write("\n===== %s 启动控制台 =====\n"
                     % time.strftime("%Y-%m-%d %H:%M:%S"))
        log_fh.flush()
        kwargs = {"stdout": log_fh, "stderr": subprocess.STDOUT}
    except OSError:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if FROZEN:
            cmd = [sys.executable, "--ui", "--no-browser"]
        else:
            cmd = [PYTHONW, os.path.join(APP_DIR, "bili_live_ui.py"), "--no-browser"]
        return subprocess.Popen(cmd, cwd=APP_DIR, creationflags=CREATE_NO_WINDOW,
                                close_fds=True, **kwargs)
    finally:
        if log_fh:
            log_fh.close()


def start_watchdog():
    if FROZEN:
        return _spawn([sys.executable, "--watchdog"])
    return _spawn([PYTHONW, os.path.join(APP_DIR, "bili_watchdog.py")])


def ensure_watchdog():
    if read_watchdog_pid():
        return
    start_watchdog()
    for _ in range(20):
        if read_watchdog_pid():
            return
        time.sleep(0.2)


def port_open(port=8765):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def console_ready():
    try:
        with open(CONSOLE_READY, "r", encoding="utf-8") as fh:
            pid = int(fh.read().split()[0])
        age = time.time() - os.path.getmtime(CONSOLE_READY)
        return bool(pid and age < 30)
    except Exception:
        return False


def start_collection(settings):
    save_settings(settings)
    try:
        os.remove(DISABLED_FILE)
    except OSError:
        pass
    pid, age = read_heartbeat()
    if pid and age >= 120:
        try:
            os.remove(ALIVE_FILE)  # 避免守护进程误以为旧进程还活着
        except OSError:
            pass
    ensure_watchdog()
    for _ in range(25):
        pid, age = read_heartbeat()
        if pid and age < 120:
            return "采集器已启动（PID %d）" % pid
        time.sleep(0.25)
    return "已通知守护进程启动，采集器正在连接…"


def stop_collection():
    with open(DISABLED_FILE, "w", encoding="utf-8") as fh:
        fh.write("stopped by client\n")
    pid, age = read_heartbeat()
    if not pid:
        try:
            with open(PID_FILE, "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip())
        except Exception:
            pid = None
    if pid and age < 120:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       creationflags=CREATE_NO_WINDOW, capture_output=True)
    return "采集已停止；守护进程保留，点击「启动采集」可恢复。"


def configured_data_dir(settings=None):
    settings = settings or load_settings()
    value = str(settings.get("data_dir") or DEFAULT_DATA_DIR).strip() or DEFAULT_DATA_DIR
    value = os.path.expandvars(os.path.expanduser(value))
    if not os.path.isabs(value):
        value = os.path.join(APP_DIR, value)
    return os.path.abspath(value)


def ensure_console_service():
    ready_before = console_ready() or port_open()
    if not ready_before:
        try:
            os.remove(CONSOLE_READY)
        except OSError:
            pass
        proc = _spawn_console()
        for _ in range(80):
            if console_ready() or port_open():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.25)
    if not console_ready() and not port_open():
        detail = ""
        try:
            with open(CONSOLE_LOG, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-12:]
            detail = "".join(lines).strip()
        except OSError:
            pass
        msg = "控制台启动失败，请查看 console.log。"
        if detail:
            msg += "\n\n" + detail
        return False, msg
    return True, "互动记录服务已就绪"


def open_console():
    ok, msg = ensure_console_service()
    if not ok:
        return False, msg
    browser_opened = False
    try:
        os.startfile(URL)
        browser_opened = True
    except Exception:
        pass
    if not browser_opened:
        try:
            browser_opened = bool(webbrowser.open(URL, new=2))
        except Exception:
            pass
    if not browser_opened:
        return False, "互动记录已运行，但默认浏览器未能打开，请手动访问 %s" % URL
    return True, "已打开互动记录：%s" % URL


def stop_console_service():
    try:
        with open(CONSOLE_READY, "r", encoding="utf-8") as fh:
            pid = int(fh.read().split()[0])
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       creationflags=CREATE_NO_WINDOW, capture_output=True)
    except Exception:
        pass
    try:
        os.remove(CONSOLE_READY)
    except OSError:
        pass


def migrate_collection_data(old_dir, new_dir):
    """把当前采集文件复制到新目录；不覆盖目标目录已有文件。"""
    old_dir = os.path.abspath(old_dir)
    new_dir = os.path.abspath(new_dir)
    if old_dir == new_dir:
        return []
    os.makedirs(new_dir, exist_ok=True)
    copied = []
    for source in sorted(glob.glob(os.path.join(old_dir, "live_events*.jsonl"))):
        name = os.path.basename(source)
        target = os.path.join(new_dir, name)
        if os.path.exists(target):
            raise FileExistsError("目标目录已存在 %s，请选择空目录。" % name)
        shutil.copy2(source, target)
        copied.append(name)
    return copied


def generate_report():
    if FROZEN:
        _spawn([sys.executable, "report"])
    else:
        _spawn([PYTHONW, os.path.join(APP_DIR, "bili_live.py"), "report"])
    return "已开始生成报告，完成后会自动在浏览器打开。"


_WM_APP = 0x8000
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_NULL = 0x0000
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_TRAY_CALLBACK = _WM_APP + 20
_NIM_ADD = 0
_NIM_DELETE = 2
_NIF_MESSAGE = 0x0001
_NIF_ICON = 0x0002
_NIF_TIP = 0x0004
_MF_STRING = 0x0000
_MF_SEPARATOR = 0x0800
_TPM_RIGHTBUTTON = 0x0002
_TPM_RETURNCMD = 0x0100
_TRAY_TITLE = "DD轨迹"
_TRAY_SHOW, _TRAY_CONSOLE, _TRAY_REPORT = 1001, 1002, 1003
_TRAY_START, _TRAY_STOP, _TRAY_EXIT = 1004, 1005, 1006
_TRAY_MANUAL = 1007


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
    wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class WinTray:
    """无第三方依赖的 Windows 通知区图标与右键菜单。"""

    def __init__(self, events):
        self.events = events
        self.hwnd = None
        self.hicon = None
        self.nid = None
        self.ok = False
        self.error = None
        self._ready = threading.Event()
        self._thread = None
        self._wndproc = None
        self._taskbar_created = 0
        self._class_name = "BiliLiveWatchTray_%d" % os.getpid()
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._shell32 = ctypes.windll.shell32

    def _bind_apis(self):
        u = self._user32
        k = self._kernel32
        u.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
        u.RegisterClassW.restype = ctypes.c_ushort
        u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.HMENU,
            wintypes.HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.AppendMenuW.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        u.TrackPopupMenu.restype = ctypes.c_int
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        u.RegisterWindowMessageW.restype = wintypes.UINT
        u.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        u.LoadIconW.restype = wintypes.HICON
        self._shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int,
            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON),
            wintypes.UINT]
        self._shell32.ExtractIconExW.restype = wintypes.UINT
        u.DestroyIcon.argtypes = [wintypes.HICON]
        self._shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        self._shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HINSTANCE

    def _load_icon(self):
        large = wintypes.HICON()
        small = wintypes.HICON()
        count = self._shell32.ExtractIconExW(
            sys.executable, 0, ctypes.byref(large), ctypes.byref(small), 1)
        if count:
            self.hicon = small.value or large.value
            return self.hicon
        icon = self._user32.LoadIconW(None, ctypes.c_void_p(32512))
        self.hicon = icon.value if icon else None
        return self.hicon

    def _add_icon(self):
        if self.nid is None:
            return False
        if self._shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(self.nid)):
            self.ok = True
            return True
        self.error = self._kernel32.GetLastError()
        return False

    def _show_menu(self):
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        try:
            self._user32.AppendMenuW(menu, _MF_STRING, _TRAY_SHOW, "打开客户端")
            self._user32.AppendMenuW(menu, _MF_STRING, _TRAY_MANUAL, "手动补录")
            self._user32.AppendMenuW(menu, _MF_STRING, _TRAY_CONSOLE, "互动记录")
            self._user32.AppendMenuW(menu, _MF_STRING, _TRAY_REPORT, "生成报告")
            self._user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, _MF_STRING, _TRAY_START, "启动采集")
            self._user32.AppendMenuW(menu, _MF_STRING, _TRAY_STOP, "停止采集")
            self._user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, _MF_STRING, _TRAY_EXIT,
                                     "退出客户端（采集继续）")
            point = wintypes.POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            self._user32.SetForegroundWindow(self.hwnd)
            command = self._user32.TrackPopupMenu(
                menu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD,
                point.x, point.y, 0, self.hwnd, None)
            self._user32.PostMessageW(self.hwnd, _WM_NULL, 0, 0)
            actions = {
                _TRAY_SHOW: "show", _TRAY_CONSOLE: "console",
                _TRAY_REPORT: "report", _TRAY_START: "start",
                _TRAY_STOP: "stop", _TRAY_EXIT: "exit",
                _TRAY_MANUAL: "manual",
            }
            action = actions.get(command)
            if action:
                self.events.put(action)
        finally:
            self._user32.DestroyMenu(menu)

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if self._taskbar_created and msg == self._taskbar_created:
            self._add_icon()
            return 0
        if msg == _TRAY_CALLBACK:
            mouse_message = lparam & 0xFFFF
            if mouse_message in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                self.events.put("show")
            elif mouse_message == _WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == _WM_CLOSE:
            self._user32.DestroyWindow(hwnd)
            return 0
        if msg == _WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        instance = None
        try:
            instance = self._kernel32.GetModuleHandleW(None)
            self._wndproc = _WNDPROC(self._window_proc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = instance
            wc.lpszClassName = self._class_name
            self._user32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = self._user32.CreateWindowExW(
                0, self._class_name, "BiliLiveWatchTrayHost", 0, 0, 0, 0, 0,
                None, None, instance, None)
            if not self.hwnd:
                return
            icon = self._load_icon()
            if not icon:
                return
            self.nid = _NOTIFYICONDATAW()
            self.nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            self.nid.hWnd = self.hwnd
            self.nid.uID = 1
            self.nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
            self.nid.uCallbackMessage = _TRAY_CALLBACK
            self.nid.hIcon = icon
            self.nid.szTip = _TRAY_TITLE
            self._taskbar_created = self._user32.RegisterWindowMessageW(
                "TaskbarCreated")
            for _ in range(12):
                if self._add_icon():
                    break
                time.sleep(0.5)
            if not self.ok:
                return
            self._ready.set()
            msg = wintypes.MSG()
            while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.error = repr(exc)
        finally:
            self._ready.set()
            if self.nid is not None:
                try:
                    self._shell32.Shell_NotifyIconW(
                        _NIM_DELETE, ctypes.byref(self.nid))
                except Exception:
                    pass
                self.nid = None
            if self.hicon:
                try:
                    self._user32.DestroyIcon(wintypes.HICON(self.hicon))
                except Exception:
                    pass
                self.hicon = None
            self.hwnd = None
            if instance is not None:
                try:
                    self._user32.UnregisterClassW(self._class_name, instance)
                except Exception:
                    pass

    def start(self):
        self._bind_apis()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(8)
        return bool(self.ok)

    def stop(self):
        if self.hwnd:
            self._user32.PostMessageW(self.hwnd, _WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)


def run_gui():
    autostart_hidden = "--autostart" in sys.argv
    # 高 DPI：必须在创建 Tk 窗口之前声明，否则 Windows 会位图拉伸导致发虚
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    if not acquire_client_lock():
        if "--autostart" not in sys.argv:
            focus_existing_client()
        return
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    BG, CARD, CARD2 = "#050912", "#0b1320", "#101c2c"
    FG, DIM, BORDER = "#eaf4ff", "#7f93ad", "#1b3450"
    PINK, CYAN, PURPLE = "#fb7299", "#22d3ee", "#8b5cf6"
    GREEN, RED, AMBER = "#36e3a1", "#ff5d73", "#ffc857"
    DEEP = "#050a12"
    settings = load_settings()

    root = tk.Tk()
    root.title(CLIENT_TITLE)
    root.configure(bg=BG)
    root.resizable(True, True)
    try:
        root.tk.call("tk", "scaling", float(root.winfo_fpixels("1i")) / 72.0)
    except Exception:
        pass
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    win_w = max(760, min(1280, screen_w - 80))
    win_h = max(660, min(1180, screen_h - 40))
    win_x = max(20, (screen_w - win_w) // 2)
    win_y = max(20, (screen_h - win_h) // 2)
    root.geometry("%dx%d+%d+%d" % (win_w, win_h, win_x, win_y))
    root.minsize(min(820, win_w), min(650, win_h))
    if autostart_hidden:
        root.withdraw()
    try:
        if os.path.isfile(ICON_FILE):
            root.iconbitmap(default=ICON_FILE)
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=FG,
                    font=("Microsoft YaHei UI", 11))
    style.configure("Card.TLabel", background=CARD, foreground=FG,
                    font=("Microsoft YaHei UI", 11))
    style.configure("Dim.TLabel", background=CARD, foreground=DIM,
                    font=("Microsoft YaHei UI", 10))
    style.configure("Accent.TButton", background=CYAN, foreground="#06121a",
                    font=("Microsoft YaHei UI", 11, "bold"), padding=(12, 8))
    style.configure("TButton", background=CARD2, foreground=FG,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    focusthickness=0, font=("Microsoft YaHei UI", 10), padding=(10, 7))
    style.map("TButton",
              background=[("active", "#17283d"), ("pressed", "#132237")],
              foreground=[("disabled", DIM)])
    style.map("Accent.TButton",
              background=[("active", "#67e8f9"), ("pressed", "#06b6d4")],
              foreground=[("disabled", "#4d6478")])
    style.configure("TEntry", fieldbackground="#07101b", foreground=FG,
                    insertcolor=CYAN, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, padding=7)
    style.map("TEntry", bordercolor=[("focus", CYAN)])
    style.configure("TCombobox", fieldbackground="#07101b", background=CARD2,
                    foreground=FG, arrowcolor=CYAN, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=5)
    style.map("TCombobox",
              fieldbackground=[("readonly", "#07101b")],
              foreground=[("readonly", FG)],
              bordercolor=[("focus", CYAN)])
    style.configure("TSpinbox", fieldbackground="#07101b", foreground=FG,
                    arrowcolor=CYAN, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, font=("Microsoft YaHei UI", 10), padding=5)
    style.map("TSpinbox", bordercolor=[("focus", CYAN)])
    style.configure("TScrollbar", background=CARD2, troughcolor=DEEP,
                    bordercolor=BG, arrowcolor=CYAN, lightcolor=CARD2,
                    darkcolor=CARD2)
    style.map("TScrollbar", background=[("active", "#1d3a55")])

    def blend(c1, c2, t):
        a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
        b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
        return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))

    def neon_button(parent, text, command, primary=False, accent=CYAN):
        bg = "#0d1928"
        active = blend(bg, accent, 0.20)
        fg = accent if primary else FG
        border = blend(BORDER, accent, 0.42 if primary else 0.24)
        button = tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=active, activeforeground=fg,
            disabledforeground="#53677b", relief="flat", bd=0,
            highlightthickness=1, highlightbackground=border,
            highlightcolor=accent, cursor="hand2", takefocus=True,
            font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
            padx=14, pady=9)
        button._neon_bg = bg
        button._neon_hover = active
        button._neon_fg = fg
        button._neon_border = border
        button._neon_accent = accent

        def hover_in(_event=None):
            if str(button.cget("state")) != "disabled":
                button.configure(background=button._neon_hover,
                                 foreground=button._neon_fg,
                                 highlightbackground=button._neon_accent)

        def hover_out(_event=None):
            if str(button.cget("state")) != "disabled":
                button.configure(background=button._neon_bg,
                                 foreground=button._neon_fg,
                                 highlightbackground=button._neon_border)

        button.bind("<Enter>", hover_in)
        button.bind("<Leave>", hover_out)
        return button

    def set_button_tone(button, primary=False, accent=CYAN):
        button._neon_accent = accent
        button._neon_fg = accent if primary else FG
        button._neon_hover = blend(button._neon_bg, accent, 0.20)
        button._neon_border = blend(BORDER, accent, 0.42 if primary else 0.24)
        button.configure(foreground=button._neon_fg,
                         activeforeground=button._neon_fg,
                         highlightbackground=button._neon_border)

    def cyber_card(parent, accent=BORDER, padx=18, pady=15):
        return tk.Frame(parent, bg=CARD, padx=padx, pady=pady,
                        highlightthickness=1, highlightbackground=accent)

    shell = tk.Frame(root, bg=BG)
    shell.pack(fill="both", expand=True)
    scrollbar = ttk.Scrollbar(shell, orient="vertical")
    scrollbar.pack(side="right", fill="y")
    canvas = tk.Canvas(shell, bg=BG, highlightthickness=0,
                       yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    outer = tk.Frame(canvas, bg=BG, padx=26, pady=20)
    outer_id = canvas.create_window((0, 0), window=outer, anchor="nw")

    def sync_scrollregion(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        total = max(1, outer.winfo_reqheight())
        visible = max(1, canvas.winfo_height())
        if total <= visible:
            scrollbar.pack_forget()
            canvas.yview_moveto(0)
        elif not scrollbar.winfo_ismapped():
            scrollbar.pack(side="right", fill="y", before=canvas)

    def resize_inner(event):
        canvas.itemconfigure(outer_id, width=event.width)
        root.after_idle(sync_scrollregion)

    def on_mousewheel(event):
        if canvas.bbox("all") and outer.winfo_reqheight() > canvas.winfo_height():
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

    canvas.bind("<Configure>", resize_inner)
    outer.bind("<Configure>", sync_scrollregion)
    root.bind("<MouseWheel>", on_mousewheel)

    hero = tk.Canvas(outer, height=188, bg=BG, highlightthickness=0)
    hero.pack(fill="x")
    settings_button = neon_button(
        hero, "⚙  设置", lambda: open_settings_dialog(), accent=CYAN)
    settings_window = hero.create_window(
        0, 0, window=settings_button, anchor="ne", tags=("settings",))

    def draw_hero(event=None):
        width = max(680, int(event.width if event is not None else hero.winfo_width()))
        height = 188
        hero.delete("decor")
        for x in range(0, width, 18):
            t = x / max(1, width - 18)
            color = blend("#07111d", "#171326", t)
            hero.create_rectangle(x, 0, x + 18, height, fill=color,
                                  outline="", tags=("decor",))
        hero.create_polygon(
            width * 0.54, 0, width, 0, width, height, width * 0.38, height,
            fill="#10243a", outline="", stipple="gray25", tags=("decor",))
        hero.create_polygon(
            width * 0.82, 0, width, 0, width, height * 0.74,
            fill="#2a1530", outline="", stipple="gray25", tags=("decor",))
        for gx in range(24, width, 32):
            hero.create_line(gx, 0, gx, height, fill="#0d2034",
                             tags=("decor",))
        for gy in range(14, height, 24):
            hero.create_line(0, gy, width, gy, fill="#0d2034",
                             tags=("decor",))
        hero.create_oval(width - 330, -110, width - 40, 180,
                         outline="#155e75", width=1, tags=("decor",))
        hero.create_oval(width - 290, -74, width - 78, 145,
                         outline="#4c1d4f", width=1, tags=("decor",))
        hero.create_line(24, height - 1, width - 24, height - 1,
                         fill=CYAN, width=1, tags=("decor",))
        hero.create_text(
            26, 22, anchor="nw", text="DD轨迹", fill=FG,
            font=("Microsoft YaHei UI", 25, "bold"), tags=("decor",))
        hero.create_text(
            28, 84, anchor="nw", text="LIVE TRACE  //  互动行为观测终端",
            fill=CYAN, font=("Microsoft YaHei UI", 10, "bold"), tags=("decor",))
        hero.create_text(
            28, 111, anchor="nw",
            text="LOCAL INTELLIGENCE  ·  PRIVATE DATA  ·  REAL-TIME COLLECTOR",
            fill=DIM, font=("Consolas", 9), tags=("decor",))
        hero.create_rectangle(28, 142, 92, 161, fill="#0e2730",
                              outline="#17657a", tags=("decor",))
        hero.create_text(60, 151, text="LOCAL", fill=CYAN,
                         font=("Consolas", 8, "bold"), tags=("decor",))
        hero.create_rectangle(99, 142, 160, 161, fill="#281321",
                              outline="#7a2b43", tags=("decor",))
        hero.create_text(130, 151, text="PRIVATE", fill=PINK,
                         font=("Consolas", 8, "bold"), tags=("decor",))
        hero.coords(settings_window, width - 24, 22)
        hero.tag_raise("settings")

    hero.bind("<Configure>", draw_hero)
    root.after_idle(draw_hero)

    top_area = tk.Frame(outer, bg=BG)
    top_area.pack(fill="x", pady=(14, 0))
    top_area.grid_columnconfigure(0, weight=1, uniform="topcards")
    top_area.grid_columnconfigure(1, weight=1, uniform="topcards")

    account_card = cyber_card(top_area, accent="#3b255d")
    account_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
    tk.Label(account_card, text="身份链路  /  LINK", bg=CARD, fg=PURPLE,
             font=("Consolas", 9, "bold")).grid(
        row=0, column=0, sticky="w")
    account_logged_in = logged_in()
    login_value = tk.Label(
        account_card, text=("登录状态：已登录" if account_logged_in else "登录状态：未登录"),
        bg="#0b211c" if account_logged_in else "#241b0d",
        fg=(GREEN if account_logged_in else AMBER),
        padx=9, pady=3, font=("Microsoft YaHei UI", 12, "bold"))
    login_value.grid(row=0, column=1, columnspan=2, sticky="e")
    identity = load_account_identity() if account_logged_in else {"uname": "", "uid": ""}
    identity_row = tk.Frame(account_card, bg=CARD)
    identity_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    identity_avatar = tk.Canvas(identity_row, width=52, height=52, bg=CARD,
                                highlightthickness=0)
    identity_avatar.pack(side="left", padx=(0, 10))
    identity_avatar.create_oval(1, 1, 51, 51, fill="#211632", outline=PURPLE,
                                width=2)
    avatar_text_id = identity_avatar.create_text(
        26, 26, text="?", fill=PURPLE,
        font=("Microsoft YaHei UI", 15, "bold"))
    avatar_image_id = identity_avatar.create_image(
        26, 26, state="hidden")
    avatar_state = {"photo": None, "request_uid": ""}
    identity_text = tk.Frame(identity_row, bg=CARD)
    identity_text.pack(side="left", fill="x", expand=True)
    account_name_var = tk.StringVar(value=identity.get("uname") or "未登录")
    account_uid_var = tk.StringVar(
        value=("UID " + identity["uid"]) if identity.get("uid") else "UID —")
    tk.Label(identity_text, textvariable=account_name_var, bg=CARD, fg=FG,
             font=("Microsoft YaHei UI", 15, "bold"), anchor="w").pack(
        fill="x")
    tk.Label(identity_text, textvariable=account_uid_var, bg=CARD, fg=DIM,
             font=("Consolas", 9), anchor="w").pack(fill="x", pady=(1, 0))

    def show_cached_avatar(uid, path):
        try:
            photo = tk.PhotoImage(file=path)
            factor = max(1, (min(photo.width(), photo.height()) + 51) // 52)
            if factor > 1:
                photo = photo.subsample(factor, factor)
            avatar_state["photo"] = photo
            identity_avatar.itemconfigure(
                avatar_image_id, image=photo, state="normal")
            identity_avatar.itemconfigure(avatar_text_id, state="hidden")
        except Exception:
            identity_avatar.itemconfigure(avatar_image_id, state="hidden")
            identity_avatar.itemconfigure(avatar_text_id, state="normal")

    def schedule_avatar(face, uid):
        avatar_state["request_uid"] = uid
        path = avatar_cache_path(uid)
        if face and os.path.isfile(path):
            show_cached_avatar(uid, path)
            return
        identity_avatar.itemconfigure(avatar_image_id, state="hidden")
        identity_avatar.itemconfigure(avatar_text_id, state="normal")
        if not face:
            return

        def work():
            try:
                cached = download_account_avatar(face, uid)
            except Exception:
                return
            if avatar_state["request_uid"] != uid:
                try:
                    os.remove(cached)
                except OSError:
                    pass
                return
            if cached:
                root.after(0, lambda: show_cached_avatar(uid, cached))

        threading.Thread(target=work, daemon=True).start()

    def refresh_identity_display(data):
        raw_uname = str((data or {}).get("uname") or "")
        uid = str((data or {}).get("uid") or "")
        face = str((data or {}).get("face") or "")
        uname = raw_uname or ("账号信息待刷新" if uid else "未登录")
        account_name_var.set(uname)
        account_uid_var.set(("UID " + uid) if uid else "UID —")
        identity_avatar.itemconfigure(
            avatar_text_id, text=(raw_uname[:1] if raw_uname else "?"))
        schedule_avatar(face, uid)

    refresh_identity_display(identity)
    tk.Label(account_card,
             text="扫码登录后，凭证仅保存在本机 cookie.txt，无需输入密码。",
             bg=CARD, fg=DIM, font=("Microsoft YaHei UI", 9),
             wraplength=330, justify="left").grid(
        row=2, column=0, columnspan=3, sticky="w", pady=(9, 0))
    tk.Frame(account_card, bg=PURPLE, height=2).grid(
        row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    account_card.grid_columnconfigure(1, weight=1)

    status_card = cyber_card(top_area, accent="#174d4d")
    status_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
    tk.Label(status_card, text="实时采集  /  TELEMETRY", bg=CARD, fg=CYAN,
             font=("Consolas", 9, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w")
    auto_var = tk.BooleanVar(value=autostart_enabled())
    auto_hint = tk.Label(
        status_card,
        text=("AUTO START  ON" if auto_var.get() else "AUTO START  OFF"),
        bg=CARD, fg=GREEN if auto_var.get() else DIM,
        font=("Consolas", 8, "bold"))
    auto_hint.grid(row=0, column=3, sticky="e")
    status_line = tk.Frame(status_card, bg=CARD)
    status_line.grid(row=1, column=0, columnspan=4, sticky="w", pady=(7, 2))
    status_dot = tk.Canvas(status_line, width=20, height=20, bg=CARD,
                           highlightthickness=0)
    status_dot.pack(side="left", padx=(0, 6))
    dot_id = status_dot.create_oval(5, 5, 15, 15, fill=AMBER, outline="")
    status_value = tk.Label(status_line, text="正在检测采集链路…", bg=CARD,
                            fg=AMBER, font=("Microsoft YaHei UI", 15, "bold"))
    status_value.pack(side="left")
    watchdog_value = tk.Label(status_card, text="守护进程：检测中", bg=CARD,
                              fg=DIM, font=("Microsoft YaHei UI", 9))
    watchdog_value.grid(row=2, column=0, columnspan=2, sticky="w")
    heart_value = tk.Label(status_card, text="心跳：—", bg=CARD, fg=DIM,
                           font=("Microsoft YaHei UI", 9))
    heart_value.grid(row=2, column=2, columnspan=2, sticky="e")
    status_card.grid_columnconfigure(2, weight=1)
    status_card.grid_rowconfigure(3, weight=1)
    tk.Frame(status_card, bg=CYAN, height=2).grid(
        row=4, column=0, columnspan=4, sticky="ew", pady=(12, 0))

    follow_var = tk.BooleanVar(value=bool(settings.get("follow", True)))
    interval_var = tk.StringVar(value=str(settings.get("interval", 60)))
    cap_var = tk.StringVar(value=str(settings.get("cap", 25)))

    def collect_settings():
        try:
            interval = max(30, min(600, int(interval_var.get())))
            cap = max(1, min(100, int(cap_var.get())))
        except ValueError:
            messagebox.showerror("输入有误", "刷新间隔和采集数量必须是数字。")
            return None
        interval_var.set(str(interval))
        cap_var.set(str(cap))
        return {"follow": bool(follow_var.get()), "interval": interval, "cap": cap,
                "rotate_mb": int(settings.get("rotate_mb", 64)),
                "keep": int(settings.get("keep", 2))}

    close_options = {"隐藏到托盘": "tray", "彻底退出客户端": "exit"}
    minimize_options = {"隐藏到托盘": "tray", "最小化到任务栏": "taskbar"}
    close_labels = {v: k for k, v in close_options.items()}
    minimize_labels = {v: k for k, v in minimize_options.items()}
    close_label_var = tk.StringVar(
        value=close_labels.get(settings.get("close_behavior"), "隐藏到托盘"))
    minimize_label_var = tk.StringVar(
        value=minimize_labels.get(settings.get("minimize_behavior"), "隐藏到托盘"))

    settings_dialog = {"window": None}

    def open_settings_dialog():
        existing = settings_dialog["window"]
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass

        dlg = tk.Toplevel(root)
        settings_dialog["window"] = dlg
        dlg.title("DD轨迹 · 设置")
        dlg.configure(bg=BG)
        dlg.resizable(True, True)
        dlg.minsize(560, 520)
        dlg.transient(root)
        root.update_idletasks()
        x = root.winfo_rootx() + max(20, (root.winfo_width() - 760) // 2)
        y = root.winfo_rooty() + max(20, (root.winfo_height() - 820) // 2)
        dlg.geometry("760x820+%d+%d" % (x, y))

        d_follow = tk.BooleanVar(value=bool(settings.get("follow", True)))
        d_interval = tk.StringVar(value=str(settings.get("interval", 60)))
        d_cap = tk.StringVar(value=str(settings.get("cap", 25)))
        d_close = tk.StringVar(value=close_labels.get(
            settings.get("close_behavior"), "隐藏到托盘"))
        d_minimize = tk.StringVar(value=minimize_labels.get(
            settings.get("minimize_behavior"), "隐藏到托盘"))
        d_auto = tk.BooleanVar(value=autostart_enabled())
        d_data_dir = tk.StringVar(value=configured_data_dir(settings))

        tk.Label(dlg, text="CONFIGURATION MATRIX", bg=BG, fg=CYAN,
                 font=("Consolas", 9, "bold")).pack(
            anchor="w", padx=24, pady=(17, 1))
        tk.Label(dlg, text="系统设置", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 22, "bold")).pack(
            anchor="w", padx=24, pady=(0, 2))
        tk.Label(dlg, text="启动、采集、窗口和数据目录统一在这里管理。",
                 bg=BG, fg=DIM, font=("Microsoft YaHei UI", 10)).pack(
            anchor="w", padx=24, pady=(0, 10))
        tk.Frame(dlg, bg=BORDER, height=1).pack(
            fill="x", padx=24, pady=(0, 8))

        actions = tk.Frame(dlg, bg=BG)
        actions.pack(side="bottom", fill="x", padx=24, pady=14)

        body_shell = tk.Frame(dlg, bg=BG)
        body_shell.pack(fill="both", expand=True, padx=(24, 12))
        settings_scrollbar = ttk.Scrollbar(
            body_shell, orient="vertical")
        settings_scrollbar.pack(side="right", fill="y", padx=(6, 0))
        settings_canvas = tk.Canvas(
            body_shell, bg=BG, highlightthickness=0,
            yscrollcommand=settings_scrollbar.set)
        settings_canvas.pack(side="left", fill="both", expand=True)
        settings_scrollbar.configure(command=settings_canvas.yview)
        body = tk.Frame(settings_canvas, bg=BG)
        body_window = settings_canvas.create_window(
            (0, 0), window=body, anchor="nw")

        def resize_settings_body(event):
            settings_canvas.itemconfigure(body_window, width=event.width)

        def sync_settings_scroll(_event=None):
            settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))

        def scroll_settings(event):
            if settings_canvas.bbox("all"):
                settings_canvas.yview_scroll(int(-event.delta / 120), "units")
                return "break"

        settings_canvas.bind("<Configure>", resize_settings_body)
        body.bind("<Configure>", sync_settings_scroll)
        dlg.bind("<MouseWheel>", scroll_settings)
        dlg.bind("<Prior>", lambda _e: settings_canvas.yview_scroll(-1, "pages"))
        dlg.bind("<Next>", lambda _e: settings_canvas.yview_scroll(1, "pages"))

        data_card = tk.Frame(body, bg=CARD, padx=18, pady=14,
                             highlightthickness=1, highlightbackground="#24445f")
        data_card.pack(fill="x")
        tk.Label(data_card, text="DATA SOURCE  /  采集数据目录", bg=CARD, fg=CYAN,
                 font=("Consolas", 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        tk.Label(data_card,
                 text="切换后会复制当前 live_events*.jsonl 到新目录，旧文件保留；互动记录服务会关闭并在下次打开时使用新路径。",
                 bg=CARD, fg=DIM, font=("Microsoft YaHei UI", 9),
                 wraplength=670, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))
        ttk.Entry(data_card, textvariable=d_data_dir).grid(
            row=2, column=0, columnspan=3, sticky="ew")
        ttk.Button(data_card, text="选择目录",
                   command=lambda: d_data_dir.set(
                       filedialog.askdirectory(initialdir=d_data_dir.get(),
                                               parent=dlg) or d_data_dir.get())).grid(
            row=3, column=0, sticky="w", pady=(8, 0))
        tk.Label(data_card,
                 text="路径只填写目录；目录内的 live_events.jsonl 是实际数据文件。",
                 bg=CARD, fg=DIM, font=("Microsoft YaHei UI", 9),
                 justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        data_card.grid_columnconfigure(0, weight=1)

        startup_card = tk.Frame(body, bg=CARD, padx=18, pady=14,
                                highlightthickness=1, highlightbackground="#3b255d")
        startup_card.pack(fill="x", pady=(12, 0))
        tk.Label(startup_card, text="BOOT SEQUENCE  /  启动设置", bg=CARD, fg=PURPLE,
                 font=("Consolas", 9, "bold")).grid(
            row=0, column=0, sticky="w")
        tk.Checkbutton(startup_card, text="开机自启，并在登录后隐藏到系统托盘",
                       variable=d_auto, bg=CARD, fg=FG, selectcolor=CARD2,
                       activebackground=CARD, activeforeground=FG,
                       highlightthickness=0,
                       font=("Microsoft YaHei UI", 11)).grid(
            row=1, column=0, sticky="w", pady=(8, 2))
        tk.Label(startup_card,
                 text="勾选后会创建 Windows 启动项；保存设置后立即生效。",
                 bg=CARD, fg=DIM, font=("Microsoft YaHei UI", 9)).grid(
            row=2, column=0, sticky="w")

        collect_card = tk.Frame(body, bg=CARD, padx=18, pady=14,
                                highlightthickness=1, highlightbackground="#174d4d")
        collect_card.pack(fill="x", pady=(12, 0))
        tk.Label(collect_card, text="COLLECTOR CORE  /  采集设置", bg=CARD, fg=CYAN,
                 font=("Consolas", 9, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        tk.Checkbutton(collect_card, text="自动识别关注列表中的开播房间",
                       variable=d_follow, bg=CARD, fg=FG, selectcolor=CARD2,
                       activebackground=CARD, activeforeground=FG,
                       highlightthickness=0,
                       font=("Microsoft YaHei UI", 10)).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 6))
        tk.Label(collect_card, text="刷新间隔（秒）", bg=CARD, fg=FG,
                 font=("Microsoft YaHei UI", 10)).grid(
            row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(collect_card, from_=30, to=600, increment=10, width=10,
                    textvariable=d_interval).grid(
            row=2, column=1, sticky="w", padx=(10, 0), pady=4)
        tk.Label(collect_card, text="最多同时采集（个直播间）", bg=CARD, fg=FG,
                 font=("Microsoft YaHei UI", 10)).grid(
            row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(collect_card, from_=1, to=100, increment=1, width=10,
                    textvariable=d_cap).grid(
            row=3, column=1, sticky="w", padx=(10, 0), pady=4)
        window_card = tk.Frame(body, bg=CARD, padx=18, pady=14,
                               highlightthickness=1, highlightbackground="#5a2a3d")
        window_card.pack(fill="x", pady=(12, 0))
        tk.Label(window_card, text="WINDOW BEHAVIOR  /  窗口设置", bg=CARD, fg=PINK,
                 font=("Consolas", 9, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        tk.Label(window_card, text="点击关闭按钮", bg=CARD, fg=FG,
                 font=("Microsoft YaHei UI", 10)).grid(
            row=1, column=0, sticky="w", pady=6)
        ttk.Combobox(window_card, textvariable=d_close, state="readonly",
                     values=list(close_options), width=22).grid(
            row=1, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=6)
        tk.Label(window_card, text="点击最小化按钮", bg=CARD, fg=FG,
                 font=("Microsoft YaHei UI", 10)).grid(
            row=2, column=0, sticky="w", pady=6)
        ttk.Combobox(window_card, textvariable=d_minimize, state="readonly",
                     values=list(minimize_options), width=22).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=6)

        def close_settings():
            settings_dialog["window"] = None
            try:
                dlg.grab_release()
            except Exception:
                pass
            dlg.destroy()

        def save_all_settings():
            try:
                interval = max(30, min(600, int(d_interval.get())))
                cap = max(1, min(100, int(d_cap.get())))
            except ValueError:
                messagebox.showerror("输入有误", "刷新间隔和采集数量必须是数字。",
                                     parent=dlg)
                return
            new_dir = os.path.abspath(os.path.expandvars(
                os.path.expanduser(d_data_dir.get().strip() or APP_DIR)))
            old_dir = configured_data_dir(load_settings())
            data_changed = os.path.normcase(new_dir) != os.path.normcase(old_dir)
            target_main = os.path.join(new_dir, "live_events.jsonl")
            use_existing = False
            if data_changed and os.path.exists(target_main):
                use_existing = messagebox.askyesno(
                    "目标目录已有采集数据",
                    "目标目录已有 live_events.jsonl。\n"
                    "是否直接切换并继续使用该目录现有数据？\n"
                    "当前目录的数据不会被删除。",
                    parent=dlg)
                if not use_existing:
                    return
            try:
                os.makedirs(new_dir, exist_ok=True)
            except Exception as exc:
                messagebox.showerror("目录不可用", str(exc), parent=dlg)
                return

            was_running = (not os.path.isfile(DISABLED_FILE)
                           and read_heartbeat()[1] < 120)
            was_console = console_ready() or port_open()
            if data_changed:
                if was_console:
                    stop_console_service()
                if was_running:
                    stop_collection()
                try:
                    if not use_existing:
                        migrate_collection_data(old_dir, new_dir)
                except Exception as exc:
                    if was_running:
                        start_collection(load_settings())
                    messagebox.showerror("迁移数据失败", str(exc), parent=dlg)
                    return

            new_settings = {
                "follow": bool(d_follow.get()),
                "interval": interval,
                "cap": cap,
                "close_behavior": close_options[d_close.get()],
                "minimize_behavior": minimize_options[d_minimize.get()],
                "data_dir": new_dir,
            }
            save_settings(new_settings)
            settings.update(new_settings)
            follow_var.set(new_settings["follow"])
            interval_var.set(str(interval))
            cap_var.set(str(cap))
            close_label_var.set(d_close.get())
            minimize_label_var.set(d_minimize.get())
            auto_var.set(bool(d_auto.get()))
            try:
                set_autostart(bool(d_auto.get()))
                auto_hint.configure(
                    text=("AUTO START  ON" if d_auto.get()
                          else "AUTO START  OFF"),
                    fg=GREEN if d_auto.get() else DIM)
                if data_changed:
                    import bili_live as live
                    live.configure_data_paths(new_dir)
                    if was_running:
                        start_collection(load_settings())
                    log("设置已保存；采集数据目录已切换，互动记录将在下次打开时使用新目录。")
                else:
                    log("设置已保存；采集参数将在下次启动采集时生效。")
            except Exception as exc:
                messagebox.showerror("应用设置失败", str(exc), parent=dlg)
                return
            close_settings()

        neon_button(actions, "取消", close_settings, accent=RED).pack(side="right")
        neon_button(actions, "保存配置", save_all_settings,
                    primary=True, accent=CYAN).pack(side="right", padx=(0, 10))
        dlg.protocol("WM_DELETE_WINDOW", close_settings)
        dlg.lift()
        dlg.focus_force()
        dlg.grab_set()

    login_dialog = {"window": None}

    def open_login_dialog():
        existing = login_dialog["window"]
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass
        try:
            import bili_login
        except Exception as exc:
            messagebox.showerror("登录组件不可用", str(exc))
            return
        dlg = tk.Toplevel(root)
        dlg.title("DD轨迹 · 扫码登录")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(root)
        login_dialog["window"] = dlg
        root.update_idletasks()
        login_x = root.winfo_rootx() + max(20, (root.winfo_width() - 520) // 2)
        login_y = root.winfo_rooty() + max(20, (root.winfo_height() - 760) // 2)
        dlg.geometry("520x760+%d+%d" % (login_x, login_y))
        tk.Label(dlg, text="IDENTITY LINK  /  B 站扫码", bg=BG, fg=CYAN,
                 font=("Consolas", 9, "bold")).pack(pady=(14, 1))
        tk.Label(dlg, text="扫码登录 B 站", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 19, "bold")).pack(pady=(0, 4))
        tk.Label(dlg, text="用 B 站手机 App 扫码，并在手机上确认登录。",
                 bg=BG, fg=DIM, font=("Microsoft YaHei UI", 10)).pack()
        qr_label = tk.Label(dlg, bg="#ffffff", width=300, height=300,
                            text="二维码加载中…", fg="#333333",
                            font=("Microsoft YaHei UI", 12),
                            highlightthickness=2, highlightbackground=CYAN)
        qr_label.pack(pady=12)
        status_var = tk.StringVar(value="正在生成二维码…")
        status_label = tk.Label(dlg, textvariable=status_var, bg=BG, fg=AMBER,
                                font=("Microsoft YaHei UI", 11), wraplength=460)
        status_label.pack(pady=(0, 8))
        state = {"cancel": threading.Event()}
        img_holder = {"img": None}

        def set_status(text, color=DIM):
            root.after(0, lambda: (status_var.set(text),
                                   status_label.configure(fg=color)))

        def stop_poll():
            state["cancel"].set()

        def show_qr():
            try:
                img = tk.PhotoImage(file=bili_login.QR_PNG)
                factor = max(1, (img.width() + 299) // 300)
                if factor > 1:
                    img = img.subsample(factor, factor)
                img_holder["img"] = img
                qr_label.configure(image=img, width=img.width(), height=img.height(),
                                   text="")
            except Exception as exc:
                set_status("二维码显示失败：%s" % exc, RED)

        def poll():
            ev = state["cancel"]
            try:
                with open(bili_login.KEY_FILE, "r", encoding="utf-8") as fh:
                    key = fh.read().strip()
            except Exception:
                set_status("二维码已失效，请重新生成", RED)
                return
            deadline, last = time.time() + 180, None
            while not ev.is_set() and time.time() < deadline:
                try:
                    data, set_cookie = bili_login.request(bili_login.POLL % key)
                except Exception as exc:
                    set_status("网络错误：%s" % exc, RED)
                    return
                payload = data.get("data") or {}
                code = payload.get("code")
                if code == 0:
                    cookie = bili_login.extract_cookie(data, set_cookie)
                    if "SESSDATA=" in cookie:
                        with open(bili_login.COOKIE_FILE, "w", encoding="utf-8") as fh:
                            fh.write(cookie)
                        set_status("登录成功，凭证已保存", GREEN)
                        identity_data = fetch_account_identity()
                        root.after(0, lambda: login_value.configure(
                            text="登录状态：已登录", fg=GREEN, bg="#0b211c"))
                        root.after(0, lambda: set_auth_button_state(True))
                        root.after(0, lambda d=identity_data:
                                   refresh_identity_display(d))
                        root.after(0, lambda: log("B 站扫码登录成功，cookie.txt 已更新。"))
                        ev.set()
                        return
                    set_status("登录成功但未取到 SESSDATA", RED)
                    return
                if code == 86038:
                    set_status("二维码已过期，请重新生成", RED)
                    return
                msg = {86101: "等待扫码…", 86090: "已扫码，请在手机上确认"}.get(
                    code, "等待结果…")
                if msg != last:
                    set_status(msg)
                    last = msg
                time.sleep(2)
            if not ev.is_set():
                set_status("等待超时，请重新生成", RED)

        def generate():
            state["cancel"].set()
            state["cancel"] = threading.Event()
            img_holder["img"] = None
            qr_label.configure(image="")
            set_status("正在生成二维码…", AMBER)

            def work():
                try:
                    if bili_login.cmd_qr() != 0:
                        set_status("二维码生成失败", RED)
                        return
                    root.after(0, show_qr)
                    set_status("等待扫码…", AMBER)
                    poll()
                except Exception as exc:
                    set_status("二维码生成失败：%s" % exc, RED)

            threading.Thread(target=work, daemon=True).start()

        def close_dialog():
            stop_poll()
            login_dialog["window"] = None
            try:
                dlg.grab_release()
            except Exception:
                pass
            dlg.destroy()

        neon_button(dlg, "重新生成二维码", generate, accent=PURPLE).pack(pady=(4, 2))
        neon_button(dlg, "关闭", close_dialog, accent=RED).pack(pady=(2, 12))
        dlg.protocol("WM_DELETE_WINDOW", close_dialog)

        def force_login_window():
            try:
                hwnd = ctypes.windll.user32.FindWindowW(None, "扫码登录 B 站")
                if hwnd:
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, -1, login_x, login_y, 520, 760, 0x0004 | 0x0040)
            except Exception:
                pass

        dlg.lift()
        dlg.focus_force()
        dlg.grab_set()
        dlg.after(350, force_login_window)
        dlg.after(450, lambda: dlg.attributes("-topmost", False))
        generate()

    auth_button = neon_button(
        account_card,
        "退出登录" if account_logged_in else "扫码登录",
        lambda: on_auth_action(),
        primary=True,
        accent=RED if account_logged_in else PURPLE)
    auth_button.grid(row=3, column=2, sticky="e", pady=(9, 0))

    action_card = cyber_card(outer, accent="#203752", padx=16, pady=14)
    action_card.pack(fill="x", pady=(14, 0))
    action_head = tk.Frame(action_card, bg=CARD)
    action_head.pack(fill="x", pady=(0, 8))
    tk.Label(action_head, text="CONTROL MATRIX  /  快捷控制", bg=CARD, fg=CYAN,
             font=("Consolas", 9, "bold")).pack(side="left")
    tk.Label(action_head, text="LOCAL CONTROL", bg=CARD, fg=DIM,
             font=("Consolas", 8)).pack(side="right")
    controls = tk.Frame(action_card, bg=CARD)
    controls.pack(fill="x")
    controls.grid_columnconfigure(0, weight=1)
    controls.grid_columnconfigure(1, weight=1)
    controls.grid_columnconfigure(2, weight=1)

    def run_async(label, fn, done=None):
        log(label)

        def work():
            try:
                msg = fn()
            except Exception as exc:
                msg = "操作失败：%s" % exc
            if msg:
                root.after(0, lambda: log(msg))
            if done:
                root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    logout_state = {"running": False}

    def set_auth_button_state(is_logged_in):
        auth_button.configure(
            text="退出登录" if is_logged_in else "扫码登录",
            state="normal")
        set_button_tone(
            auth_button, primary=True, accent=RED if is_logged_in else PURPLE)

    def on_auth_action():
        if logout_state["running"]:
            return
        if logged_in():
            on_logout()
        else:
            open_login_dialog()

    def on_logout():
        if logout_state["running"]:
            return
        if not logged_in():
            login_value.configure(
                text="登录状态：未登录", fg=AMBER, bg="#241b0d")
            refresh_identity_display({"uname": "", "uid": "", "face": ""})
            set_auth_button_state(False)
            return
        if not messagebox.askyesno(
                "退出登录",
                "退出后会停止采集，并删除本机登录凭证、身份缓存和头像缓存。\n"
                "历史采集数据不会删除。之后需要重新扫码登录。\n\n"
                "确定退出登录吗？",
                parent=root):
            return
        logout_state["running"] = True
        avatar_state["request_uid"] = ""
        auth_button.configure(state="disabled")

        def action():
            stop_error = None
            try:
                stop_collection()
            except Exception as exc:
                stop_error = str(exc)
            removed = clear_login_files()
            if stop_error:
                return "已清除登录信息，但停止采集失败：%s" % stop_error
            detail = ("，清除 %d 个登录缓存文件" % len(removed)) if removed else ""
            return "已退出登录；采集已停止%s。历史采集数据已保留。" % detail

        def finish():
            logout_state["running"] = False
            login_value.configure(
                text="登录状态：未登录", fg=AMBER, bg="#241b0d")
            refresh_identity_display({"uname": "", "uid": "", "face": ""})
            set_auth_button_state(False)

        run_async("正在退出登录…", action, done=finish)

    def on_start():
        s = collect_settings()
        if s:
            run_async("正在启动采集…", lambda: start_collection(s))

    def on_stop():
        run_async("正在停止采集…", stop_collection)

    def on_collection_toggle():
        pid, age = read_heartbeat()
        running = (pid and age < 120 and not os.path.isfile(DISABLED_FILE))
        if running:
            on_stop()
        else:
            on_start()

    def on_console():
        def action():
            ok, msg = open_console()
            if not ok:
                root.after(0, lambda: messagebox.showerror(
                    "互动记录启动失败", msg, parent=root))
            return msg
        run_async("正在打开互动记录…", action)

    def on_report():
        run_async("正在生成报告…", generate_report)

    def on_assets():
        def action():
            import bili_live as live
            live.cmd_snapshot()
            return "直播资产已刷新"
        run_async("正在刷新直播资产…", action)

    sync_state = {"running": False}

    def on_sync_history():
        import danmaku_history_sync as sync
        if sync_state["running"]:
            return
        sync_state["running"] = True
        sync_button.configure(state="disabled")

        def action():
            try:
                was_console = console_ready() or port_open()
                audit = sync.sync_history(
                    progress=lambda msg: root.after(0, lambda m=msg: log(m)))
                if was_console:
                    stop_console_service()
                    ensure_console_service()
                generate_report()
                return ("danmaku同步完成：匹配 %d 条，本次新增 %d 条，"
                        "当前补充集 %d 条；报告正在生成。" %
                        (audit["matched"], audit["remote_only_added"],
                         audit["supplement_total"]))
            except sync.DailySyncLimitExceeded:
                root.after(0, lambda: messagebox.showwarning(
                    "同步danmaku数据", "已到当日使用上限", parent=root))
                return None

        def finish():
            sync_state["running"] = False
            sync_button.configure(state="normal")

        run_async("正在同步danmaku数据…", action, done=finish)

    manual_dialog = {"window": None}

    def open_manual_dialog():
        existing = manual_dialog["window"]
        if existing is not None:
            try:
                if existing.winfo_exists():
                    show_window()
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass
        dlg = tk.Toplevel(root)
        manual_dialog["window"] = dlg
        dlg.title("DD轨迹 · 手动补录")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(root)
        root.update_idletasks()
        x = root.winfo_rootx() + max(20, (root.winfo_width() - 720) // 2)
        y = root.winfo_rooty() + max(20, (root.winfo_height() - 820) // 2)
        dlg.geometry("720x820+%d+%d" % (x, y))

        tk.Label(dlg, text="EVENT INJECTOR  /  手动补录", bg=BG, fg=CYAN,
                 font=("Consolas", 9, "bold")).pack(
            anchor="w", padx=24, pady=(16, 0))
        tk.Label(dlg, text="手动补录", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 19, "bold")).pack(
            anchor="w", padx=24, pady=(0, 2))
        tk.Label(dlg, text="仅写入当前登录账号；弹幕、礼物、SC、上舰和进入均可补录。",
                 bg=BG, fg=DIM, font=("Microsoft YaHei UI", 10)).pack(
            anchor="w", padx=24, pady=(0, 12))

        actions = tk.Frame(dlg, bg=BG)
        actions.pack(side="bottom", fill="x", padx=24, pady=14)
        form = tk.Frame(dlg, bg=CARD, padx=20, pady=16,
                        highlightthickness=1, highlightbackground="#24445f")
        form.pack(fill="both", expand=True, padx=24)
        form.grid_columnconfigure(1, weight=1)
        form.grid_columnconfigure(3, weight=1)

        def form_label(text, row, column=0):
            tk.Label(form, text=text, bg=CARD, fg=FG,
                     font=("Microsoft YaHei UI", 10)).grid(
                row=row, column=column, sticky="w", pady=6)

        time_var = tk.StringVar(value=time.strftime("%Y-%m-%d %H:%M:%S"))
        room_var = tk.StringVar()
        type_var = tk.StringVar(value="礼物")
        content_var = tk.StringVar()
        amount_var = tk.StringVar()
        quantity_var = tk.StringVar(value="1")
        estimated_var = tk.BooleanVar(value=False)

        form_label("时间", 0)
        ttk.Entry(form, textvariable=time_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=6)
        form_label("直播间号", 1)
        ttk.Entry(form, textvariable=room_var).grid(
            row=1, column=1, sticky="ew", pady=6, padx=(0, 20))
        form_label("类型", 1, 2)
        type_options = {
            "礼物": "gift", "醒目留言": "sc", "上舰": "guard",
            "弹幕": "danmu", "进入直播间": "entry",
        }
        ttk.Combobox(form, textvariable=type_var, state="readonly",
                     values=list(type_options)).grid(
            row=1, column=3, sticky="ew", pady=6)

        content_label_var = tk.StringVar(value="礼物名称")
        tk.Label(form, textvariable=content_label_var, bg=CARD, fg=FG,
                 font=("Microsoft YaHei UI", 10)).grid(
            row=2, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=content_var).grid(
            row=2, column=1, columnspan=3, sticky="ew", pady=6)

        form_label("金额（元）", 3)
        amount_entry = ttk.Entry(form, textvariable=amount_var)
        amount_entry.grid(row=3, column=1, sticky="ew", pady=6, padx=(0, 20))
        form_label("数量", 3, 2)
        ttk.Spinbox(form, from_=1, to=9999, textvariable=quantity_var).grid(
            row=3, column=3, sticky="ew", pady=6)

        form_label("备注", 4)
        note_text = tk.Text(form, height=4, bg=DEEP, fg=FG,
                            insertbackground=FG, relief="flat", wrap="word",
                            font=("Microsoft YaHei UI", 10))
        note_text.grid(row=4, column=1, columnspan=3, sticky="ew", pady=6)
        tk.Checkbutton(form, text="这条记录的时间或金额为估算", variable=estimated_var,
                       bg=CARD, fg=FG, selectcolor=CARD2, activebackground=CARD,
                       activeforeground=FG,
                       font=("Microsoft YaHei UI", 10)).grid(
            row=5, column=1, columnspan=3, sticky="w", pady=(2, 6))
        tk.Label(form,
                 text="本地接口：GET /api/manual 查看格式；POST /api/manual 提交 JSON。",
                 bg=CARD, fg=DIM, font=("Microsoft YaHei UI", 9)).grid(
            row=6, column=0, columnspan=4, sticky="w", pady=(4, 0))

        def update_type_fields(_event=None):
            kind = type_options[type_var.get()]
            if kind == "gift":
                content_label_var.set("礼物名称")
                content_var.set("粉丝团灯牌")
                amount_var.set("0.10")
                amount_entry.configure(state="normal")
            elif kind == "sc":
                content_label_var.set("醒目留言内容")
                content_var.set("")
                amount_var.set("")
                amount_entry.configure(state="normal")
            elif kind == "guard":
                content_label_var.set("上舰类型")
                content_var.set("舰长")
                amount_var.set("138")
                amount_entry.configure(state="normal")
            elif kind == "danmu":
                content_label_var.set("弹幕内容")
                content_var.set("")
                amount_var.set("0")
                amount_entry.configure(state="disabled")
            else:
                content_label_var.set("进入内容")
                content_var.set("进入直播间")
                amount_var.set("0")
                amount_entry.configure(state="disabled")

        type_widget = form.grid_slaves(row=1, column=3)[0]
        type_widget.bind("<<ComboboxSelected>>", update_type_fields)

        def close_manual():
            manual_dialog["window"] = None
            dlg.destroy()

        def save_manual():
            kind = type_options[type_var.get()]
            note = note_text.get("1.0", "end").strip() or "客户端手动补录"
            payload = {
                "time": time_var.get().strip(),
                "room": room_var.get().strip(),
                "type": kind,
                "content": content_var.get().strip(),
                "amount": amount_var.get().strip() or 0,
                "quantity": quantity_var.get().strip() or 1,
                "estimated": 1 if estimated_var.get() else 0,
                "note": note,
            }
            try:
                import bili_live as live
                event = live.append_manual_event(payload)
            except Exception as exc:
                messagebox.showerror("补录失败", str(exc), parent=dlg)
                return
            log("手动补录已保存：%s / %s / %.2f 元" % (
                event.get("room"), event.get("cmd"), live.yuan_of(event)))
            close_manual()

        neon_button(actions, "取消", close_manual, accent=RED).pack(side="right")
        neon_button(actions, "保存补录", save_manual,
                    primary=True, accent=CYAN).pack(side="right", padx=(0, 10))
        dlg.protocol("WM_DELETE_WINDOW", close_manual)
        update_type_fields()
        dlg.lift()
        dlg.focus_force()
        dlg.grab_set()

    collection_button = neon_button(
        controls, "▶  启动采集", on_collection_toggle,
        primary=True, accent=CYAN)
    collection_button.grid(
        row=0, column=0, sticky="ew", padx=(0, 4), pady=4)
    neon_button(controls, "◉  互动记录", on_console, accent=CYAN).grid(
        row=0, column=1, sticky="ew", padx=4, pady=4)
    neon_button(controls, "▣  生成报告", on_report,
                primary=True, accent=PINK).grid(
        row=0, column=2, sticky="ew", padx=(4, 0), pady=4)
    neon_button(controls, "↻  刷新资产", on_assets, accent=GREEN).grid(
        row=1, column=0, sticky="ew", padx=(0, 4), pady=4)
    neon_button(controls, "＋  手动补录", open_manual_dialog, accent=PURPLE).grid(
        row=1, column=1, sticky="ew", padx=4, pady=4)
    sync_button = neon_button(
        controls, "⇄  同步danmaku数据", on_sync_history, accent=AMBER)
    sync_button.grid(row=1, column=2, sticky="ew", padx=(4, 0), pady=4)

    log_card = cyber_card(outer, accent="#15334a", padx=14, pady=12)
    log_card.pack(fill="x", pady=(12, 0))
    log_head = tk.Frame(log_card, bg=CARD)
    log_head.pack(fill="x", pady=(0, 7))
    tk.Label(log_head, text="SYSTEM FEED  /  运行日志", bg=CARD, fg=CYAN,
             font=("Consolas", 9, "bold")).pack(side="left")
    tk.Label(log_head, text="LOCAL BUFFER", bg=CARD, fg=DIM,
             font=("Consolas", 8)).pack(side="right")
    log_shell = tk.Frame(log_card, bg=DEEP, highlightthickness=1,
                         highlightbackground="#12304a")
    log_shell.pack(fill="x")
    log_scroll = ttk.Scrollbar(log_shell, orient="vertical")
    log_scroll.pack(side="right", fill="y")
    log_box = tk.Text(log_shell, height=6, bg=DEEP, fg="#7dd3fc", relief="flat",
                      insertbackground=CYAN, font=("Consolas", 10), wrap="word",
                      selectbackground="#164e63", selectforeground=FG,
                      yscrollcommand=log_scroll.set, padx=10, pady=8)
    log_box.pack(side="left", fill="x", expand=True)
    log_scroll.configure(command=log_box.yview)
    log_box.configure(state="disabled")
    footer = tk.Frame(outer, bg=BG)
    footer.pack(fill="x", pady=(8, 0))
    tk.Label(footer, text="●  LOCAL MODE", bg=BG, fg=GREEN,
             font=("Consolas", 8, "bold")).pack(side="left")
    tk.Label(footer, text="后台采集持续运行",
             bg=BG, fg=DIM, font=("Microsoft YaHei UI", 8)).pack(side="right")

    def log(msg):
        stamp = time.strftime("%H:%M:%S")
        log_box.configure(state="normal")
        log_box.insert("end", "[%s] %s\n" % (stamp, msg))
        log_box.see("end")
        log_box.configure(state="disabled")

    def refresh():
        pid, age = read_heartbeat()
        disabled = os.path.isfile(DISABLED_FILE)
        if disabled:
            status_value.configure(text="已停止（手动暂停）", fg=AMBER)
            status_dot.itemconfigure(dot_id, fill=AMBER, outline="#5a4217")
            collection_button.configure(text="▶  启动采集")
            set_button_tone(collection_button, primary=True, accent=CYAN)
        elif pid and age < 120:
            status_value.configure(text="数据流在线 · PID %d" % pid, fg=GREEN)
            status_dot.itemconfigure(dot_id, fill=GREEN, outline="#14533d")
            collection_button.configure(text="■  停止采集")
            set_button_tone(collection_button, primary=True, accent=RED)
        else:
            status_value.configure(text="数据流离线", fg=RED)
            status_dot.itemconfigure(dot_id, fill=RED, outline="#5a1e29")
            collection_button.configure(text="▶  启动采集")
            set_button_tone(collection_button, primary=True, accent=CYAN)
        wd = read_watchdog_pid()
        watchdog_value.configure(text="守护进程：运行中（PID %d）" % wd if wd else "守护进程：未运行")
        heart_value.configure(text=("心跳：%.0f 秒前" % age) if pid else "心跳：—")
        root.after(2500, refresh)

    tray_events = queue.Queue()
    tray = WinTray(tray_events)
    tray_available = tray.start()
    closing = {"value": False}

    def show_window():
        if closing["value"]:
            return
        root.deiconify()
        try:
            root.state("normal")
        except Exception:
            pass
        root.lift()
        root.after(50, root.focus_force)

    def hide_to_tray():
        if closing["value"] or not tray_available:
            return
        root.withdraw()

    def exit_client():
        if closing["value"]:
            return
        closing["value"] = True
        tray.stop()
        root.destroy()

    def on_close_request():
        behavior = close_options.get(close_label_var.get(), "tray")
        if behavior == "exit" or not tray_available:
            exit_client()
        else:
            hide_to_tray()

    def on_unmap(event):
        behavior = minimize_options.get(minimize_label_var.get(), "tray")
        if (behavior == "tray" and tray_available and event.widget is root
                and not closing["value"] and root.state() == "iconic"):
            root.after(60, hide_to_tray)

    def process_tray_events():
        if closing["value"]:
            return
        while True:
            try:
                action = tray_events.get_nowait()
            except queue.Empty:
                break
            if action == "show":
                show_window()
            elif action == "manual":
                show_window()
                open_manual_dialog()
            elif action == "console":
                on_console()
            elif action == "report":
                on_report()
            elif action == "start":
                on_start()
            elif action == "stop":
                on_stop()
            elif action == "exit":
                exit_client()
                return
        root.after(100, process_tray_events)

    root.protocol("WM_DELETE_WINDOW", on_close_request)
    if tray_available:
        root.bind("<Unmap>", on_unmap)
        process_tray_events()
    else:
        if autostart_hidden:
            root.deiconify()
            log("系统托盘初始化失败，客户端保持窗口显示。")

    # 兼容旧版启动项，并确保它指向当前 DD轨迹 可执行文件。
    if autostart_enabled() and os.path.isfile(LEGACY_STARTUP_FILE):
        try:
            set_autostart(True)
        except OSError:
            pass
    if FROZEN and autostart_enabled():
        try:
            old = read_startup_script(STARTUP_FILE)
            if sys.executable not in old or "--autostart" not in old:
                set_autostart(True)
        except Exception:
            pass

    log("客户端已启动。")
    if autostart_hidden:
        def start_service():
            try:
                ensure_watchdog()
                msg = "开机自启完成：后台采集守护已就绪。"
            except Exception as exc:
                msg = "开机自启启动后台采集失败：%s" % exc
            root.after(0, lambda: log(msg))

        threading.Thread(target=start_service, daemon=True).start()
    if "--login" in sys.argv:
        show_window()
        root.after(1500, open_login_dialog)
    if "--manual" in sys.argv:
        show_window()
        root.after(500, open_manual_dialog)
    if "--settings" in sys.argv:
        show_window()
        root.after(500, open_settings_dialog)
    refresh()
    root.mainloop()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--watchdog":
        import bili_watchdog
        bili_watchdog.main()
        return
    if args and args[0] == "--ui":
        log_fh = None
        if sys.stdout is None or sys.stderr is None:
            try:
                log_fh = open(CONSOLE_LOG, "a", encoding="utf-8", buffering=1)
                sys.stdout = log_fh
                sys.stderr = log_fh
            except OSError:
                pass
        try:
            import bili_live_ui
            sys.argv = ["bili_live_ui.py"] + args[1:]
            bili_live_ui.main()
        except Exception:
            traceback.print_exc()
            raise
        finally:
            if log_fh:
                log_fh.flush()
        return
    if args and args[0] in (
            "collect", "report", "snapshot", "discover", "add", "verify",
            "sync-danmakus"):
        import bili_live
        sys.argv = ["bili_live.py"] + args
        raise SystemExit(bili_live.main())
    run_gui()


if __name__ == "__main__":
    main()
