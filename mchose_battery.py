# -*- coding: utf-8 -*-
"""
MCHOSE 无线外设电量托盘监控
------------------------------------------------
支持设备（协议为逆向官方网页驱动得到，仅适用于同型号）：
  * 迈从 A7 鼠标      VID 0x5253 / PID 0x1021
      接口 usagePage 0xFF01
      命令 Feature Report 0x11，载荷 0x0600（其余字节 0xFF 填充）
      回包 65 字节，去掉前 2 字节后逐字节 XOR 0xFF，第 11 字节 = 电量%
  * 迈从 G87 键盘     VID 0x41E4 / PID 0x2001
      接口 usagePage 0xFF04
      Feature Report 0x06，8 字节，第 3 字节 = 电量%

托盘只占一个图标：外圈=鼠标，内圈=键盘，鼠标悬停看百分比。
"""

import os
import sys
import time
import threading
import subprocess
from pathlib import Path

import hid
from PIL import Image, ImageDraw
import pystray

# ---------------------------------------------------------------- 配置
ICON_SIZE = 64
REFRESH_SEC = 3600        # 多久读一次（秒），1 小时
LOW_BATTERY = 10          # 低于等于这个百分比时弹一次提醒
LOW_COLOR_LIMIT = 15      # 低于等于这个百分比图标变红
MID_COLOR_LIMIT = 30      # 低于等于这个百分比图标变黄

APP_NAME = "2.4G设备电量"
LNK_NAME = "2.4G设备电量.lnk"


# ---------------------------------------------------------------- HID 协议
def _find_path(vid, usage_page):
    for d in hid.enumerate(vid):
        if d.get("usage_page") == usage_page:
            return d["path"]
    return None


def read_a7():
    """迈从 A7 鼠标电量，读不到返回 None"""
    path = _find_path(0x5253, 0xFF01)
    if not path:
        return None
    dev = hid.device()
    try:
        dev.open_path(path)
        # 0x0600 查询：首字节是 reportId，其后为命令字节取反
        dev.send_feature_report(bytes([0x11, 0xF9, 0xFF] + [0xFF] * 17))
        time.sleep(0.12)
        resp = dev.get_feature_report(0x11, 65)
        if len(resp) < 14:
            return None
        dec = [b ^ 0xFF for b in resp[2:]]
        val = dec[11]
        return val if 0 <= val <= 100 else None
    except Exception:
        return None
    finally:
        try:
            dev.close()
        except Exception:
            pass


def read_g87():
    """迈从 G87 键盘电量，读不到返回 None"""
    path = _find_path(0x41E4, 0xFF04)
    if not path:
        return None
    dev = hid.device()
    try:
        dev.open_path(path)
        resp = dev.get_feature_report(0x06, 8)
        if len(resp) < 4:
            return None
        val = resp[3]
        return val if 0 <= val <= 100 else None
    except Exception:
        return None
    finally:
        try:
            dev.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 图标绘制
def _fg_color():
    """跟随任务栏主题：深色任务栏用白线，浅色用深灰线，跟系统图标一致"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        light, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        winreg.CloseKey(key)
        return (32, 32, 32, 255) if light else (255, 255, 255, 255)
    except Exception:
        return (255, 255, 255, 255)


def _arc_color(v, fg):
    if v is None:
        return fg
    if v <= LOW_COLOR_LIMIT:
        return (255, 90, 90, 255)
    if v <= MID_COLOR_LIMIT:
        return (255, 196, 66, 255)
    return fg


def make_icon(mouse, keyboard):
    """两个同心细线圆环：外圈鼠标，内圈键盘。平时纯白/纯黑，低电量才变色。"""
    s = ICON_SIZE
    ss = 4  # 超采样，缩小后线条才不毛糙
    big = Image.new("RGBA", (s * ss, s * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    fg = _fg_color()
    track = (fg[0], fg[1], fg[2], 46)

    def ring(x0, y0, x1, y1, width, val):
        box = [x0 * ss, y0 * ss, x1 * ss, y1 * ss]
        w = max(1, int(round(width * ss)))
        d.ellipse(box, outline=track, width=w)
        if val is not None and val > 0:
            d.arc(
                box,
                start=-90,
                end=-90 + 360 * min(val, 100) / 100,
                fill=_arc_color(val, fg),
                width=w,
            )

    ring(3, 3, s - 4, s - 4, 3.8, mouse)          # 外圈：鼠标
    ring(19, 19, s - 20, s - 20, 3.0, keyboard)   # 内圈：键盘

    return big.resize((s, s), Image.LANCZOS)


def _fmt(v):
    return "--" if v is None else f"{v}%"


# ---------------------------------------------------------------- 开机自启
def _startup_dir():
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _lnk_path():
    return _startup_dir() / LNK_NAME


def autostart_enabled():
    return _lnk_path().exists()


def set_autostart(enable):
    lnk = _lnk_path()
    try:
        if enable:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            target = pyw if pyw.exists() else Path(sys.executable)
            script = Path(__file__).resolve()
            ps = (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
                "$s.TargetPath='{t}';$s.Arguments='\"{sc}\"';"
                "$s.WorkingDirectory='{wd}';$s.Save()"
            ).format(lnk=lnk, t=target, sc=script, wd=script.parent)
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                creationflags=0x08000000, check=False, timeout=20,
            )
        else:
            if lnk.exists():
                lnk.unlink()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 单实例
def already_running():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    handle = k32.CreateMutexW(None, False, "Global\\MCHOSE_BATTERY_TRAY")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return True, handle
    return False, handle


# ---------------------------------------------------------------- 主程序
class TrayApp:
    def __init__(self):
        self.mouse = None
        self.keyboard = None
        self.running = True
        self.lock = threading.Lock()
        self.notified = {"mouse": False, "keyboard": False}
        self.icon = pystray.Icon(
            "mchose_battery",
            make_icon(None, None),
            f"{APP_NAME}\n正在读取…",
            menu=pystray.Menu(
                pystray.MenuItem("立即刷新", self.on_refresh, default=True),
                pystray.MenuItem("开机自启", self.on_toggle_autostart, checked=lambda i: autostart_enabled()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self.on_quit),
            ),
        )

    # --- 低电量提醒（每次掉到阈值以下只提醒一次，回充后重置）---
    def _check_low(self, name, val, key):
        if val is None:
            return
        if val <= LOW_BATTERY:
            if not self.notified.get(key):
                self.notified[key] = True
                try:
                    self.icon.notify(f"{name} 电量仅剩 {val}%", APP_NAME)
                except Exception:
                    pass
        else:
            self.notified[key] = False

    # --- 读取 ---
    def refresh(self):
        m = read_a7()
        k = read_g87()
        with self.lock:
            if m is not None:
                self.mouse = m
            if k is not None:
                self.keyboard = k
            mouse, kb = self.mouse, self.keyboard
        try:
            self.icon.icon = make_icon(mouse, kb)
            self.icon.title = (
                f"鼠标 A7   {_fmt(mouse)}\n"
                f"键盘 G87  {_fmt(kb)}"
            )
        except Exception:
            pass
        self._check_low("鼠标 A7", mouse, "mouse")
        self._check_low("键盘 G87", kb, "keyboard")

    def loop(self):
        while self.running:
            self.refresh()
            for _ in range(REFRESH_SEC):
                if not self.running:
                    return
                time.sleep(1)

    # --- 菜单回调 ---
    def on_refresh(self, icon=None, item=None):
        threading.Thread(target=self.refresh, daemon=True).start()

    def on_toggle_autostart(self, icon=None, item=None):
        set_autostart(not autostart_enabled())
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def on_quit(self, icon=None, item=None):
        self.running = False
        try:
            self.icon.stop()
        except Exception:
            pass

    def run(self):
        self.icon.run(setup=self._setup)

    def _setup(self, icon):
        icon.visible = True
        threading.Thread(target=self.loop, daemon=True).start()


def main():
    dup, _handle = already_running()
    if dup:
        return
    TrayApp().run()


if __name__ == "__main__":
    main()
