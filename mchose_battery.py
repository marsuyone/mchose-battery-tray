# -*- coding: utf-8 -*-
"""
2.4G 无线外设电量托盘监控
------------------------------------------------
支持设备（协议均为逆向/公开资料得到，仅适用于同芯片方案）：

  * 迈从 A7 鼠标      VID 0x5253 / PID 0x1021
      接口 usagePage 0xFF01
      命令 Feature Report 0x11，载荷 0x0600（其余字节 0xFF 填充）
      回包 65 字节，去掉前 2 字节后逐字节 XOR 0xFF，第 11 字节 = 电量%

  * 迈从 G87 键盘     VID 0x41E4 / PID 0x2001
      接口 usagePage 0xFF04
      Feature Report 0x06，8 字节，第 3 字节 = 电量%

  * ATK / VXE 2.4G 鼠标（VID 0x373B / PID 0x1216，Nordic 方案）
      VID 0x373B / PID 0x1216，接口 usagePage 0xFF05
      无 Feature 报告：Output Report 0x08（64 字节）+ 命令 0x72
      请求 08 7D 72 02 00 01 07 01 ...  回包 Input 0x08
      回包 byte1=0x72 且 byte5=0x07 为有效标志，byte7=电量%，byte8~9=电压 mV(小端)

托盘只占一个图标：外圈=鼠标，内圈=键盘，鼠标悬停看百分比。
"""

import os
import sys
import time
import argparse
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

# 设备标识
MCHOSE_A7_VID, MCHOSE_A7_UP = 0x5253, 0xFF01
MCHOSE_G87_VID, MCHOSE_G87_UP = 0x41E4, 0xFF04

# ATK / VXE 系无线鼠标（Nordic 54L15 方案）
ATK_VID, ATK_UP = 0x373B, 0xFF05
ATK_RID = 0x08
ATK_CMD = [0x7D, 0x72, 0x02, 0x00, 0x01, 0x07, 0x01]
ATK_LEN = 64
ATK_LEVEL_IDX = 7
ATK_VOLT_IDX = 8
ATK_RETRY = 5             # 第一次常常只用于唤醒鼠标，多试几次更稳
ATK_RETRY_GAP = 0.15      # 两次尝试之间的间隔（秒）


# ---------------------------------------------------------------- HID 协议
def _find_path(vid, usage_page):
    try:
        devices = hid.enumerate(vid)
    except TypeError:
        devices = hid.enumerate(vid, 0)
    for d in devices:
        if d.get("usage_page") == usage_page:
            return d["path"]
    return None


def _result(level, voltage=None):
    """统一返回 (电量%, 电压mV)；读不到为 (None, None)"""
    if level is None:
        return None, None
    return level, voltage


def read_a7():
    """迈从 A7 鼠标电量，读不到返回 (None, None)"""
    path = _find_path(MCHOSE_A7_VID, MCHOSE_A7_UP)
    if not path:
        return _result(None)
    dev = hid.device()
    try:
        dev.open_path(path)
        # 0x0600 查询：首字节是 reportId，其后为命令字节取反
        dev.send_feature_report(bytes([0x11, 0xF9, 0xFF] + [0xFF] * 17))
        time.sleep(0.12)
        resp = dev.get_feature_report(0x11, 65)
        if len(resp) < 14:
            return _result(None)
        dec = [b ^ 0xFF for b in resp[2:]]
        val = dec[11]
        return _result(val if 0 <= val <= 100 else None)
    except Exception:
        return _result(None)
    finally:
        try:
            dev.close()
        except Exception:
            pass


def read_g87():
    """迈从 G87 键盘电量，读不到返回 (None, None)"""
    path = _find_path(MCHOSE_G87_VID, MCHOSE_G87_UP)
    if not path:
        return _result(None)
    dev = hid.device()
    try:
        dev.open_path(path)
        resp = dev.get_feature_report(0x06, 8)
        if len(resp) < 4:
            return _result(None)
        val = resp[3]
        return _result(val if 0 <= val <= 100 else None)
    except Exception:
        return _result(None)
    finally:
        try:
            dev.close()
        except Exception:
            pass


def read_atk():
    """ATK / VXE 2.4G 鼠标电量（Output 0x08 + 命令 0x72），读不到返回 (None, None)

    回包形如 08 72 00 05 00 07 01 32 41 0f ...
      byte1=0x72 / byte5=0x07  → 有效电量回包
      byte7=0x32               → 50%
      byte8~9=0x0F41           → 3905 mV
    设备休眠时第一帧会丢，所以重试几次。
    """
    path = _find_path(ATK_VID, ATK_UP)
    if not path:
        return _result(None)
    body = bytes(([ATK_RID] + ATK_CMD + [0] * ATK_LEN)[:ATK_LEN])
    dev = hid.device()
    try:
        dev.open_path(path)
        for attempt in range(ATK_RETRY):
            if attempt:
                time.sleep(ATK_RETRY_GAP)
            # 清掉上一次残留的 Input 报告，避免误判成这次的回包
            for _ in range(50):
                if not dev.read(ATK_LEN, 1):
                    break
            dev.write(body)
            deadline = time.monotonic() + 0.7
            while time.monotonic() < deadline:
                resp = dev.read(ATK_LEN, 120)
                if not resp:
                    continue
                if (
                    len(resp) > ATK_LEVEL_IDX
                    and resp[1] == 0x72
                    and resp[5] == 0x07
                ):
                    val = resp[ATK_LEVEL_IDX]
                    if not 0 <= val <= 100:
                        return _result(None)
                    volt = None
                    if len(resp) > ATK_VOLT_IDX + 1:
                        raw = resp[ATK_VOLT_IDX] | (resp[ATK_VOLT_IDX + 1] << 8)
                        if 2500 <= raw <= 5000:
                            volt = raw
                    return _result(val, volt)
        return _result(None)
    except Exception:
        return _result(None)
    finally:
        try:
            dev.close()
        except Exception:
            pass


def read_mouse():
    """鼠标槽位：优先 A7，其次 ATK。返回 (标签, 电量, 电压)"""
    if _find_path(MCHOSE_A7_VID, MCHOSE_A7_UP):
        level, volt = read_a7()
        return "鼠标 A7", level, volt
    if _find_path(ATK_VID, ATK_UP):
        level, volt = read_atk()
        return "鼠标 ATK", level, volt
    return "鼠标", None, None


def read_keyboard():
    """键盘槽位：G87。返回 (标签, 电量, 电压)"""
    if _find_path(MCHOSE_G87_VID, MCHOSE_G87_UP):
        level, volt = read_g87()
        return "键盘 G87", level, volt
    return "键盘", None, None


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


def _fmt_line(label, v, volt=None):
    text = f"{label}  {_fmt(v)}"
    if volt:
        text += f"  ({volt / 1000:.2f}V)"
    return text


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
        self.mouse_volt = None
        self.mouse_label = "鼠标"
        self.keyboard_label = "键盘"
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
        m_label, m, m_volt = read_mouse()
        k_label, k, _ = read_keyboard()
        with self.lock:
            self.mouse_label = m_label
            self.keyboard_label = k_label
            if m is not None:
                self.mouse = m
                self.mouse_volt = m_volt
            if k is not None:
                self.keyboard = k
            mouse, kb = self.mouse, self.keyboard
            volt = self.mouse_volt
            m_label, k_label = self.mouse_label, self.keyboard_label
        try:
            self.icon.icon = make_icon(mouse, kb)
            self.icon.title = (
                f"{_fmt_line(m_label, mouse, volt)}\n"
                f"{_fmt_line(k_label, kb)}"
            )
        except Exception:
            pass
        self._check_low(m_label, mouse, "mouse")
        self._check_low(k_label, kb, "keyboard")

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


# ---------------------------------------------------------------- 自检
def _emit(text):
    """有控制台就打印；打包成无窗口 exe 时用弹窗显示"""
    try:
        if sys.stdout is not None:
            print(text)
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)
    except Exception:
        pass


def probe():
    """自检：不启动托盘，直接列出设备与电量"""
    lines = [f"{APP_NAME} 自检", "-" * 46]
    found = []
    for d in hid.enumerate():
        up = d.get("usage_page") or 0
        if (d["vendor_id"], d["product_id"]) in ((ATK_VID, 0x1216), (MCHOSE_G87_VID, 0x2001), (MCHOSE_A7_VID, 0x1021)):
            if up >= 0xFF00:
                found.append(
                    f"  VID 0x{d['vendor_id']:04X} PID 0x{d['product_id']:04X} "
                    f"usage_page=0x{up:04X} "
                    f"{(d.get('manufacturer_string') or '').strip()} | "
                    f"{(d.get('product_string') or '').strip()}"
                )
    lines.append("已识别的私有接口:")
    lines.extend(found if found else ["  （无）"])

    for name, fn in (("鼠标", read_mouse), ("键盘", read_keyboard)):
        label, level, volt = fn()
        extra = f"  {volt / 1000:.2f}V" if volt else ""
        lines.append(f"{name}: {label}  ->  {_fmt(level)}{extra}")
    lines.append("-" * 46)
    _emit("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--probe", action="store_true", help="只查看设备与电量，不启动托盘")
    args = parser.parse_args()

    if args.probe:
        probe()
        return

    dup, _handle = already_running()
    if dup:
        _emit("已经在运行了，看任务栏右下角的折叠图标（^）。")
        return
    try:
        TrayApp().run()
    except Exception:
        import traceback
        _emit("启动失败：\n\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
