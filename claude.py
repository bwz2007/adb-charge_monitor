# -*- coding: utf-8 -*-
"""
手机充电功率监测工具（ADB 无线调试 / Wi-Fi）

功能
  1. 通过 Wi-Fi 无线调试连接手机（支持配对码配对、连接、mDNS 自动发现），支持电脑上同时存在多个 adb 设备时选择目标设备
  2. 定时读取电压、电流、电量、温度，实时绘制充电功率曲线
  3. 统计 平均 / 最高 / 最低功率、累计充入能量、有效时长；支持 暂停 / 继续 / 停止；
     可设置采样间隔、总时长、延迟开始、充满自动停止
  4. 独立“保活进程”每 5 秒与手机通信一次，防止息屏后无线调试断开（暂停/停止检测期间依然保活）
  5. 数据导出为 CSV / JSON，曲线保存为 PNG / PDF / SVG

运行环境: Python 3.8+（Windows），依赖: pip install matplotlib
运行方式: python charge_monitor.py
"""
import csv
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------
APP_NAME = "手机充电功率监测"
DEFAULT_ADB = r"C:\install\adb\adb.exe"
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".adb_charge_monitor.json")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 不弹出黑色 cmd 窗口
KEEPALIVE_INTERVAL = 5.0                                         # 保活间隔（秒）
STATUS_NAMES = {1: "未知", 2: "充电中", 3: "放电中", 4: "未充电", 5: "已充满"}

# 一次 adb 调用同时拿到：sysfs 实时电流/电压 + dumpsys battery 全部信息
# 注意：脚本内不能出现双引号（避免 Windows 命令行转义问题）
SNAPSHOT_CMD = (
    "for d in battery Battery bms; do "
    "P=/sys/class/power_supply/$d; "
    "I=$(cat $P/current_now 2>/dev/null); "
    "if [ ${#I} -gt 0 ]; then echo I=$I; echo V=$(cat $P/voltage_now 2>/dev/null); break; fi; "
    "done; dumpsys battery"
)

# 图表“附加曲线”选项: 显示名 -> (Sample 属性名, 坐标轴名称)
EXTRAS = {
    "无": None,
    "电流 (mA)": ("current_ma", "电流 (mA)"),
    "电压 (V)": ("voltage_v", "电压 (V)"),
    "电量 (%)": ("level", "电量 (%)"),
    "温度 (℃)": ("temp_c", "温度 (℃)"),
}
UNIT_CHOICES = {"自动识别": "auto", "µA（微安）": "uA", "mA（毫安）": "mA"}
DIR_CHOICES = {"自动（取绝对值）": "abs", "正数表示充电": "pos", "负数表示充电": "neg"}

np = mdates = Figure = FigureCanvasTkAgg = NavigationToolbar2Tk = None   # 延迟加载 matplotlib


def load_matplotlib():
    """延迟导入 matplotlib（保活子进程不需要它，启动更快）"""
    global np, mdates, Figure, FigureCanvasTkAgg, NavigationToolbar2Tk
    import numpy as _np
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.dates as _md
    from matplotlib.figure import Figure as _Fig
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg as _Canvas, NavigationToolbar2Tk as _Tb
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    np, mdates, Figure, FigureCanvasTkAgg, NavigationToolbar2Tk = _np, _md, _Fig, _Canvas, _Tb


# --------------------------------------------------------------------------------------
# 设置保存
# --------------------------------------------------------------------------------------
def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# ADB 封装
# --------------------------------------------------------------------------------------
class AdbError(Exception):
    pass


def run_cmd(cmd: List[str], timeout: float = 10) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise AdbError("命令超时（%ss）: %s" % (int(timeout), " ".join(cmd[1:4])))
    except FileNotFoundError:
        raise AdbError("找不到可执行文件: %s" % cmd[0])
    except OSError as e:
        raise AdbError(str(e))
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def parse_devices(text: str) -> List[Dict[str, str]]:
    """解析 `adb devices -l`"""
    devs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        rest = parts[2:]
        if state == "no" and rest[:1] == ["permissions"]:
            state = "no permissions"
        kv = {}
        for p in rest:
            if ":" in p:
                k, v = p.split(":", 1)
                kv[k] = v
        if "usb" in kv:
            conn = "USB"
        elif serial.startswith("emulator-"):
            conn = "模拟器"
        elif ":" in serial or "_adb-tls" in serial:
            conn = "Wi-Fi"
        else:
            conn = "其他"
        devs.append({"serial": serial, "state": state,
                     "model": kv.get("model", "").replace("_", " "), "conn": conn})
    return devs


def parse_mdns(text: str) -> Dict[str, List[str]]:
    """解析 `adb mdns services`，返回 {'connect': ['ip:port'], 'pair': ['ip:port']}"""
    res = {"connect": [], "pair": []}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        if "_adb-tls-connect" in parts[1]:
            res["connect"].append(parts[2])
        elif "_adb-tls-pairing" in parts[1]:
            res["pair"].append(parts[2])
    return res


def _to_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except Exception:
        return None


def norm_voltage(raw: Optional[int]) -> Optional[float]:
    """不同手机电压单位不同（µV / mV / V），统一换算为 V"""
    if raw is None or raw <= 0:
        return None
    if raw >= 100000:
        return raw / 1e6
    if raw >= 100:
        return raw / 1e3
    return float(raw)


@dataclass
class Snapshot:
    level: Optional[int] = None
    status: int = 1
    voltage_v: Optional[float] = None
    temp_c: Optional[float] = None
    ac: bool = False
    usb: bool = False
    wireless: bool = False
    dock: bool = False
    max_ma: Optional[float] = None       # 充电器协商的最大电流
    max_v: Optional[float] = None        # 充电器协商的最大电压
    charge_counter: Optional[int] = None  # µAh
    sys_i: Optional[int] = None          # sysfs current_now 原始值
    sys_v: Optional[int] = None          # sysfs voltage_now 原始值


def parse_snapshot(text: str) -> Snapshot:
    kv: Dict[str, str] = {}
    sys_i = sys_v = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^([IV])=(-?\d*)$", s)
        if m:
            val = _to_int(m.group(2))
            if m.group(1) == "I":
                sys_i = val
            else:
                sys_v = val
            continue
        if ":" in s:
            k, v = s.split(":", 1)
            kv[k.strip().lower()] = v.strip()
    level = _to_int(kv.get("level"))
    if level is None and _to_int(kv.get("voltage")) is None:
        raise AdbError("未读取到电池信息（设备可能离线或未授权）")
    snap = Snapshot()
    snap.level = level
    snap.status = _to_int(kv.get("status")) or 1
    snap.voltage_v = norm_voltage(sys_v) or norm_voltage(_to_int(kv.get("voltage")))
    t = _to_int(kv.get("temperature"))
    snap.temp_c = t / 10.0 if t is not None else None
    truthy = lambda k: "true" in kv.get(k, "").lower()
    snap.ac, snap.usb = truthy("ac powered"), truthy("usb powered")
    snap.wireless, snap.dock = truthy("wireless powered"), truthy("dock powered")
    mc = _to_int(kv.get("max charging current"))
    if mc:
        snap.max_ma = mc / 1000.0 if mc >= 20000 else float(mc)
    snap.max_v = norm_voltage(_to_int(kv.get("max charging voltage")))
    snap.charge_counter = _to_int(kv.get("charge counter"))
    snap.sys_i, snap.sys_v = sys_i, sys_v
    return snap


class Adb:
    def __init__(self, path: str):
        self.path = path

    def run(self, args: List[str], serial: Optional[str] = None, timeout: float = 10):
        cmd = [self.path]
        if serial:
            cmd += ["-s", serial]
        cmd += list(args)
        return run_cmd(cmd, timeout)

    def devices(self) -> List[Dict[str, str]]:
        rc, out, err = self.run(["devices", "-l"], timeout=20)
        if rc != 0 and not out.strip():
            raise AdbError(err.strip() or "adb devices 执行失败")
        return parse_devices(out)

    def device_props(self, serial: str) -> str:
        rc, out, err = self.run(
            ["shell", "getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release"],
            serial=serial, timeout=10)
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        if len(lines) < 2:
            raise AdbError((err or out).strip() or "无法与设备通信")
        ver = lines[2] if len(lines) > 2 else "?"
        return "%s %s (Android %s)" % (lines[0], lines[1], ver)

    def snapshot(self, serial: str) -> Snapshot:
        rc, out, err = self.run(["shell", SNAPSHOT_CMD], serial=serial, timeout=12)
        try:
            return parse_snapshot(out)
        except AdbError:
            msg = (err or out).strip().splitlines()
            raise AdbError(msg[0] if msg else "设备无响应")


# --------------------------------------------------------------------------------------
# 保活：独立子进程，每 5 秒与手机通信一次
# --------------------------------------------------------------------------------------
def keepalive_once(adb_path: str, serial: str) -> Tuple[bool, str]:
    try:
        # 网络设备：先 connect（已连接时是无害的，断线时会自动重连）
        if re.match(r"^\S+:\d+$", serial) and "_adb-tls" not in serial:
            rc, out, err = run_cmd([adb_path, "connect", serial], 6)
            text = (out + err).strip()
            if "connected to" not in text.lower():
                return False, text[:100] or "connect 失败"
        # 再发一条极轻量的 shell 命令，真正产生一次通信，避免手机休眠后断开
        rc, out, err = run_cmd([adb_path, "-s", serial, "shell", "echo", "ka"], 6)
        if out.strip() == "ka":
            return True, "ok"
        return False, ((err or out).strip() or "设备无响应")[:100]
    except AdbError as e:
        return False, str(e)[:100]


def keepalive_worker(adb_path: str, serial: str, interval: float) -> None:
    """保活子进程入口: python charge_monitor.py --keepalive-worker <adb> <serial> <interval>"""
    stop = threading.Event()

    def watch_stdin():
        try:
            for line in sys.stdin:           # 主程序关闭管道(或意外退出)时也会结束，避免产生孤儿进程
                if line.strip().lower() == "quit":
                    break
        except Exception:
            pass
        stop.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    fails = 0
    while not stop.is_set():
        t0 = time.time()
        ok, msg = keepalive_once(adb_path, serial)
        fails = 0 if ok else fails + 1
        try:
            sys.stdout.write(json.dumps({"ok": ok, "msg": msg, "fails": fails, "ts": time.time()}) + "\n")
            sys.stdout.flush()
        except Exception:
            break
        stop.wait(max(0.5, interval - (time.time() - t0)))


def worker_cmd(adb_path: str, serial: str, interval: float) -> List[str]:
    if getattr(sys, "frozen", False):                       # PyInstaller 打包后
        base = [sys.executable]
    else:
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):             # pythonw 没有标准输入输出，改用 python.exe（不会弹窗）
            cand = exe[:-len("pythonw.exe")] + "python.exe"
            if os.path.exists(cand):
                exe = cand
        base = [exe, os.path.abspath(__file__)]
    return base + ["--keepalive-worker", adb_path, serial, str(interval)]


class KeepAlive:
    def __init__(self, adb_path: str, serial: str, interval: float, callback: Callable[[dict], None]):
        self.cb = callback
        self.closing = False
        self.proc = subprocess.Popen(
            worker_cmd(adb_path, serial, interval),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, universal_newlines=True, encoding="utf-8", bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not self.closing:
                    self.cb(msg)
        except Exception:
            pass
        if not self.closing:
            self.cb({"ok": False, "msg": "保活进程已退出", "fails": -1, "exited": True})

    def stop(self):
        self.closing = True
        p = self.proc
        try:
            p.stdin.write("quit\n")
            p.stdin.flush()
            p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=1.5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------------------
# 数据模型 / 统计
# --------------------------------------------------------------------------------------
@dataclass
class Sample:
    ts: float                 # 时间戳（秒）
    seg: int                  # 分段号：每次“暂停→继续”后 +1，用于让曲线在暂停处断开
    level: Optional[int]
    voltage_v: float
    current_ma: float
    power_w: float
    temp_c: Optional[float]
    status: int
    source: str
    charging: bool
    method: str
    max_ma: Optional[float] = None
    max_v: Optional[float] = None


CSV_HEADERS = ["时间", "相对时间(s)", "电量(%)", "电压(V)", "电流(mA)", "功率(W)", "温度(℃)", "状态", "供电方式",
               "充电器协商最大电流(mA)", "充电器协商最大电压(V)", "数据来源"]


def sample_row(s: Sample, t0: float) -> list:
    return [datetime.fromtimestamp(s.ts).strftime("%Y-%m-%d %H:%M:%S"), round(s.ts - t0, 1),
            "" if s.level is None else s.level, round(s.voltage_v, 3), round(s.current_ma, 1),
            round(s.power_w, 3), "" if s.temp_c is None else round(s.temp_c, 1),
            STATUS_NAMES.get(s.status, "未知"), s.source,
            "" if s.max_ma is None else round(s.max_ma), "" if s.max_v is None else round(s.max_v, 2), s.method]


def compute_stats(samples: List[Sample], only_charging: bool = True) -> Optional[dict]:
    use = [s for s in samples if s.charging] if only_charging else list(samples)
    if not use:
        return None
    mx = max(use, key=lambda s: s.power_w)
    mn = min(use, key=lambda s: s.power_w)
    avg = sum(s.power_w for s in use) / len(use)
    energy = 0.0                                  # 梯形积分，单位 Wh
    for a, b in zip(samples, samples[1:]):
        if a.seg != b.seg:
            continue
        if only_charging and not (a.charging and b.charging):
            continue
        dt = b.ts - a.ts
        if dt > 0:
            energy += (a.power_w + b.power_w) / 2.0 * dt / 3600.0
    dur, first, prev = 0.0, None, None            # 有效时长：各分段时长之和（不含暂停）
    for s in samples:
        if first is None or s.seg != prev.seg:
            if first is not None:
                dur += prev.ts - first.ts
            first = s
        prev = s
    if first is not None:
        dur += prev.ts - first.ts
    return {"count": len(use), "avg": avg, "max": mx.power_w, "max_ts": mx.ts, "min": mn.power_w,
            "min_ts": mn.ts, "energy_wh": energy, "duration_s": dur}


def fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


# --------------------------------------------------------------------------------------
# 采样线程
# --------------------------------------------------------------------------------------
class CounterEstimator:
    """没有 current_now 的手机：用 dumpsys 的 Charge counter(µAh) 在一个时间窗口内的变化量估算电流"""

    def __init__(self, window: float = 30.0):
        self.window = window
        self.h = deque()

    def reset(self):
        self.h.clear()

    def update(self, t: float, counter_uah: int) -> Optional[float]:
        self.h.append((t, counter_uah))
        while len(self.h) >= 2 and t - self.h[1][0] >= self.window:
            self.h.popleft()
        t0, c0 = self.h[0]
        if t - t0 < self.window * 0.8:
            return None
        return (counter_uah - c0) / 1000.0 / ((t - t0) / 3600.0)


class Sampler(threading.Thread):
    def __init__(self, adb: Adb, serial: str, out_q: "queue.Queue", sid: int, interval: float,
                 duration_s: float, delay_s: float, stop_when_full: bool, unit_mode: str, direction: str):
        super().__init__(daemon=True)
        self.adb, self.serial, self.out_q, self.sid = adb, serial, out_q, sid
        self.interval = max(1.0, interval)
        self.duration_s, self.delay_s = duration_s, delay_s
        self.stop_when_full = stop_when_full
        self.unit_mode, self.direction = unit_mode, direction
        self.run_ev = threading.Event()      # set=运行, clear=暂停
        self.run_ev.set()
        self.stop_ev = threading.Event()
        self.unit = None                     # 自动识别后锁定: 'uA' / 'mA'
        self.est = CounterEstimator()
        self.seg = 0
        self._warned = set()

    # ---- 控制接口（UI 线程调用）----
    def pause(self):
        self.run_ev.clear()

    def resume(self):
        self.run_ev.set()

    def stop(self):
        self.stop_ev.set()
        self.run_ev.set()

    # ---- 内部 ----
    def emit(self, kind: str, payload=None):
        self.out_q.put((self.sid, kind, payload))

    def warn_once(self, key: str, text: str):
        if key not in self._warned:
            self._warned.add(key)
            self.emit("log", text)

    def run(self):
        try:
            reason = self._loop()
        except Exception as e:
            reason = "检测线程异常: %r" % (e,)
        self.emit("done", reason)

    def _loop(self) -> str:
        if self.delay_s > 0:
            self.emit("state", "delay")
            end = time.monotonic() + self.delay_s
            while not self.stop_ev.is_set():
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.emit("status", "延迟开始，剩余 %d 秒" % int(left + 0.99))
                self.stop_ev.wait(min(1.0, left))
            if self.stop_ev.is_set():
                return "已在延迟阶段停止"
        self.emit("state", "running")
        active, last_tick, started = 0.0, None, False
        next_t = time.monotonic()
        fails = full_cnt = 0
        while not self.stop_ev.is_set():
            now = time.monotonic()
            if not self.run_ev.is_set():              # 暂停：不向手机发送任何命令
                last_tick = None
                self.run_ev.wait(0.2)
                continue
            if last_tick is None:                     # 首次开始 / 从暂停恢复
                if started:
                    self.seg += 1
                    self.est.reset()
                    next_t = now
                started, last_tick = True, now
            active += now - last_tick
            last_tick = now
            if self.duration_s and active >= self.duration_s:
                return "已达到设定的检测时长，自动停止"
            if now < next_t:
                self.stop_ev.wait(min(0.2, next_t - now))
                continue

            try:
                snap = self.adb.snapshot(self.serial)
                fails = 0
            except AdbError as e:
                fails += 1
                if fails <= 3 or fails % 10 == 0:
                    self.emit("log", "读取失败（连续 %d 次）: %s" % (fails, e))
                if fails >= 3:
                    self.emit("status", "与手机通信失败，正在重试…（保活进程会尝试重连）")
                next_t = max(next_t + self.interval, time.monotonic() + 0.5)
                continue

            sample = self._make_sample(snap)
            if sample is not None:
                self.emit("sample", sample)
                self.emit("status", "检测中 · 间隔 %g 秒" % self.interval)
            next_t = max(next_t + self.interval, time.monotonic() + 0.2)

            if self.stop_when_full:
                full_cnt = full_cnt + 1 if snap.status == 5 else 0
                if full_cnt >= 3:
                    return "电池已充满，自动停止"
        return "已停止"

    def _to_ma(self, raw: int) -> float:
        if self.unit_mode == "uA":
            ma = raw / 1000.0
        elif self.unit_mode == "mA":
            ma = float(raw)
        else:
            if self.unit is None:
                a = abs(raw)
                if a >= 10000:
                    self.unit = "uA"
                elif a >= 200:
                    self.unit = "mA"
                if self.unit:
                    self.emit("log", "电流单位自动识别为 %s（原始值 %d）" % ("µA" if self.unit == "uA" else "mA", raw))
            ma = float(raw) if self.unit == "mA" else raw / 1000.0
        if self.direction == "abs":
            ma = abs(ma)
        elif self.direction == "pos":
            ma = max(ma, 0.0)
        elif self.direction == "neg":
            ma = max(-ma, 0.0)
        return ma

    def _make_sample(self, snap: Snapshot) -> Optional[Sample]:
        charging = snap.status == 2
        method, ma = "sysfs", None
        if snap.sys_i is not None:
            ma = self._to_ma(snap.sys_i)
            self.warn_once("src", "电流来源: /sys/class/power_supply 实时读数")
        elif snap.charge_counter is not None:
            self.warn_once("src", "该手机不允许读取 current_now，改用 Charge counter 变化量估算电流（需约 30 秒出第一个点，精度较低）")
            est = self.est.update(time.monotonic(), snap.charge_counter)
            if est is None:
                self.emit("status", "正在估算电流，请稍候…")
                return None
            ma, method = abs(est), "charge_counter估算"
        else:
            self.warn_once("nocur", "该手机既无法读取 current_now，也没有 Charge counter，无法计算功率")
            return None
        if snap.voltage_v is None:
            self.warn_once("nov", "未读取到电压")
            return None
        src = [n for n, f in (("交流(AC)", snap.ac), ("USB", snap.usb), ("无线", snap.wireless), ("底座", snap.dock)) if f]
        return Sample(
            ts=time.time(), seg=self.seg, level=snap.level, voltage_v=snap.voltage_v,
            current_ma=ma if charging else 0.0,
            power_w=snap.voltage_v * ma / 1000.0 if charging else 0.0,
            temp_c=snap.temp_c, status=snap.status, source="+".join(src) or "无",
            charging=charging, method=method, max_ma=snap.max_ma, max_v=snap.max_v)


# --------------------------------------------------------------------------------------
# 无线调试对话框
# --------------------------------------------------------------------------------------
class WirelessDialog(tk.Toplevel):
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("无线调试：配对 / 连接")
        self.transient(app)
        self.resizable(False, False)
        c = app.cfg
        self.var_ip = tk.StringVar(value=c.get("last_ip", ""))
        self.var_pair_port = tk.StringVar(value=c.get("pair_port", ""))
        self.var_code = tk.StringVar()
        self.var_conn_port = tk.StringVar(value=c.get("conn_port", ""))

        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        tip = ("手机：设置 → 开发者选项 → 无线调试。\n"
               "• 无线调试主界面显示的「IP 地址和端口」= 连接端口\n"
               "• 点「使用配对码配对设备」弹窗里显示的 IP:端口 和 6 位配对码 = 配对端口 / 配对码（与连接端口不同！）\n"
               "• 配对只需做一次；同一台电脑已配对过，直接填 IP 和连接端口点「连接」即可。")
        ttk.Label(f, text=tip, justify="left", wraplength=520, foreground="#444").grid(row=0, column=0, columnspan=6, sticky="w")

        ttk.Label(f, text="手机 IP:").grid(row=1, column=0, sticky="e", pady=6)
        ttk.Entry(f, textvariable=self.var_ip, width=18).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Button(f, text="自动发现 (mDNS)", command=self.do_discover).grid(row=1, column=2, columnspan=2, sticky="w", padx=6)

        ttk.Label(f, text="配对端口:").grid(row=2, column=0, sticky="e")
        ttk.Entry(f, textvariable=self.var_pair_port, width=8).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(f, text="配对码:").grid(row=2, column=2, sticky="e")
        ttk.Entry(f, textvariable=self.var_code, width=10).grid(row=2, column=3, sticky="w", padx=4)
        ttk.Button(f, text="配对", command=lambda: self.do_pair(False)).grid(row=2, column=4, padx=4)

        ttk.Label(f, text="连接端口:").grid(row=3, column=0, sticky="e", pady=6)
        ttk.Entry(f, textvariable=self.var_conn_port, width=8).grid(row=3, column=1, sticky="w", padx=4)
        ttk.Button(f, text="连接", command=self.do_connect).grid(row=3, column=4, padx=4)
        ttk.Button(f, text="配对并连接", command=lambda: self.do_pair(True)).grid(row=3, column=3, padx=4)

        self.out = tk.Text(f, width=72, height=11, state="disabled", wrap="word")
        self.out.grid(row=4, column=0, columnspan=6, pady=(6, 0))
        ttk.Button(f, text="关闭", command=self.destroy).grid(row=5, column=5, sticky="e", pady=(8, 0))

    def write(self, text: str):
        self.out.config(state="normal")
        self.out.insert("end", text.rstrip() + "\n")
        self.out.see("end")
        self.out.config(state="disabled")

    def _ip(self) -> str:
        return self.var_ip.get().strip()

    def do_discover(self):
        adb = self.app._adb()
        if not adb:
            return
        self.write("正在通过 mDNS 搜索手机…（需手机与电脑在同一网络，且无线调试已开启）")

        def work():
            rc, out, err = adb.run(["mdns", "services"], timeout=12)
            return (out + err)

        def done(res, err):
            if err:
                self.write("搜索失败: %s" % err)
                return
            self.write(res.strip() or "(无输出)")
            found = parse_mdns(res)
            if found["connect"]:
                ip, port = found["connect"][0].rsplit(":", 1)
                self.var_ip.set(ip)
                self.var_conn_port.set(port)
                self.write("→ 已填入连接端口: %s:%s" % (ip, port))
            if found["pair"]:
                ip, port = found["pair"][0].rsplit(":", 1)
                self.var_ip.set(ip)
                self.var_pair_port.set(port)
                self.write("→ 已填入配对端口: %s:%s（请再输入手机上显示的配对码）" % (ip, port))
            if not found["connect"] and not found["pair"]:
                self.write("未发现设备，请手动填写 IP 和端口。")

        self.app.run_bg(work, done)

    def do_pair(self, then_connect: bool):
        adb = self.app._adb()
        ip, port, code = self._ip(), self.var_pair_port.get().strip(), self.var_code.get().strip()
        if not adb:
            return
        if not (ip and port.isdigit() and code):
            messagebox.showwarning("信息不完整", "请填写 手机 IP、配对端口、配对码", parent=self)
            return
        self.write("> adb pair %s:%s ******" % (ip, port))

        def work():
            rc, out, err = adb.run(["pair", "%s:%s" % (ip, port), code], timeout=30)
            return (out + err).strip()

        def done(res, err):
            if err:
                self.write("配对出错: %s" % err)
                return
            self.write(res)
            ok = "successfully paired" in res.lower()
            if ok:
                self.app.cfg.update(last_ip=ip, pair_port=port)
                self.write("✔ 配对成功。")
                if then_connect:
                    self.do_connect()
            else:
                self.write("✘ 配对失败：请确认用的是『配对端口』（不是连接端口）、配对码未过期，且手机弹窗仍在显示。")

        self.app.run_bg(work, done)

    def do_connect(self):
        adb = self.app._adb()
        ip, port = self._ip(), self.var_conn_port.get().strip()
        if not adb:
            return
        if not (ip and port.isdigit()):
            messagebox.showwarning("信息不完整", "请填写 手机 IP 和连接端口", parent=self)
            return
        target = "%s:%s" % (ip, port)
        self.write("> adb connect %s" % target)

        def work():
            rc, out, err = adb.run(["connect", target], timeout=20)
            return (out + err).strip()

        def done(res, err):
            if err:
                self.write("连接出错: %s" % err)
                return
            self.write(res)
            low = res.lower()
            if "connected to" in low and "failed" not in low and "cannot" not in low:
                self.app.cfg.update(last_ip=ip, conn_port=port)
                self.write("✔ 连接成功，已刷新设备列表。")
                self.app._want_select = target
                self.app.refresh_devices()
            else:
                self.write("✘ 连接失败：请确认 ① 用的是『连接端口』 ② 已配对 ③ 手机与电脑在同一网络 ④ 无线调试已开启"
                           "（每次重新开关无线调试，连接端口都会变化）。")

        self.app.run_bg(work, done)


# --------------------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME + "  (ADB / Wi-Fi)")
        self.geometry("1220x880")
        self.minsize(1040, 740)
        self.cfg = load_settings()
        self.ui_q = queue.Queue()        # 后台线程 → UI 线程的回调
        self.data_q = queue.Queue()      # 采样线程 → UI 线程的数据
        self.serial: Optional[str] = None
        self.device_label = ""
        self.sampler: Optional[Sampler] = None
        self.keepalive: Optional[KeepAlive] = None
        self.samples: List[Sample] = []
        self.mode = "idle"               # idle / delay / running / paused / stopped
        self._sid = 0
        self._cur_sid = -1
        self._devs: Dict[str, Dict[str, str]] = {}
        self._want_select: Optional[str] = None
        self._ka_ok: Optional[bool] = None
        self._saved = True
        self._dirty = False
        self._last_draw = 0.0
        self._t0: Optional[float] = None
        self._x0 = 0.0
        self.tz = datetime.now().astimezone().tzinfo

        self._build_ui()
        self._init_chart()
        self._set_state("idle")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self._poll)
        self.after(300, lambda: self.refresh_devices(quiet=True))

    # ------------------------------------------------------------------ 构建界面
    def _build_ui(self):
        style = ttk.Style(self)
        scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        style.configure("Treeview", rowheight=int(22 * scale))
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)

        # ① 设备区
        dev = ttk.LabelFrame(root, text="① ADB 与设备（电脑上有多个 adb 设备时，在列表中选择要检测的那一台）", padding=6)
        dev.pack(fill="x")
        r0 = ttk.Frame(dev)
        r0.pack(fill="x")
        ttk.Label(r0, text="ADB 路径:").pack(side="left")
        self.var_adb = tk.StringVar(value=self.cfg.get("adb_path", DEFAULT_ADB))
        ttk.Entry(r0, textvariable=self.var_adb).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r0, text="浏览…", command=self.browse_adb).pack(side="left")

        r1 = ttk.Frame(dev)
        r1.pack(fill="x", pady=4)
        self.tree = ttk.Treeview(r1, columns=("serial", "state", "model", "conn"), show="headings",
                                 height=4, selectmode="browse")
        for col, text, w in (("serial", "设备序列号 / 地址", 330), ("state", "状态", 120),
                             ("model", "型号", 220), ("conn", "连接方式", 90)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(r1, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.use_selected())

        r2 = ttk.Frame(dev)
        r2.pack(fill="x")
        self.btn_refresh = ttk.Button(r2, text="刷新设备", command=self.refresh_devices)
        self.btn_wireless = ttk.Button(r2, text="无线调试：配对 / 连接…", command=self.open_wireless)
        self.btn_disc = ttk.Button(r2, text="断开选中设备", command=self.disconnect_selected)
        self.btn_use = ttk.Button(r2, text="✔ 使用选中设备（并启动保活）", command=self.use_selected)
        for b in (self.btn_refresh, self.btn_wireless, self.btn_disc, self.btn_use):
            b.pack(side="left", padx=(0, 6))
        self.var_keepalive = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="启用保活进程（每 5 秒）", variable=self.var_keepalive,
                        command=self._on_keepalive_toggle).pack(side="left", padx=8)
        r3 = ttk.Frame(dev)
        r3.pack(fill="x", pady=(4, 0))
        self.var_dev = tk.StringVar(value="当前设备: （未选择）")
        ttk.Label(r3, textvariable=self.var_dev, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.lbl_ka = ttk.Label(r3, text="保活: 未启动", foreground="#888")
        self.lbl_ka.pack(side="right")

        # ② 检测设置
        st = ttk.LabelFrame(root, text="② 检测设置", padding=6)
        st.pack(fill="x", pady=(6, 0))
        a = ttk.Frame(st)
        a.pack(fill="x")
        self.var_interval = tk.StringVar(value=str(self.cfg.get("interval", 2)))
        self.var_duration = tk.StringVar(value=str(self.cfg.get("duration", 0)))
        self.var_delay = tk.StringVar(value=str(self.cfg.get("delay", 0)))
        for text, var, lo, hi in (("采样间隔(秒):", self.var_interval, 1, 3600),
                                  ("总时长(分钟, 0=不限):", self.var_duration, 0, 10000),
                                  ("延迟开始(分钟):", self.var_delay, 0, 1000)):
            ttk.Label(a, text=text).pack(side="left", padx=(0, 3))
            ttk.Spinbox(a, from_=lo, to=hi, increment=1, textvariable=var, width=7).pack(side="left", padx=(0, 14))
        self.var_full = tk.BooleanVar(value=True)
        self.var_only_chg = tk.BooleanVar(value=True)
        ttk.Checkbutton(a, text="充满后自动停止", variable=self.var_full).pack(side="left", padx=6)
        ttk.Checkbutton(a, text="统计时忽略未充电的采样点", variable=self.var_only_chg,
                        command=self._mark_dirty).pack(side="left", padx=6)
        b = ttk.Frame(st)
        b.pack(fill="x", pady=(4, 0))
        ttk.Label(b, text="电流单位:").pack(side="left")
        self.var_unit = tk.StringVar(value="自动识别")
        ttk.Combobox(b, textvariable=self.var_unit, values=list(UNIT_CHOICES), state="readonly",
                     width=12).pack(side="left", padx=(3, 14))
        ttk.Label(b, text="电流方向:").pack(side="left")
        self.var_dir = tk.StringVar(value="自动（取绝对值）")
        ttk.Combobox(b, textvariable=self.var_dir, values=list(DIR_CHOICES), state="readonly",
                     width=16).pack(side="left", padx=(3, 14))
        ttk.Label(b, text="（不同手机 current_now 的单位/正负号不一致；数值明显不对时手动指定）",
                  foreground="#888").pack(side="left")

        # 控制按钮
        c = ttk.Frame(root)
        c.pack(fill="x", pady=6)
        self.btn_start = ttk.Button(c, text="▶ 开始检测", command=self.start)
        self.btn_pause = ttk.Button(c, text="⏸ 暂停", command=self.toggle_pause)
        self.btn_stop = ttk.Button(c, text="⏹ 停止", command=self.stop)
        self.btn_clear = ttk.Button(c, text="清空数据", command=self.clear_clicked)
        self.btn_export = ttk.Button(c, text="导出数据 (CSV/JSON)…", command=self.export_data)
        self.btn_png = ttk.Button(c, text="保存曲线图…", command=self.save_chart)
        for btn in (self.btn_start, self.btn_pause, self.btn_stop, self.btn_clear, self.btn_export, self.btn_png):
            btn.pack(side="left", padx=(0, 6))
        self.var_state = tk.StringVar(value="就绪")
        ttk.Label(c, textvariable=self.var_state, font=("Microsoft YaHei UI", 10, "bold"),
                  foreground="#1a5fb4").pack(side="right")

        # 图表选项
        o = ttk.Frame(root)
        o.pack(fill="x")
        ttk.Label(o, text="附加曲线:").pack(side="left")
        self.var_extra = tk.StringVar(value="无")
        cb = ttk.Combobox(o, textvariable=self.var_extra, values=list(EXTRAS), state="readonly", width=10)
        cb.pack(side="left", padx=(3, 14))
        cb.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        ttk.Label(o, text="平滑:").pack(side="left")
        self.var_smooth = tk.StringVar(value="关")
        cb2 = ttk.Combobox(o, textvariable=self.var_smooth, values=["关", "5 点", "10 点", "30 点"],
                           state="readonly", width=7)
        cb2.pack(side="left", padx=(3, 14))
        cb2.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        self.var_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(o, text="视图跟随最新数据（取消勾选后可用工具栏自由缩放/平移）",
                        variable=self.var_follow).pack(side="left")

        # 图表
        self.chart_frame = ttk.Frame(root)
        self.chart_frame.pack(fill="both", expand=True, pady=(2, 0))

        # 统计
        sf = ttk.LabelFrame(root, text="实时数据与统计（功率 = 电池电压 × 充电电流，即电池侧功率，通常略低于充电器标称功率）", padding=4)
        sf.pack(fill="x", pady=(4, 0))
        self.sv = {}
        items = [("cur", "当前功率"), ("avg", "平均功率"), ("max", "最高功率"), ("min", "最低功率"), ("energy", "累计充入能量"),
                 ("level", "电量"), ("vi", "电压 / 电流"), ("temp", "电池温度"), ("stat", "状态 / 供电"), ("dur", "有效时长 / 采样点数")]
        for i, (key, cap) in enumerate(items):
            self.sv[key] = tk.StringVar(value="--")
            cell = ttk.Frame(sf, padding=(8, 1))
            cell.grid(row=i // 5, column=i % 5, sticky="nsew")
            ttk.Label(cell, text=cap, foreground="#666").pack(anchor="w")
            ttk.Label(cell, textvariable=self.sv[key], font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        sf.columnconfigure((0, 1, 2, 3, 4), weight=1)

        # 日志
        lf = ttk.Frame(root)
        lf.pack(fill="x", pady=(4, 0))
        self.txt_log = tk.Text(lf, height=5, state="disabled", wrap="word")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=lsb.set)
        self.txt_log.pack(side="left", fill="x", expand=True)
        lsb.pack(side="left", fill="y")

    def _init_chart(self):
        self.fig = Figure(figsize=(8, 3.6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax2 = self.ax.twinx()
        self.fig.subplots_adjust(left=0.07, right=0.92, top=0.89, bottom=0.21)
        self.ax.set_ylabel("充电功率 (W)")
        self.ax.grid(True, alpha=0.3)
        self.line_raw, = self.ax.plot([], [], color="#1f77b4", lw=1.4, label="充电功率 (W)")
        self.line_smooth, = self.ax.plot([], [], color="#d62728", lw=2.0, label="平滑")
        self.line_avg = self.ax.axhline(0, color="#ff7f0e", ls="--", lw=1.2, label="平均")
        self.line_avg.set_visible(False)
        self.pt_max, = self.ax.plot([], [], "o", color="#d62728", ms=7, label="最高")
        self.pt_min, = self.ax.plot([], [], "o", color="#2ca02c", ms=7, label="最低")
        self.line_extra, = self.ax2.plot([], [], color="#7f7f7f", lw=1.0, label="")
        self.ax2.get_yaxis().set_visible(False)
        self.ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=self.tz))
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=self.tz))
        now = mdates.date2num(datetime.now(timezone.utc))
        self.ax.set_xlim(now, now + 60 / 86400.0)
        self.ax.set_ylim(0, 10)
        self.mpl_canvas = FigureCanvasTkAgg(self.fig, master=self.chart_frame)
        self.toolbar = NavigationToolbar2Tk(self.mpl_canvas, self.chart_frame)
        self.toolbar.update()
        self.mpl_canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

    # ------------------------------------------------------------------ 通用工具
    def log(self, msg: str):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", "[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
        if int(self.txt_log.index("end-1c").split(".")[0]) > 600:
            self.txt_log.delete("1.0", "100.0")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def run_bg(self, fn: Callable, cb: Optional[Callable] = None):
        """在后台线程执行 fn，完成后在 UI 线程调用 cb(result, error)"""
        def work():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, e
            if cb:
                self.ui_q.put((cb, (res, err)))
        threading.Thread(target=work, daemon=True).start()

    def _adb(self, quiet: bool = False) -> Optional[Adb]:
        p = self.var_adb.get().strip().strip('"')
        if not (p and os.path.isfile(p)):
            w = shutil.which("adb")
            if w:
                self.log("配置的 adb 路径不存在，改用系统 PATH 中的: %s" % w)
                self.var_adb.set(w)
                p = w
            else:
                msg = "找不到 adb: %s\n请点击“浏览…”选择 adb.exe" % p
                if quiet:
                    self.log(msg.replace("\n", " "))
                else:
                    messagebox.showerror("找不到 adb", msg)
                return None
        return Adb(p)

    def browse_adb(self):
        p = filedialog.askopenfilename(title="选择 adb.exe", filetypes=[("adb", "adb.exe"), ("所有文件", "*.*")])
        if p:
            self.var_adb.set(p.replace("/", "\\") if os.name == "nt" else p)
            self.refresh_devices()

    def _mark_dirty(self):
        self._dirty = True
        self._last_draw = 0.0

    # ------------------------------------------------------------------ 设备管理
    def refresh_devices(self, quiet: bool = False):
        adb = self._adb(quiet=quiet)
        if not adb:
            return
        self.run_bg(adb.devices, self._on_devices)

    def _on_devices(self, devs, err):
        if err:
            self.log("读取设备列表失败: %s" % err)
            return
        self._devs = {d["serial"]: d for d in devs}
        self.tree.delete(*self.tree.get_children())
        for d in devs:
            self.tree.insert("", "end", iid=d["serial"], values=(d["serial"], d["state"], d["model"], d["conn"]))
        want = self._want_select or self.serial
        if want in self._devs:
            self.tree.selection_set(want)
            self.tree.see(want)
        elif len(devs) == 1:
            self.tree.selection_set(devs[0]["serial"])
        self._want_select = None
        self.log("发现 %d 台设备" % len(devs) if devs else "未发现任何设备：请先通过「无线调试：配对 / 连接」连接手机")

    def open_wireless(self):
        WirelessDialog(self)

    def _selected(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def disconnect_selected(self):
        serial = self._selected()
        if not serial:
            messagebox.showinfo("提示", "请先在列表中选中要断开的设备")
            return
        if self.mode in ("delay", "running", "paused") and serial == self.serial:
            messagebox.showwarning("提示", "正在检测该设备，请先停止检测")
            return
        adb = self._adb()
        if not adb:
            return
        if self._devs.get(serial, {}).get("conn") == "USB":
            messagebox.showinfo("提示", "USB 设备无需断开，拔掉数据线即可")
            return
        if serial == self.serial:
            self._stop_keepalive()
            self.serial = None
            self.var_dev.set("当前设备: （未选择）")
            self._set_state(self.mode)

        def done(res, err):
            self.log("断开: %s" % (err or res))
            self.refresh_devices()
        self.run_bg(lambda: (lambda r: (r[1] + r[2]).strip())(adb.run(["disconnect", serial], timeout=10)), done)

    def use_selected(self):
        serial = self._selected()
        if not serial:
            messagebox.showinfo("提示", "请先在设备列表中选中一台设备")
            return
        if self.mode in ("delay", "running", "paused"):
            messagebox.showwarning("提示", "正在检测中，请先停止检测再切换设备")
            return
        state = self._devs.get(serial, {}).get("state", "")
        if state != "device":
            hint = {"unauthorized": "请在手机上点“允许调试”（勾选始终允许）后刷新。",
                    "offline": "设备离线，请重新连接（断开后重新点“连接”）或重启手机的无线调试。"}.get(state, "请检查连接。")
            messagebox.showwarning("设备不可用", "该设备状态为「%s」。\n%s" % (state, hint))
            return
        adb = self._adb()
        if not adb:
            return
        self.log("正在验证设备 %s …" % serial)

        def done(label, err):
            if err:
                self.log("无法与设备通信: %s" % err)
                messagebox.showerror("连接失败", str(err))
                return
            self.serial, self.device_label = serial, label
            self.var_dev.set("当前设备: %s  [%s]" % (label, serial))
            self.cfg["adb_path"] = adb.path
            self.log("已选择设备: %s [%s]" % (label, serial))
            self._start_keepalive()
            self._set_state(self.mode)
        self.run_bg(lambda: adb.device_props(serial), done)

    # ------------------------------------------------------------------ 保活
    def _start_keepalive(self):
        self._stop_keepalive()
        if not self.serial:
            return
        if not self.var_keepalive.get():
            self.lbl_ka.config(text="保活: 已禁用", foreground="#888")
            return
        adb = self._adb(quiet=True)
        if not adb:
            return
        try:
            self._ka_ok = None
            self.keepalive = KeepAlive(adb.path, self.serial, KEEPALIVE_INTERVAL,
                                       lambda m: self.ui_q.put((self._on_keepalive_msg, (m,))))
            self.lbl_ka.config(text="保活: 启动中…", foreground="#888")
            self.log("保活进程已启动（PID %s），每 %g 秒与手机通信一次" % (self.keepalive.proc.pid, KEEPALIVE_INTERVAL))
        except Exception as e:
            self.keepalive = None
            self.log("启动保活进程失败: %s" % e)

    def _stop_keepalive(self):
        if self.keepalive:
            self.keepalive.stop()
            self.keepalive = None
        self.lbl_ka.config(text="保活: 未启动", foreground="#888")

    def _on_keepalive_toggle(self):
        if self.serial:
            self._start_keepalive()

    def _on_keepalive_msg(self, m: dict):
        if not self.keepalive:
            return
        if m.get("exited"):
            self.lbl_ka.config(text="保活: 进程已退出", foreground="#c0392b")
            self.log("保活进程意外退出")
            return
        ok = bool(m.get("ok"))
        now = datetime.now().strftime("%H:%M:%S")
        if ok:
            self.lbl_ka.config(text="保活: 正常 ✓ (%s)" % now, foreground="#228b22")
        else:
            self.lbl_ka.config(text="保活: 失败 ×%s (%s)" % (m.get("fails"), m.get("msg")), foreground="#c0392b")
        if self._ka_ok is not None and ok != self._ka_ok:
            self.log("保活状态变化: %s" % ("已恢复正常" if ok else "通信失败 - %s" % m.get("msg")))
        self._ka_ok = ok

    # ------------------------------------------------------------------ 检测控制
    def _read_params(self) -> Optional[dict]:
        try:
            interval = float(self.var_interval.get())
            duration = float(self.var_duration.get())
            delay = float(self.var_delay.get())
        except ValueError:
            messagebox.showerror("参数错误", "采样间隔、总时长、延迟开始 必须是数字")
            return None
        if interval < 1:
            interval = 1.0
            self.var_interval.set("1")
            self.log("采样间隔最小为 1 秒，已自动调整")
        return dict(interval=interval, duration_s=max(0.0, duration) * 60, delay_s=max(0.0, delay) * 60,
                    stop_when_full=self.var_full.get(), unit_mode=UNIT_CHOICES.get(self.var_unit.get(), "auto"),
                    direction=DIR_CHOICES.get(self.var_dir.get(), "abs"))

    def start(self):
        if not self.serial:
            messagebox.showinfo("提示", "请先在设备列表中选中设备，并点击「使用选中设备」")
            return
        adb = self._adb()
        if not adb:
            return
        params = self._read_params()
        if not params:
            return
        if self.samples and not messagebox.askyesno(
                "开始新的检测", "当前已有 %d 条数据，开始新检测会将其清空。\n（可先取消并点“导出数据”保存）\n\n是否继续？" % len(self.samples)):
            return
        self._clear_data()
        self._sid += 1
        self._cur_sid = self._sid
        self.sampler = Sampler(adb, self.serial, self.data_q, self._sid, **params)
        self.sampler.start()
        self.ax.set_title("")
        self._set_state("delay" if params["delay_s"] > 0 else "running")
        self.log("开始检测：间隔 %g 秒%s%s" % (
            params["interval"],
            "，总时长 %g 分钟" % (params["duration_s"] / 60) if params["duration_s"] else "，不限时长",
            "，延迟 %g 分钟后开始" % (params["delay_s"] / 60) if params["delay_s"] else ""))

    def toggle_pause(self):
        if not self.sampler:
            return
        if self.mode == "running":
            self.sampler.pause()
            self._set_state("paused")
            self.log("已暂停：暂停期间不会向手机发送任何检测命令（保活进程仍在运行）")
        elif self.mode == "paused":
            self.sampler.resume()
            self._set_state("running")
            self.log("已继续检测")

    def stop(self):
        if self.sampler and self.mode in ("delay", "running", "paused"):
            self.sampler.stop()
            self._set_state("stopped")
            self.log("已停止检测，数据已保留，可导出")

    def clear_clicked(self):
        if self.mode in ("delay", "running", "paused"):
            messagebox.showinfo("提示", "请先停止检测再清空数据")
            return
        if self.samples and not self._saved and not messagebox.askyesno("清空数据", "数据尚未导出，确定清空吗？"):
            return
        self._clear_data()
        self._set_state("idle")

    def _clear_data(self):
        self.samples = []
        self._t0 = None
        self._saved = True
        for v in self.sv.values():
            v.set("--")
        self._clear_chart()

    def _set_state(self, mode: str):
        self.mode = mode
        busy = mode in ("delay", "running", "paused")
        self.btn_start.config(state="disabled" if busy or not self.serial else "normal")
        self.btn_pause.config(state="normal" if mode in ("running", "paused") else "disabled",
                              text="▶ 继续" if mode == "paused" else "⏸ 暂停")
        self.btn_stop.config(state="normal" if busy else "disabled")
        for b in (self.btn_use, self.btn_disc, self.btn_clear):
            b.config(state="disabled" if busy else "normal")
        self.var_state.set({"idle": "就绪", "delay": "等待开始…", "running": "● 检测中",
                            "paused": "⏸ 已暂停（不向手机发送命令）", "stopped": "■ 已停止"}.get(mode, mode))

    # ------------------------------------------------------------------ 事件循环
    def _poll(self):
        try:
            for _ in range(200):
                try:
                    fn, args = self.ui_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn(*args)
                except Exception as e:
                    self.log("界面回调异常: %r" % (e,))
            for _ in range(500):
                try:
                    sid, kind, payload = self.data_q.get_nowait()
                except queue.Empty:
                    break
                if sid != self._cur_sid:
                    continue                       # 旧检测线程遗留的数据，丢弃
                try:
                    self._handle_event(kind, payload)
                except Exception as e:
                    self.log("处理数据异常: %r" % (e,))
            if self._dirty and time.time() - self._last_draw >= 0.5:
                try:
                    self._refresh_chart()
                except Exception as e:
                    self._dirty = False
                    self.log("刷新图表异常: %r" % (e,))
        finally:
            self.after(200, self._poll)

    def _handle_event(self, kind: str, payload):
        if kind == "sample":
            if self._t0 is None:
                self._t0 = payload.ts
                self._x0 = mdates.date2num(datetime.fromtimestamp(payload.ts, timezone.utc))
            self.samples.append(payload)
            self._saved = False
            self._update_live(payload)
            self._dirty = True
        elif kind == "log":
            self.log(payload)
        elif kind == "status":
            if self.mode in ("delay", "running"):
                self.var_state.set(payload if self.mode == "delay" else "● " + payload)
        elif kind == "state":
            if self.mode != "stopped":
                self._set_state(payload)
        elif kind == "done":
            self.log(payload)
            self._set_state("stopped")
            self._mark_dirty()

    def _update_live(self, s: Sample):
        sv = self.sv
        sv["cur"].set("%.2f W" % s.power_w)
        sv["vi"].set("%.3f V / %.0f mA" % (s.voltage_v, s.current_ma))
        sv["temp"].set("%.1f ℃" % s.temp_c if s.temp_c is not None else "--")
        sv["stat"].set("%s / %s%s" % (STATUS_NAMES.get(s.status, "未知"), s.source, "（估算）" if s.method != "sysfs" else ""))
        sv["level"].set("%s%%" % s.level if s.level is not None else "--")

    # ------------------------------------------------------------------ 图表
    def _xnum(self, ts: float) -> float:
        return (ts - self._t0) / 86400.0 + self._x0

    def _clear_chart(self):
        for ln in (self.line_raw, self.line_smooth, self.pt_max, self.pt_min, self.line_extra):
            ln.set_data([], [])
        self.line_avg.set_visible(False)
        leg = self.ax.get_legend()
        if leg:
            leg.remove()
        self.ax.set_title("")
        self.ax2.get_yaxis().set_visible(False)
        self._dirty = False
        self.mpl_canvas.draw_idle()

    @staticmethod
    def _smooth(y, seg, k: int):
        if k <= 1 or len(y) == 0:
            return y
        out = np.empty_like(y)
        bounds = [0] + list(np.where(np.diff(seg) != 0)[0] + 1) + [len(y)]
        for a, b in zip(bounds[:-1], bounds[1:]):
            part = y[a:b]
            cs = np.concatenate(([0.0], np.cumsum(part)))
            i = np.arange(1, len(part) + 1)
            lo = np.maximum(0, i - k)
            out[a:b] = (cs[i] - cs[lo]) / (i - lo)
        return out

    def _xy(self, ts, seg, y):
        """生成绘图数据；在“暂停→继续”的分段处插入 NaN，让曲线断开而不是连一条斜线"""
        x = (ts - self._t0) / 86400.0 + self._x0
        idx = np.where(np.diff(seg) != 0)[0] + 1
        if len(idx):
            x = np.insert(x, idx, x[idx])
            y = np.insert(y, idx, np.nan)
        return x, y

    def _refresh_chart(self):
        self._dirty = False
        self._last_draw = time.time()
        S = self.samples
        if not S:
            self._clear_chart()
            return
        ts = np.array([s.ts for s in S], dtype=float)
        seg = np.array([s.seg for s in S])
        p = np.array([s.power_w for s in S], dtype=float)
        k = 1 if self.var_smooth.get() == "关" else int(re.findall(r"\d+", self.var_smooth.get())[0])

        handles = []
        x, y = self._xy(ts, seg, p)
        self.line_raw.set_data(x, y)
        self.line_raw.set_alpha(0.35 if k > 1 else 1.0)
        handles.append(self.line_raw)
        if k > 1:
            xs, ys = self._xy(ts, seg, self._smooth(p, seg, k))
            self.line_smooth.set_data(xs, ys)
            self.line_smooth.set_label("平滑(%d点)" % k)
            handles.append(self.line_smooth)
        else:
            self.line_smooth.set_data([], [])

        stats = compute_stats(S, self.var_only_chg.get())
        title = self.device_label or (self.serial or "")
        if stats:
            self.line_avg.set_ydata([stats["avg"], stats["avg"]])
            self.line_avg.set_visible(True)
            self.line_avg.set_label("平均 %.2f W" % stats["avg"])
            self.pt_max.set_data([self._xnum(stats["max_ts"])], [stats["max"]])
            self.pt_max.set_label("最高 %.2f W" % stats["max"])
            self.pt_min.set_data([self._xnum(stats["min_ts"])], [stats["min"]])
            self.pt_min.set_label("最低 %.2f W" % stats["min"])
            handles += [self.line_avg, self.pt_max, self.pt_min]
            title += "    平均 %.2f W   最高 %.2f W   最低 %.2f W" % (stats["avg"], stats["max"], stats["min"])
            sv = self.sv
            sv["avg"].set("%.2f W" % stats["avg"])
            sv["max"].set("%.2f W  @%s" % (stats["max"], datetime.fromtimestamp(stats["max_ts"]).strftime("%H:%M:%S")))
            sv["min"].set("%.2f W  @%s" % (stats["min"], datetime.fromtimestamp(stats["min_ts"]).strftime("%H:%M:%S")))
            sv["energy"].set("%.3f Wh" % stats["energy_wh"])
            sv["dur"].set("%s / %d 点" % (fmt_dur(stats["duration_s"]), len(S)))
        else:
            self.line_avg.set_visible(False)
            self.pt_max.set_data([], [])
            self.pt_min.set_data([], [])
            self.sv["dur"].set("-- / %d 点" % len(S))
        l0, l1 = S[0].level, S[-1].level
        if l0 is not None and l1 is not None:
            self.sv["level"].set("%s%% → %s%%" % (l0, l1))
        self.ax.set_title(title, fontsize=10)

        extra = EXTRAS.get(self.var_extra.get())
        if extra:
            attr, label = extra
            ev = np.array([np.nan if getattr(s, attr) is None else float(getattr(s, attr)) for s in S], dtype=float)
            xe, ye = self._xy(ts, seg, ev)
            self.line_extra.set_data(xe, ye)
            self.line_extra.set_label(label)
            self.ax2.set_ylabel(label)
            self.ax2.get_yaxis().set_visible(True)
            self.ax2.relim()
            self.ax2.autoscale_view(scalex=False)
            handles.append(self.line_extra)
        else:
            self.line_extra.set_data([], [])
            self.ax2.get_yaxis().set_visible(False)

        if self.var_follow.get():
            x0, x1 = float(x[0]), float(x[-1])
            if x1 - x0 < 5 / 86400.0:
                x1 = x0 + 5 / 86400.0
            self.ax.set_xlim(x0, x1 + (x1 - x0) * 0.02)
            pmax = float(np.nanmax(y)) if np.isfinite(y).any() else 1.0
            self.ax.set_ylim(0, max(pmax, 1.0) * 1.18)
        self.ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.09),
                       ncol=len(handles), fontsize=9, frameon=False)
        self.mpl_canvas.draw_idle()

    # ------------------------------------------------------------------ 导出
    def _summary(self) -> dict:
        st = compute_stats(self.samples, self.var_only_chg.get()) or {}
        return {"平均功率_W": round(st.get("avg", 0), 3), "最高功率_W": round(st.get("max", 0), 3),
                "最低功率_W": round(st.get("min", 0), 3), "累计充入能量_Wh": round(st.get("energy_wh", 0), 4),
                "有效时长_s": round(st.get("duration_s", 0), 1), "统计采样点数": st.get("count", 0),
                "总采样点数": len(self.samples),
                "起始电量_%": self.samples[0].level if self.samples else None,
                "结束电量_%": self.samples[-1].level if self.samples else None,
                "说明": "功率=电池电压×充电电流（电池侧功率）；统计%s" % ("仅包含充电中的采样点" if self.var_only_chg.get() else "包含所有采样点")}

    def export_data(self):
        if not self.samples:
            messagebox.showinfo("提示", "还没有数据可导出")
            return
        name = re.sub(r"[^\w.\-]+", "_", self.serial or "device")
        path = filedialog.asksaveasfilename(
            title="导出数据", defaultextension=".csv",
            initialfile="charge_%s_%s.csv" % (name, datetime.now().strftime("%Y%m%d_%H%M%S")),
            filetypes=[("CSV 文件 (Excel 可直接打开)", "*.csv"), ("JSON 文件 (含统计汇总)", "*.json")])
        if not path:
            return
        try:
            t0 = self.samples[0].ts
            if path.lower().endswith(".json"):
                data = {"device": self.device_label, "serial": self.serial,
                        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "summary": self._summary(),
                        "samples": [dict(asdict(s), time=datetime.fromtimestamp(s.ts).strftime("%Y-%m-%d %H:%M:%S"))
                                    for s in self.samples]}
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:      # utf-8-sig：Excel 不会乱码
                    w = csv.writer(f)
                    w.writerow(CSV_HEADERS)
                    for s in self.samples:
                        w.writerow(sample_row(s, t0))
            self._saved = True
            self.log("已导出 %d 条数据 → %s" % (len(self.samples), path))
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def save_chart(self):
        if not self.samples:
            messagebox.showinfo("提示", "还没有数据")
            return
        path = filedialog.asksaveasfilename(
            title="保存曲线图", defaultextension=".png",
            initialfile="charge_chart_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S"),
            filetypes=[("PNG 图片", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")])
        if not path:
            return
        try:
            self.fig.savefig(path, dpi=160)
            self.log("曲线图已保存 → %s" % path)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    # ------------------------------------------------------------------ 退出
    def on_close(self):
        if self.mode in ("delay", "running", "paused"):
            if not messagebox.askyesno("退出", "检测仍在进行，确定退出吗？"):
                return
        elif self.samples and not self._saved:
            if not messagebox.askyesno("退出", "当前数据尚未导出，确定退出吗？"):
                return
        if self.sampler:
            self.sampler.stop()
        self._stop_keepalive()
        self.cfg.update(adb_path=self.var_adb.get().strip(), interval=self.var_interval.get(),
                        duration=self.var_duration.get(), delay=self.var_delay.get())
        save_settings(self.cfg)
        self.destroy()


# --------------------------------------------------------------------------------------
def enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    if len(sys.argv) >= 5 and sys.argv[1] == "--keepalive-worker":     # 保活子进程模式
        keepalive_worker(sys.argv[2], sys.argv[3], float(sys.argv[4]))
        return
    enable_dpi_awareness()
    try:
        load_matplotlib()
    except Exception as e:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("缺少依赖", "无法加载 matplotlib:\n%s\n\n请先在命令行执行:\npip install matplotlib" % e)
        return
    App().mainloop()


if __name__ == "__main__":
    main()