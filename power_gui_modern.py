import os, sys, time, threading, subprocess, argparse, sqlite3, csv, json
from datetime import datetime, timedelta, date
import tkinter.messagebox as mb
from tkinter import filedialog, Toplevel, StringVar
import customtkinter as ctk
import psutil
import winreg

# ====== Optional tray (สามารถตัดออกได้ถ้าไม่ใช้) ======
import pystray
from PIL import Image, ImageDraw

# ====== NVML ======
try:
    import nvidia_smi as pynvml   # alias เป็น pynvml ให้โค้ดเดิมใช้ต่อได้
    pynvml.nvmlInit()
    HAS_NVML = True
except Exception:
    HAS_NVML = False

APP_TITLE = "Real-time Power Monitor — Modern UI (SQLite)"
APP_NAME  = "PowerMonitorAutoStart"

# ====== Paths ======
DATA_DIR  = os.path.expanduser("~/.power_monitor")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH   = os.path.join(DATA_DIR, "power.sqlite3")
STATE_JSON = os.path.join(DATA_DIR, "state.json")  # ใช้จำค่า kWh สะสมของเดือนปัจจุบัน/เวลาเริ่ม
CONFIG_JSON = os.path.join(DATA_DIR, "config.json")

# ====== CONFIG (default) ======
DEFAULT_CONFIG = {
    "unit_price": 8.0,
    "sample_sec": 1.0,
    "cpu_tdp": 45.0,
    "cpu_idle": 8.0,
    "gpu_tdp": 75.0,
    "gpu_idle": 8.0,
    "monitor_w": 10.0,
    "other_w": 20.0,
}

# ====== Prompt templates (copy-to-clipboard) ======
PROMPT_NOTEBOOK = """ช่วยบอกสเปกโน้ตบุ๊กเพื่อคำนวณพลังงาน/ค่าไฟให้หน่อยครับ:
1) ยี่ห้อและรุ่นโน้ตบุ๊ก
2) CPU รุ่นอะไร (เช่น Intel i5-1240P / Ryzen 7 5800H)
3) ใช้การ์ดจอแบบไหน: iGPU หรือ dGPU (ถ้ามีระบุรุ่น เช่น RTX 3050 Laptop)
4) ขนาดจอในตัวกี่นิ้ว (โดยทั่วไปจอในตัว ~8–12W)
5) การใช้งานหลัก (พิมพ์งาน/ดูเว็บ, เล่นเกม, ตัดต่อ) เพื่อประมาณโหลด
"""

PROMPT_DESKTOP = """ช่วยบอกสเปกคอมตั้งโต๊ะเพื่อคำนวณพลังงาน/ค่าไฟให้หน่อยครับ:
1) CPU รุ่นอะไร (เช่น Intel i7-13700K / Ryzen 5 5600X)
2) การ์ดจอรุ่นอะไร (เช่น RTX 3060 / RX 6600)
3) ใช้จอกี่เครื่อง ขนาดกี่นิ้ว และรุ่น (จอทั่วไป ~20–50W/จอ)
4) มีอุปกรณ์เสริมไหม (HDD หลายลูก, พัดลม/ไฟ RGB เยอะ ฯลฯ)
5) การใช้งานหลัก (เล่นเกม, ทำงาน 3D, เขียนโปรแกรม ฯลฯ)
"""
# ---------------- Config globals ----------------

# ค่า runtime (จะถูกตั้งจาก config)
UNIT_PRICE = DEFAULT_CONFIG["unit_price"]
SAMPLE_SEC = DEFAULT_CONFIG["sample_sec"]
CPU_TDP, CPU_IDLE = DEFAULT_CONFIG["cpu_tdp"], DEFAULT_CONFIG["cpu_idle"]
GPU_TDP, GPU_IDLE = DEFAULT_CONFIG["gpu_tdp"], DEFAULT_CONFIG["gpu_idle"]
MONITOR_W, OTHER_W = DEFAULT_CONFIG["monitor_w"], DEFAULT_CONFIG["other_w"]


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    try:
        if os.path.exists(CONFIG_JSON):
            with open(CONFIG_JSON, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k in cfg:
                if k in user:
                    cfg[k] = user[k]
    except Exception as e:
        print("load_config warning:", e)
    return cfg


def save_config(cfg: dict):
    try:
        with open(CONFIG_JSON, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("save_config error:", e)


def apply_config_globals(cfg: dict):
    global UNIT_PRICE, SAMPLE_SEC, CPU_TDP, CPU_IDLE, GPU_TDP, GPU_IDLE, MONITOR_W, OTHER_W
    UNIT_PRICE  = float(cfg.get("unit_price", DEFAULT_CONFIG["unit_price"]))
    SAMPLE_SEC  = float(cfg.get("sample_sec", DEFAULT_CONFIG["sample_sec"]))
    CPU_TDP     = float(cfg.get("cpu_tdp", DEFAULT_CONFIG["cpu_tdp"]))
    CPU_IDLE    = float(cfg.get("cpu_idle", DEFAULT_CONFIG["cpu_idle"]))
    GPU_TDP     = float(cfg.get("gpu_tdp", DEFAULT_CONFIG["gpu_tdp"]))
    GPU_IDLE    = float(cfg.get("gpu_idle", DEFAULT_CONFIG["gpu_idle"]))
    MONITOR_W   = float(cfg.get("monitor_w", DEFAULT_CONFIG["monitor_w"]))
    OTHER_W     = float(cfg.get("other_w", DEFAULT_CONFIG["other_w"]))


# ---------------- Power helpers ----------------
def estimate_cpu_w(util): return CPU_IDLE + (CPU_TDP - CPU_IDLE) * (util/100.0)


def _nvml_power_w():
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
    except Exception:
        return None


def _nvml_util():
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
    except Exception:
        return None


_last_smi = {"t": 0.0, "w": None}

def _nvidiasmi_power_w():
    import time as _time
    now = _time.time()
    if now - _last_smi["t"] < 2.0 and _last_smi["w"] is not None:
        return _last_smi["w"]
    try:
        startupinfo = subprocess.STARTUPINFO(); startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        out = subprocess.check_output(
            ["nvidia-smi","--query-gpu=power.draw","--format=csv,noheader,nounits"],
            text=True, timeout=1.5, startupinfo=startupinfo, creationflags=0x08000000
        )
        v = out.strip().splitlines()[0].strip()
        w = float(v) if v and v.lower()!="n/a" else None
    except Exception:
        w = None
    _last_smi["t"], _last_smi["w"] = now, w
    return w


def estimate_gpu_w():
    if HAS_NVML:
        w = _nvml_power_w()
        if w is not None and w > 3.0: return w
    w2 = _nvidiasmi_power_w()
    if w2 is not None and w2 > 3.0: return w2
    util = _nvml_util() if HAS_NVML else 0.0
    util = 0.0 if util is None else util
    return GPU_IDLE + (GPU_TDP - GPU_IDLE) * (util/100.0)


def integrate_kwh(prev_kwh, watts, dt):
    return prev_kwh + (watts * dt) / 3_600_000.0


# ---------------- SQLite ----------------
def ensure_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    # per-second (เฉพาะวันนี้)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,          -- ISO datetime
        day TEXT NOT NULL,         -- YYYY-MM-DD
        watts REAL NOT NULL,
        kwh REAL NOT NULL,
        cost REAL NOT NULL
    )""")
    # daily summary
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        day TEXT PRIMARY KEY,      -- YYYY-MM-DD
        kwh REAL NOT NULL,
        cost REAL NOT NULL,
        seconds REAL NOT NULL,
        avg_watts REAL NOT NULL,
        max_watts REAL NOT NULL,
        last_watts REAL NOT NULL   -- sample สุดท้ายของวัน
    )""")
    conn.commit()
    return conn


def today_str(dt=None):
    dt = dt or datetime.now()
    return dt.strftime("%Y-%m-%d")


def month_key(dt=None):
    dt = dt or datetime.now()
    return dt.strftime("%Y-%m")


def insert_sample(conn, ts, watts, kwh, cost):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO samples (ts, day, watts, kwh, cost) VALUES (?, ?, ?, ?, ?)",
        (ts.isoformat(), today_str(ts), float(watts), float(kwh), float(cost))
    )
    conn.commit()


def summarize_day(conn, day):
    cur = conn.cursor()
    # ใช้ค่าสะสมต้นวันและปลายวัน
    cur.execute("SELECT MIN(kwh), MAX(kwh), MAX(watts), AVG(watts), COUNT(*) FROM samples WHERE day=?", (day,))
    row = cur.fetchone()
    if not row or row[4] == 0:
        return False
    kwh_min, kwh_max, max_watts, avg_watts, count = row
    kwh_day = max(0.0, (kwh_max or 0.0) - (kwh_min or 0.0))
    # เวลาจริงของวัน
    cur.execute("SELECT MIN(ts), MAX(ts) FROM samples WHERE day=?", (day,))
    tmin, tmax = cur.fetchone()
    seconds = (datetime.fromisoformat(tmax) - datetime.fromisoformat(tmin)).total_seconds() if (tmin and tmax) else count * SAMPLE_SEC
    cost = kwh_day * UNIT_PRICE
    cur.execute("""
        INSERT INTO daily_summary (day, kwh, cost, seconds, avg_watts, max_watts, last_watts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET
            kwh=excluded.kwh, cost=excluded.cost, seconds=excluded.seconds,
            avg_watts=excluded.avg_watts, max_watts=excluded.max_watts, last_watts=excluded.last_watts
    """, (day, float(kwh_day), float(cost), float(seconds), float(avg_watts or 0.0),
          float(max_watts or 0.0),
          float(cur.execute("SELECT watts FROM samples WHERE day=? ORDER BY id DESC LIMIT 1",(day,)).fetchone()[0])))
    conn.commit()
    return True


def delete_samples_of_day(conn, day):
    cur = conn.cursor()
    cur.execute("DELETE FROM samples WHERE day=?", (day,))
    conn.commit()


def available_months(conn):
    """คืน ['YYYY-MM', ...] ที่มีใน daily_summary"""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT substr(day,1,7) FROM daily_summary ORDER BY 1 DESC")
    return [r[0] for r in cur.fetchall()]


def export_month_csv(conn, yyyymm, path):
    cur = conn.cursor()
    cur.execute("SELECT day, kwh, cost, seconds, avg_watts, max_watts, last_watts FROM daily_summary WHERE substr(day,1,7)=? ORDER BY day", (yyyymm,))
    rows = cur.fetchall()
    if not rows:
        raise FileNotFoundError("เดือนที่เลือกยังไม่มีข้อมูลสรุป")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day","kwh","cost","seconds","avg_watts","max_watts","last_watts"])
        w.writerows(rows)
    return path


# ---------------- Autostart (Registry) ----------------
def _pythonw_path():
    py = sys.executable; cand = os.path.join(os.path.dirname(py), "pythonw.exe")
    return cand if os.path.exists(cand) else py

def _autostart_cmd():
    if getattr(sys,"frozen",False): return f'"{sys.executable}" --autostart'
    return f'"{_pythonw_path()}" "{os.path.abspath(sys.argv[0])}" --autostart'

def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r"Software\\Microsoft\\Windows\\CurrentVersion\\Run") as k:
            v,_ = winreg.QueryValueEx(k, APP_NAME); return bool(v.strip())
    except: return False

def enable_autostart():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",0,winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_cmd())

def disable_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",0,winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME); return True
    except: return False


# ---------------- Tray helpers ----------------
def _tray_icon_img(size=64, fg=(57,197,187,255), bg=(12,18,32,255)):
    img=Image.new("RGBA",(size,size),bg); d=ImageDraw.Draw(img)
    d.polygon([(28,10),(38,10),(30,28),(42,28),(22,56),(28,36),(20,36)], fill=fg)
    return img


# ---------------- GUI ----------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class KPI(ctk.CTkFrame):
    def __init__(self, master, title, unit="", big=False):
        super().__init__(master, corner_radius=16)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, text_color="#9aa3af").grid(row=0, column=0, sticky="w", padx=16, pady=(12,0))
        self.value = ctk.CTkLabel(self, text="—", font=("SF Pro Display", 28 if big else 24, "bold"))
        self.value.grid(row=1, column=0, sticky="w", padx=16, pady=(2,14))
        self.unit = unit
    def set(self, v): self.value.configure(text=f"{v}{self.unit}")


class Overlay(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.9)
        self.configure(fg_color="#0b1220")
        pad = ctk.CTkFrame(self, corner_radius=14, fg_color="#0b1220"); pad.pack(padx=6, pady=6)
        self.l1 = ctk.CTkLabel(pad, font=("JetBrains Mono", 18, "bold"))
        self.l2 = ctk.CTkLabel(pad, font=("JetBrains Mono", 16))
        self.l3 = ctk.CTkLabel(pad, font=("JetBrains Mono", 16))
        self.l4 = ctk.CTkLabel(pad, font=("JetBrains Mono", 14), text_color="#9aa3af")
        for w in (self.l1,self.l2,self.l3,self.l4): w.pack(anchor="w", padx=10, pady=(4,0))
        for w in (self, pad, self.l1, self.l2, self.l3, self.l4):
            w.bind("<Button-1>", self._start_move); w.bind("<B1-Motion>", self._on_move)
        self._tick()
    def _start_move(self, e): self._dx, self._dy = e.x, e.y
    def _on_move(self, e): self.geometry(f"+{e.x_root-self._dx}+{e.y_root-self._dy}")
    def _tick(self):
        self.l1.configure(text=f"⚡ {self.app._watts:,.1f} W")
        self.l2.configure(text=f"🔋 {self.app._kwh:.4f} kWh")
        self.l3.configure(text=f"💸 {self.app._cost:.2f} ฿")
        self.l4.configure(text=f"⏱ {self.app._elapsed_str()}")
        self.after(400, self._tick)


class MonthDialog(Toplevel):
    def __init__(self, master, months: list[str]):
        super().__init__(master)
        self.title("Export รายเดือน")
        self.resizable(False, False)
        self.choice = None
        ctk.CTkLabel(self, text="เลือกเดือนที่มีข้อมูล:").pack(padx=16, pady=(16,6))
        self.combo = ctk.CTkComboBox(self, values=months, width=180)
        if months: self.combo.set(months[0])
        self.combo.pack(padx=16, pady=6)
        btns = ctk.CTkFrame(self); btns.pack(padx=16, pady=(10,14), fill="x")
        ctk.CTkButton(btns, text="OK", command=self._ok).pack(side="left", expand=True, padx=(0,6))
        ctk.CTkButton(btns, text="Cancel", command=self._cancel, fg_color="#374151").pack(side="left", expand=True, padx=(6,0))
        self.grab_set(); self.transient(master)
    def _ok(self): self.choice = self.combo.get(); self.destroy()
    def _cancel(self): self.choice = None; self.destroy()


class SettingsDialog(Toplevel):
    FIELDS = [
        ("unit_price", "ราคา/หน่วย (บาทต่อ kWh)"),
        ("sample_sec", "ช่วงวัด (วินาที)"),
        ("cpu_tdp", "CPU TDP (W)"),
        ("cpu_idle", "CPU Idle (W)"),
        ("gpu_tdp", "GPU TDP (W)"),
        ("gpu_idle", "GPU Idle (W)"),
        ("monitor_w", "จอภาพ (W)"),
        ("other_w", "อุปกรณ์อื่น (W)"),
    ]
    def __init__(self, master, cfg: dict):
        super().__init__(master)
        self.title("Settings")
        self.resizable(False, False)
        self.cfg = cfg.copy()
        self._vars = {}
        frame = ctk.CTkFrame(self)
        frame.pack(padx=16, pady=16, fill="both", expand=True)

        for key, label in self.FIELDS:
            row = ctk.CTkFrame(frame)
            row.pack(fill="x", pady=6)
            ctk.CTkLabel(row, text=label, width=220, anchor="w").pack(side="left")
            v = StringVar(value=str(self.cfg.get(key, "")))
            ent = ctk.CTkEntry(row, textvariable=v, width=160)
            ent.pack(side="right")
            self._vars[key] = v

        btns = ctk.CTkFrame(frame); btns.pack(fill="x", pady=(10,0))
        ctk.CTkButton(btns, text="Save", command=self._save).pack(side="left", expand=True, padx=(0,6))
        ctk.CTkButton(btns, text="Cancel", fg_color="#374151", command=self._cancel).pack(side="left", expand=True, padx=(6,0))

        self.grab_set(); self.transient(master)
        self.result = None

    def _save(self):
        try:
            out = {}
            for k in self._vars:
                out[k] = float(self._vars[k].get().strip())
            self.result = out
            self.destroy()
        except ValueError:
            mb.showerror("Settings", "กรุณากรอกเป็นตัวเลข")

    def _cancel(self):
        self.result = None
        self.destroy()


class App(ctk.CTk):
    def __init__(self, autostart_flag=False):
        super().__init__()
        self.title(APP_TITLE); self.geometry("900x560"); self.minsize(860,520)

        # DB
        self.conn = ensure_db()

        # Config
        self.cfg = load_config()
        apply_config_globals(self.cfg)

        # state
        self._running=False; self._t0=None
        self._kwh=0.0; self._cost=0.0; self._watts=0.0; self._gpu_w=0.0
        self._cur_day = today_str()
        self._tray = None
        self._overlay = None

        # resume month/session (เก็บใน json ง่าย ๆ)
        self._resume_state()

        # layout
        self.grid_columnconfigure(1, weight=1); self.grid_rowconfigure(1, weight=1)
        side=ctk.CTkFrame(self,width=260,corner_radius=0); side.grid(row=0,column=0,rowspan=2,sticky="nsew"); side.grid_propagate(False)

        self.btn_start=ctk.CTkButton(side,text="▶ Start",command=self.start)
        self.btn_stop=ctk.CTkButton(side,text="■ Stop",command=self.stop, state="disabled")
        self.btn_reset=ctk.CTkButton(side,text="♻ Reset เดือนนี้",command=self.reset_month, fg_color="#7c3aed")
        self.btn_export=ctk.CTkButton(side,text="📤 Export CSV รายเดือน",command=self.export_month, fg_color="#10b981")
        self.btn_overlay=ctk.CTkButton(side,text="🪟 Overlay (ซ่อนลง tray)",command=self.toggle_overlay)
        self.btn_settings=ctk.CTkButton(side,text="⚙ Settings",command=self.open_settings, fg_color="#2563eb")

        # copy-prompt buttons
        self.btn_copy_nb = ctk.CTkButton(side, text="📋 Copy Prompt: Notebook", command=lambda: self.copy_text(PROMPT_NOTEBOOK), fg_color="#0891b2")
        self.btn_copy_pc = ctk.CTkButton(side, text="📋 Copy Prompt: Desktop", command=lambda: self.copy_text(PROMPT_DESKTOP), fg_color="#0ea5e9")

        for b in (self.btn_start,self.btn_stop,self.btn_reset,self.btn_export,self.btn_overlay,self.btn_settings,self.btn_copy_nb,self.btn_copy_pc):
            b.pack(padx=16,pady=6,fill="x")

        self.autostart_info=ctk.CTkLabel(side,text="",text_color="#9aa3af",wraplength=220,justify="left")
        self.autostart_info.pack(padx=16,pady=(4,4),anchor="w")
        self.autostart_btn=ctk.CTkButton(side,text="",command=self.toggle_autostart)
        self.autostart_btn.pack(padx=16,pady=(0,12),fill="x")
        self._refresh_autostart_ui()

        head=ctk.CTkFrame(self,corner_radius=0); head.grid(row=0,column=1,sticky="nsew")
        ctk.CTkLabel(head,text="Real-time PC Power (SQLite)",font=("SF Pro Display",24,"bold")).pack(padx=20,pady=14,anchor="w")
        self.time_lbl=ctk.CTkLabel(head,text="—",text_color="#9aa3af"); self.time_lbl.pack(padx=20,pady=(0,10),anchor="w")

        content=ctk.CTkFrame(self,corner_radius=0); content.grid(row=1,column=1,sticky="nsew")
        content.grid_columnconfigure((0,1),weight=1); content.grid_rowconfigure((0,1),weight=1)
        self.kpi_watts=KPI(content,"กำลังไฟรวม"," W",True)
        self.kpi_gpu=KPI(content,"GPU Watt"," W")
        self.kpi_kwh=KPI(content,"พลังงานสะสม (เดือนนี้)"," kWh")
        self.kpi_cost=KPI(content,"ค่าไฟสะสม (เดือนนี้)"," ฿")
        self.kpi_watts.grid(row=0,column=0,padx=16,pady=16,sticky="nsew")
        self.kpi_gpu.grid(row=0,column=1,padx=16,pady=16,sticky="nsew")
        self.kpi_kwh.grid(row=1,column=0,padx=16,pady=16,sticky="nsew")
        self.kpi_cost.grid(row=1,column=1,padx=16,pady=16,sticky="nsew")

        # ปุ่ม X → ย่อไป tray
        self.protocol("WM_DELETE_WINDOW", self.minimize_to_tray)

        self._ui_tick()
        if autostart_flag: self.after(500,self.start)

    # ---------- settings ----------
    def open_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        # บันทึก + ใช้ค่าใหม่
        self.cfg.update(dlg.result)
        save_config(self.cfg)
        apply_config_globals(self.cfg)
        mb.showinfo("Settings", "บันทึกและใช้ค่าใหม่เรียบร้อย")

    # ---------- utilities ----------
    def copy_text(self, text: str):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()  # ensure clipboard updated on some platforms
            mb.showinfo("Copied", "คัดลอกข้อความสำหรับถามเรียบร้อยแล้ว")
        except Exception as e:
            mb.showerror("Copy Error", str(e))

        dlg = SettingsDialog(self, self.cfg)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        # บันทึก + ใช้ค่าใหม่
        self.cfg.update(dlg.result)
        save_config(self.cfg)
        apply_config_globals(self.cfg)
        mb.showinfo("Settings", "บันทึกและใช้ค่าใหม่เรียบร้อย")

    # ---------- state persistence ----------
    def _resume_state(self):
        try:
            with open(STATE_JSON,"r",encoding="utf-8") as f:
                s=json.load(f)
            mk = s.get("month_key", month_key())
            if mk == month_key():
                self._kwh = s.get("kwh",0.0); self._cost = s.get("cost",0.0)
                t0 = s.get("t0"); self._t0 = datetime.fromisoformat(t0) if t0 else datetime.now()
            else:
                self._kwh = 0.0; self._cost = 0.0; self._t0 = datetime.now()
        except Exception:
            self._kwh = 0.0; self._cost = 0.0; self._t0 = datetime.now()
        self._save_state()

    def _save_state(self):
        obj={"month_key":month_key(), "kwh":self._kwh, "cost":self._cost, "t0":self._t0.isoformat() if self._t0 else None}
        try:
            with open(STATE_JSON,"w",encoding="utf-8") as f: json.dump(obj,f,indent=2)
        except: pass

    # ---------- month reset ----------
    def reset_month(self):
        if not mb.askyesno("Reset เดือน","เริ่มรอบใหม่เดือนนี้? (ค่าสะสมเดือนจะเป็น 0 แต่ daily_summary ยังอยู่)"):
            return
        self._kwh=0.0; self._cost=0.0; self._t0=datetime.now()
        self._save_state()

    # ---------- export ----------
    def export_month(self):
        months = available_months(self.conn)
        if not months:
            mb.showinfo("Export","ยังไม่มีข้อมูลสรุปรายวันให้ส่งออก")
            return
        dlg = MonthDialog(self, months)
        self.wait_window(dlg)
        choice = dlg.choice
        if not choice: return
        path = filedialog.asksaveasfilename(
            title=f"บันทึกสรุปรายเดือน {choice}",
            defaultextension=".csv",
            initialfile=f"power_{choice}.csv",
            filetypes=[("CSV","*.csv")]
        )
        if not path: return
        try:
            export_month_csv(self.conn, choice, path)
            mb.showinfo("Export สำเร็จ", f"บันทึกไฟล์:\n{path}")
        except Exception as e:
            mb.showerror("Export Error", str(e))

    # ---------- autostart ----------
    def _refresh_autostart_ui(self):
        if is_autostart_enabled():
            self.autostart_btn.configure(text="Disable Autostart on Windows")
            self.autostart_info.configure(text="เปิดอัตโนมัติไว้แล้ว (Registry Run key)")
        else:
            self.autostart_btn.configure(text="Enable Autostart on Windows")
            self.autostart_info.configure(text="กดเพื่อให้โปรแกรมเริ่มอัตโนมัติเมื่อเข้า Windows (ไม่มี terminal)")

    def toggle_autostart(self):
        try:
            if is_autostart_enabled():
                if disable_autostart(): mb.showinfo("Autostart","ปิดการเปิดอัตโนมัติแล้ว")
            else:
                enable_autostart(); mb.showinfo("Autostart","ตั้งค่าเรียบร้อย")
        except Exception as e:
            mb.showerror("Autostart Error", str(e))
        self._refresh_autostart_ui()

    # ---------- tray ----------
    def _create_tray(self):
        if getattr(self,"_tray",None): return
        icon = _tray_icon_img()
        menu = pystray.Menu(
            pystray.MenuItem("Restore", self._tray_restore),
            pystray.MenuItem("Toggle Overlay", self._tray_toggle_overlay),
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray = pystray.Icon("PowerMonitor", icon, "Power Monitor", menu)
    def _tray_run_async(self):
        self._create_tray()
        threading.Thread(target=self._tray.run, daemon=True).start()
    def minimize_to_tray(self):
        try: self.withdraw()
        except: pass
        self._tray_run_async()
    def restore_from_tray(self):
        try: self.deiconify(); self.lift(); self.focus_force()
        except: pass
        
    def _tray_restore(self, *a): self.restore_from_tray()
    def _tray_toggle_overlay(self, *a): self.toggle_overlay()
    def _tray_quit(self, *a):
        try:
            self.stop()
            if self._overlay and self._overlay.winfo_exists():
                self._overlay.destroy()
        except: pass
        self.after(100, self.destroy)

    # ---------- core ----------
    def _elapsed_str(self):
        if not self._t0: return "0:00:00"
        td = datetime.now() - self._t0
        return str(timedelta(seconds=int(td.total_seconds())))

    def start(self):
        if self._running: return
        self._running=True; self._t0=self._t0 or datetime.now()
        self.btn_start.configure(state="disabled"); self.btn_stop.configure(state="normal")
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running=False
        self.btn_start.configure(state="normal"); self.btn_stop.configure(state="disabled")
        self._save_state()

    def toggle_overlay(self):
        if self._overlay and self._overlay.winfo_exists():
            self._overlay.destroy(); self._overlay=None
            self.restore_from_tray()
        else:
            self._overlay = Overlay(self)
            self.minimize_to_tray()

    def _rollover_if_needed(self, now):
        # ถ้าข้ามวันจาก self._cur_day → สรุป self._cur_day ลง daily_summary แล้วลบ samples ของวันนั้น
        day_now = today_str(now)
        if day_now != self._cur_day:
            try:
                summarize_day(self.conn, self._cur_day)
                delete_samples_of_day(self.conn, self._cur_day)
            except Exception as e:
                # ไม่หยุดโปรแกรม แค่แจ้งเตือนใน console
                print("rollover error:", e)
            self._cur_day = day_now

    def _loop(self):
        last = datetime.now()
        # prime CPU meter ให้ interval=None ใช้งานได้แม่นขึ้น
        _ = psutil.cpu_percent(interval=None)
        while self._running:
            now = datetime.now()
            self._rollover_if_needed(now)

            cpu_w = estimate_cpu_w(psutil.cpu_percent(interval=None))
            gpu_w = estimate_gpu_w()
            watts = cpu_w + gpu_w + MONITOR_W + OTHER_W

            dt = (now - last).total_seconds(); last = now
            self._kwh = integrate_kwh(self._kwh, watts, dt)
            self._cost = self._kwh * UNIT_PRICE
            self._watts = watts; self._gpu_w = gpu_w

            # เก็บ sample เฉพาะวันนี้ (เมื่อ rollover แล้วของเมื่อวานถูกลบไปแล้ว)
            try:
                insert_sample(self.conn, now, watts, self._kwh, self._cost)
            except Exception as e:
                print("insert sample error:", e)

            # บันทึก state เดือน (เพื่อจำต่อเนื่องข้ามการรีสตาร์ท)
            self._save_state()

            # คุม loop timing ตาม SAMPLE_SEC จาก config
            time.sleep(max(0.0, SAMPLE_SEC))

    def _ui_tick(self):
        self.kpi_watts.set(f"{self._watts:,.1f}")
        self.kpi_gpu.set(f"{self._gpu_w:.1f}")
        self.kpi_kwh.set(f"{self._kwh:.4f}")
        self.kpi_cost.set(f"{self._cost:.2f}")
        self.time_lbl.configure(text=f"⏱ {self._elapsed_str()}  |  DB: {DB_PATH}")
        self.after(300, self._ui_tick)


# --------------- Entry ---------------
if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--autostart",action="store_true")
    # overrides ทาง CLI (ถ้าอยากเซ็ตทับชั่วคราว/สคริปต์)
    parser.add_argument("--unit-price", type=float)
    parser.add_argument("--sample-sec", type=float)
    parser.add_argument("--cpu-tdp", type=float)
    parser.add_argument("--cpu-idle", type=float)
    parser.add_argument("--gpu-tdp", type=float)
    parser.add_argument("--gpu-idle", type=float)
    parser.add_argument("--monitor-w", type=float)
    parser.add_argument("--other-w", type=float)
    args=parser.parse_args()

    app=App(autostart_flag=args.autostart)

    # ถ้ามีค่า override ก็อัปเดต/เซฟ/ใช้ทันที
    overrides = {
        "unit_price": args.unit_price,
        "sample_sec": args.sample_sec,
        "cpu_tdp": args.cpu_tdp,
        "cpu_idle": args.cpu_idle,
        "gpu_tdp": args.gpu_tdp,
        "gpu_idle": args.gpu_idle,
        "monitor_w": args.monitor_w,
        "other_w": args.other_w,
    }
    changed = False
    for k, v in list(overrides.items()):
        if v is not None:
            app.cfg[k] = float(v); changed = True
    if changed:
        save_config(app.cfg)
        apply_config_globals(app.cfg)

    app.mainloop()
