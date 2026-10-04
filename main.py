"""
main.py — 克拉尼圖形音訊分析工具
Tkinter GUI + Matplotlib 即時頻譜視覺化 + INA219 直流量測

功能：
  - 即時 FFT 頻譜顯示與共振峰值自動偵測
  - Arduino + INA219 直流量測：即時電阻 R、電壓、電流、功率、漣波
  - 自動掃頻（支援由低到高或由高到低）
  - ★ 在掃頻曲線上點擊峰值 → 自動以 1 Hz 細掃該點 ±10 Hz
  - ★ 歷史記錄可依頻率排序，重複頻率自動取平均
  - 匯出 CSV

2026-09-30：新增點擊細掃、依頻率排序合併、反向掃頻；移除自動記錄門檻；介面改版。
"""

import csv
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import matplotlib
import matplotlib.figure as mpl_figure
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from audio_processor import AudioProcessor, AudioGenerator
from serial_meter import (
    RIPPLE_WARN_PCT, InaMeter, MeterError, list_serial_ports, serial_available,
)

matplotlib.use("TkAgg")

# ── 日誌 ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── 配色 ─────────────────────────────────────────────────────────────────────
THEME = {
    "bg":        "#12131a",   # 視窗底
    "surface":   "#1b1d27",   # 卡片
    "surface2":  "#232634",   # 卡片內的次層（輸入框、表格底）
    "border":    "#2e3243",
    "accent":    "#5b8cff",   # 主色（藍）
    "accent_dk": "#3f6ad8",
    "danger":    "#ff5c72",
    "danger_dk": "#d93a50",
    "ok":        "#3ddc97",
    "ok_dk":     "#28b87c",
    "text":      "#e8eaf2",
    "text_dim":  "#8b91a8",
    "spectrum":  "#4dd8ff",
    "peak":      "#ff8a4c",
    "electric":  "#ffc857",
}

# ── 字型（在 main() 由 init_fonts 決定）──────────────────────────────────────
UI_FAMILY = "TkDefaultFont"
MONO_FAMILY = "TkFixedFont"


def fnt(size: int, bold: bool = False) -> tuple:
    return (UI_FAMILY, size, "bold") if bold else (UI_FAMILY, size)


def mono(size: int, bold: bool = False) -> tuple:
    return (MONO_FAMILY, size, "bold") if bold else (MONO_FAMILY, size)


def init_fonts(root: tk.Tk) -> None:
    """
    挑選中文字型。微軟正黑體的小字可讀性遠優於標楷體。

    NOTE: Tk 與 Matplotlib 的字型名稱**不一定相同**，必須分開挑。
      例如 Windows 的 msjh.ttc 在 Tk 叫「Microsoft JhengHei UI」，
      但 Matplotlib 只認得「Microsoft JhengHei」——直接把 Tk 的名字丟給
      Matplotlib 會退回 DejaVu Sans，中文全變成方塊並洗出一堆 findfont 警告。
    """
    global UI_FAMILY, MONO_FAMILY

    # ── Tk 端 ──
    tk_fams = set(tkfont.families(root))
    for c in ("Microsoft JhengHei UI", "微軟正黑體", "Microsoft JhengHei",
              "Noto Sans CJK TC", "PingFang TC", "DFKai-SB", "標楷體"):
        if c in tk_fams:
            UI_FAMILY = c
            break
    for c in ("Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Courier New"):
        if c in tk_fams:
            MONO_FAMILY = c
            break

    # ── Matplotlib 端：只從它自己認得的清單裡挑 ──
    from matplotlib import font_manager as fm
    mpl_fams = {f.name for f in fm.fontManager.ttflist}
    mpl_pick = next(
        (c for c in ("Microsoft JhengHei", "微軟正黑體", "Microsoft YaHei",
                     "Noto Sans CJK TC", "Noto Sans TC", "PingFang TC",
                     "DFKai-SB", "標楷體", "MingLiU", "細明體", "SimHei")
         if c in mpl_fams), None)
    if mpl_pick:
        matplotlib.rcParams["font.family"] = [mpl_pick, "sans-serif"]
        logger.info("圖表中文字型：%s（介面字型：%s）", mpl_pick, UI_FAMILY)
    else:
        matplotlib.rcParams["font.family"] = ["sans-serif"]
        logger.warning("Matplotlib 找不到中文字型，圖表中文會顯示為方塊。"
                       "可執行：python -c \"import matplotlib;"
                       "print(matplotlib.get_cachedir())\" 後刪除該快取資料夾再試。")
    matplotlib.rcParams["axes.unicode_minus"] = False
    # 字型找不到時 matplotlib 每畫一次就警告一行，會把日誌洗掉
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)


# ── 更新頻率 ─────────────────────────────────────────────────────────────────
REFRESH_MS      = 50    # 頻譜更新間隔（毫秒）
ELEC_REFRESH_MS = 200   # 電氣讀值更新間隔（毫秒）

# 細掃參數：點擊峰值後掃 ±FINE_SPAN Hz，間隔 FINE_STEP Hz
FINE_SPAN = 10
FINE_STEP = 1
# 點擊位置與已記錄頻率相差多少以內就吸附過去
SNAP_HZ = 20.0
# 合併「相同頻率」時的四捨五入位數
MERGE_DECIMALS = 1

# 掃頻曲線右軸可選的物理量：顯示名稱 → (記錄欄位, 軸標籤)
Y2_CHOICES = {
    "電阻 R (Ω)":    ("r",   "電阻 R (Ω)"),
    "ΔP 功率 (mW)":  ("dp",  "激振功率增量 ΔP (mW)"),
    "電流 Idc (mA)": ("idc", "直流電流 Idc (mA)"),
    "ΔI 電流 (mA)":  ("di",  "電流增量 ΔI (mA)"),
}

CSV_HEADER = [
    "時間", "目標頻率 (Hz)", "主峰頻率 (Hz)", "dBSPL",
    "電阻 R (Ω)", "電壓 Vdc (V)", "電流 Idc (mA)", "功率 P (mW)",
    "ΔI (mA)", "ΔP (mW)", "漣波 Iac (mA)", "漣波比 (%)",
    "取樣率 (Hz)", "歸零偏移 (mA)", "合併筆數",
    "峰值1 (Hz)", "峰值2 (Hz)", "峰值3 (Hz)", "峰值4 (Hz)", "峰值5 (Hz)",
]

# 合併時要取平均的數值欄位
AVG_KEYS = ("top_freq", "db", "r", "vdc", "idc", "p",
            "di", "dp", "iac", "ripple", "fs", "off")


def _fmt(value, digits: int = 2) -> str:
    """把可能為 None 的數值格式化成字串。"""
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def build_freq_list(start: float, end: float, step: float) -> list[float]:
    """
    產生掃頻頻率序列，支援由低到高與由高到低。

    start > end 時自動反向遞減。無論方向，結尾都會補上終點。
    """
    step = abs(float(step)) or 1.0
    if end >= start:
        n = int((end - start) / step)
        freqs = [start + i * step for i in range(n + 1)]
    else:
        n = int((start - end) / step)
        freqs = [start - i * step for i in range(n + 1)]
    if freqs and abs(freqs[-1] - end) > 1e-6:
        freqs.append(float(end))
    return freqs


class ChladniAnalyzerApp:
    """克拉尼圖形音訊分析主視窗。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("克拉尼圖形音訊分析儀")
        self.root.configure(bg=THEME["bg"])
        self.root.geometry("1560x920")
        self.root.minsize(1120, 700)

        # 音訊狀態
        self._is_running   = False
        self._processor: AudioProcessor | None = None
        self._generator: AudioGenerator = AudioGenerator()
        self._frame_queue: queue.Queue = queue.Queue(maxsize=2)
        self._worker_thread: threading.Thread | None = None
        self._record_history: list[dict] = []
        self._current_peaks: list[dict] = []
        self._current_db: float = 0.0

        # 掃頻狀態
        self._is_sweeping = False
        self._sweep_thread: threading.Thread | None = None
        self._current_target_freq: float | None = None
        self._sweep_is_fine = False

        # 直流量測狀態
        self._meter: InaMeter | None = None
        self._elec_running = False
        self._elec_thread: threading.Thread | None = None
        self._latest_elec: dict | None = None
        self._baseline: dict | None = None

        self._build_ui()
        self._populate_devices()
        self._populate_serial_ports()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_elec_label()

    # ══════════════════════════════════════════════════════════════════════
    #  UI 小工具
    # ══════════════════════════════════════════════════════════════════════

    def _card(self, parent, title: str | None = None, **grid) -> tk.Frame:
        """
        帶細邊框的卡片容器；title 不為 None 時加上標題列。

        回傳的是**內容區（body）**，不是最外層。
        NOTE: 外層自己用 pack 排標題列，若呼叫端又對同一個 parent 用 grid，
          Tk 會丟 "cannot use geometry manager grid ... already has slaves
          managed by pack"。回傳獨立的 body 讓呼叫端可自由使用 pack 或 grid。
        """
        outer = tk.Frame(parent, bg=THEME["surface"],
                         highlightbackground=THEME["border"],
                         highlightthickness=1, bd=0)
        if grid:
            outer.grid(**grid)
        if title:
            head = tk.Frame(outer, bg=THEME["surface"])
            head.pack(fill="x", padx=12, pady=(9, 0))
            tk.Frame(head, bg=THEME["accent"], width=3, height=13).pack(
                side="left", padx=(0, 7))
            tk.Label(head, text=title, bg=THEME["surface"], fg=THEME["text"],
                     font=fnt(10, True)).pack(side="left")
        body = tk.Frame(outer, bg=THEME["surface"])
        body.pack(fill="both", expand=True)
        return body

    def _button(self, parent, text, command, kind="ghost", width=None):
        """統一樣式的扁平按鈕，含滑鼠滑過回饋。"""
        palette = {
            "primary": (THEME["accent"], THEME["accent_dk"], "#ffffff"),
            "danger":  (THEME["danger"], THEME["danger_dk"], "#ffffff"),
            "ok":      (THEME["ok"], THEME["ok_dk"], "#0d1117"),
            "ghost":   (THEME["surface2"], THEME["border"], THEME["text"]),
        }
        base, hover, fg = palette[kind]
        b = tk.Button(parent, text=text, command=command,
                      bg=base, fg=fg, activebackground=hover, activeforeground=fg,
                      font=fnt(9, kind != "ghost"), relief="flat", bd=0,
                      padx=12, pady=5, cursor="hand2", highlightthickness=0)
        if width:
            b.config(width=width)
        b._base, b._hover = base, hover

        def on_enter(e):
            if str(e.widget["state"]) != "disabled":
                e.widget.config(bg=e.widget._hover)

        b.bind("<Enter>", on_enter)
        b.bind("<Leave>", lambda e: e.widget.config(bg=e.widget._base))
        return b

    def _label(self, parent, text, dim=False, size=9, bold=False):
        return tk.Label(parent, text=text, bg=parent["bg"],
                        fg=THEME["text_dim"] if dim else THEME["text"],
                        font=fnt(size, bold))

    def _spin(self, parent, var, frm, to, inc=1, width=6, fmt=None):
        kw = dict(from_=frm, to=to, increment=inc, textvariable=var, width=width,
                  font=fnt(9), bg=THEME["surface2"], fg=THEME["text"],
                  buttonbackground=THEME["border"], relief="flat",
                  insertbackground=THEME["text"], highlightthickness=1,
                  highlightbackground=THEME["border"], justify="center")
        if fmt:
            kw["format"] = fmt
        return tk.Spinbox(parent, **kw)

    # ══════════════════════════════════════════════════════════════════════
    #  UI 組裝
    # ══════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self._build_header()
        self._build_body()
        self._build_status_bar()

    def _build_header(self) -> None:
        head = tk.Frame(self.root, bg=THEME["bg"])
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        head.columnconfigure(0, weight=1)

        title_row = tk.Frame(head, bg=THEME["bg"])
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tk.Label(title_row, text="克拉尼圖形音訊分析儀", bg=THEME["bg"],
                 fg=THEME["text"], font=fnt(15, True)).pack(side="left")
        tk.Label(title_row, text="聲壓 × 直流電阻 雙通道掃頻", bg=THEME["bg"],
                 fg=THEME["text_dim"], font=fnt(9)).pack(side="left", padx=(10, 0))

        self._run_pill = tk.Label(title_row, text="  待機中  ", bg=THEME["surface2"],
                                  fg=THEME["text_dim"], font=fnt(9, True),
                                  padx=6, pady=3)
        self._run_pill.pack(side="right")

        self._build_audio_card(head)
        self._build_meter_card(head)

    def _build_audio_card(self, parent) -> None:
        card = self._card(parent, "音訊", row=1, column=0, sticky="ew", pady=(0, 6))
        row = tk.Frame(card, bg=THEME["surface"])
        row.pack(fill="x", padx=12, pady=(6, 11))

        self._label(row, "輸入").pack(side="left")
        self._device_var = tk.StringVar()
        self._device_combo = ttk.Combobox(row, textvariable=self._device_var,
                                          width=20, state="readonly", font=fnt(9))
        self._device_combo.pack(side="left", padx=(5, 14))

        self._label(row, "輸出").pack(side="left")
        self._out_device_var = tk.StringVar()
        self._out_device_combo = ttk.Combobox(row, textvariable=self._out_device_var,
                                              width=20, state="readonly", font=fnt(9))
        self._out_device_combo.pack(side="left", padx=(5, 14))

        self._label(row, "顯示範圍 (Hz)").pack(side="left")
        self._freq_min_var = tk.IntVar(value=20)
        self._freq_max_var = tk.IntVar(value=4000)
        self._spin(row, self._freq_min_var, 10, 500, width=5).pack(side="left", padx=(5, 2))
        self._label(row, "~", dim=True).pack(side="left")
        self._spin(row, self._freq_max_var, 200, 20000, width=6).pack(side="left", padx=(2, 14))

        self._label(row, "尋峰靈敏度").pack(side="left")
        self._prominence_var = tk.DoubleVar(value=0.005)
        self._spin(row, self._prominence_var, 0.001, 0.5, 0.001, 7, "%.3f").pack(
            side="left", padx=(5, 14))

        self._btn_stop = self._button(row, "停止", self._stop, "danger", width=6)
        self._btn_stop.pack(side="right")
        self._btn_stop.config(state="disabled")
        self._btn_start = self._button(row, "開始", self._start, "ok", width=6)
        self._btn_start.pack(side="right", padx=(0, 6))

    def _build_meter_card(self, parent) -> None:
        card = self._card(parent, "直流量測（Arduino + INA219）",
                          row=2, column=0, sticky="ew")
        row = tk.Frame(card, bg=THEME["surface"])
        row.pack(fill="x", padx=12, pady=(6, 11))

        self._label(row, "序列埠").pack(side="left")
        self._serial_var = tk.StringVar()
        self._serial_combo = ttk.Combobox(row, textvariable=self._serial_var,
                                          width=28, state="readonly", font=fnt(9))
        self._serial_combo.pack(side="left", padx=(5, 4))
        self._button(row, "↻", self._populate_serial_ports).pack(side="left")
        self._btn_serial = self._button(row, "連線", self._toggle_serial,
                                        "primary", width=6)
        self._btn_serial.pack(side="left", padx=(6, 16))

        self._label(row, "平均窗 (ms)").pack(side="left")
        self._elec_window_var = tk.IntVar(value=500)
        self._spin(row, self._elec_window_var, 100, 2000, 50).pack(side="left", padx=(5, 14))

        self._label(row, "ADC 位元").pack(side="left")
        self._bits_var = tk.StringVar(value="12")
        bits_combo = ttk.Combobox(row, textvariable=self._bits_var, width=4,
                                  state="readonly", values=("9", "10", "11", "12"),
                                  font=fnt(9))
        bits_combo.pack(side="left", padx=(5, 14))
        bits_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_bits())

        self._button(row, "歸零", self._zero_meter).pack(side="left", padx=(0, 5))
        self._button(row, "記錄靜音基線", self._set_baseline).pack(side="left")

        self._serial_status = tk.Label(row, text="未連線", bg=THEME["surface"],
                                       fg=THEME["text_dim"], font=fnt(9))
        self._serial_status.pack(side="right")

        if not serial_available():
            self._serial_status.config(text="未安裝 pyserial（pip install pyserial）",
                                       fg=THEME["danger"])
            self._btn_serial.config(state="disabled")

    # ── 主體 ─────────────────────────────────────────────────────────────

    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=THEME["bg"])
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=380)
        body.rowconfigure(0, weight=1)

        self._build_plot_panel(body)
        self._build_side_panel(body)

    def _build_plot_panel(self, parent) -> None:
        wrap = self._card(parent, None, row=0, column=0, sticky="nsew", padx=(0, 8))
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        nb = ttk.Notebook(wrap)
        nb.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        spec = tk.Frame(nb, bg=THEME["surface"])
        sweep = tk.Frame(nb, bg=THEME["surface"])
        nb.add(spec, text="   即時頻譜   ")
        nb.add(sweep, text="   掃頻曲線   ")
        self._notebook = nb

        self._build_spectrum_panel(spec)
        self._build_sweep_panel(sweep)

    def _style_axes(self, ax) -> None:
        ax.set_facecolor(THEME["bg"])
        ax.tick_params(colors=THEME["text_dim"], labelsize=8)
        for s in ax.spines.values():
            s.set_color(THEME["border"])

    def _build_spectrum_panel(self, frame) -> None:
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # NOTE: 直接用 Figure()，不走 pyplot 的 figure manager，
        #       避免串流關閉時 pyplot 生命週期管理意外觸發視窗關閉
        self._fig = mpl_figure.Figure(figsize=(9, 5), dpi=100)
        self._fig.patch.set_facecolor(THEME["surface"])
        self._ax = self._fig.add_subplot(111)
        self._style_axes(self._ax)

        (self._line,) = self._ax.plot([], [], color=THEME["spectrum"], lw=1.2)
        self._peak_scatter = self._ax.scatter([], [], color=THEME["peak"], s=55,
                                              zorder=5, label="共振峰值")
        self._peak_texts: list = []

        self._ax.set_xlabel("頻率 (Hz)", color=THEME["text_dim"], fontsize=9)
        self._ax.set_ylabel("振幅", color=THEME["text_dim"], fontsize=9)
        self._ax.set_title("即時 FFT 頻譜", color=THEME["text"], fontsize=11)
        self._ax.grid(True, color=THEME["border"], alpha=0.6, linestyle="--", lw=0.6)
        self._ax.legend(facecolor=THEME["surface"], edgecolor=THEME["border"],
                        labelcolor=THEME["text"], fontsize=8)
        self._fig.tight_layout(pad=1.4)

        self._canvas = FigureCanvasTkAgg(self._fig, master=frame)
        self._canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)

    def _build_sweep_panel(self, frame) -> None:
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        bar = tk.Frame(frame, bg=THEME["surface"])
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))

        tk.Label(bar, text="右軸", bg=THEME["surface"], fg=THEME["text"],
                 font=fnt(9)).pack(side="left")
        self._y2_var = tk.StringVar(value="電阻 R (Ω)")
        y2 = ttk.Combobox(bar, textvariable=self._y2_var, width=15,
                          state="readonly", values=list(Y2_CHOICES), font=fnt(9))
        y2.pack(side="left", padx=(5, 12))
        y2.bind("<<ComboboxSelected>>", lambda _e: self._redraw_sweep_curve())

        tk.Label(bar, text=f"點擊曲線上的峰 → 自動以 {FINE_STEP} Hz 細掃 ±{FINE_SPAN} Hz",
                 bg=THEME["surface"], fg=THEME["accent"],
                 font=fnt(9, True)).pack(side="left")
        tk.Label(bar, text="（ΔI / ΔP 需先記錄靜音基線）", bg=THEME["surface"],
                 fg=THEME["text_dim"], font=fnt(8)).pack(side="right")

        holder = tk.Frame(frame, bg=THEME["surface"])
        holder.grid(row=1, column=0, sticky="nsew")

        self._fig2 = mpl_figure.Figure(figsize=(9, 5), dpi=100)
        self._fig2.patch.set_facecolor(THEME["surface"])
        self._ax_db = self._fig2.add_subplot(111)
        self._style_axes(self._ax_db)
        self._ax_e = self._ax_db.twinx()
        self._style_axes(self._ax_e)
        self._ax_e.set_facecolor("none")

        (self._sweep_db_line,) = self._ax_db.plot(
            [], [], color=THEME["spectrum"], marker="o", ms=3, lw=1.3, label="dBSPL")
        (self._sweep_e_line,) = self._ax_e.plot(
            [], [], color=THEME["electric"], marker="s", ms=3, lw=1.3)
        # 細掃標記線
        self._fine_marker = self._ax_db.axvline(0, color=THEME["peak"], lw=1.0,
                                                ls=":", alpha=0.0)

        self._ax_db.set_xlabel("驅動頻率 (Hz)", color=THEME["text_dim"], fontsize=9)
        self._ax_db.set_ylabel("聲壓位準 (dBSPL)", color=THEME["spectrum"], fontsize=9)
        self._ax_e.set_ylabel("電阻 R (Ω)", color=THEME["electric"], fontsize=9)
        self._ax_db.set_title("掃頻結果：聲壓 vs 電氣量", color=THEME["text"], fontsize=11)
        self._ax_db.grid(True, color=THEME["border"], alpha=0.6, linestyle="--", lw=0.6)
        self._fig2.tight_layout(pad=1.4)

        self._canvas2 = FigureCanvasTkAgg(self._fig2, master=holder)
        self._canvas2.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)
        # ★ 點擊峰值 → 細掃
        self._canvas2.mpl_connect("button_press_event", self._on_sweep_click)

    # ── 右側面板 ─────────────────────────────────────────────────────────

    def _build_side_panel(self, parent) -> None:
        side = tk.Frame(parent, bg=THEME["bg"])
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        side.rowconfigure(2, weight=1)

        self._build_readout_card(side)
        self._build_peaks_card(side)
        self._build_history_card(side)
        self._build_sweep_card(side)

    def _build_readout_card(self, parent) -> None:
        card = self._card(parent, "即時讀值", row=0, column=0, sticky="ew", pady=(0, 8))

        grid = tk.Frame(card, bg=THEME["surface"])
        grid.pack(fill="x", padx=12, pady=(8, 4))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        for col, (cap, attr, color) in enumerate(
                (("聲壓位準 (dBSPL)", "_db_label", THEME["ok"]),
                 ("電阻 R", "_r_label", THEME["electric"]))):
            box = tk.Frame(grid, bg=THEME["surface2"])
            box.grid(row=0, column=col, sticky="ew",
                     padx=(0, 6) if col == 0 else (6, 0))
            tk.Label(box, text=cap, bg=THEME["surface2"], fg=THEME["text_dim"],
                     font=fnt(8)).pack(pady=(7, 0))
            lab = tk.Label(box, text="--", bg=THEME["surface2"], fg=color,
                           font=mono(18, True))
            lab.pack(pady=(0, 8))
            setattr(self, attr, lab)

        self._elec_detail = tk.Label(card, text="未連線量測儀", bg=THEME["surface"],
                                     fg=THEME["text_dim"], font=mono(8),
                                     justify="left", anchor="w")
        self._elec_detail.pack(fill="x", padx=12, pady=(2, 0))
        self._elec_warn = tk.Label(card, text="", bg=THEME["surface"],
                                   fg=THEME["danger"], font=fnt(8), anchor="w")
        self._elec_warn.pack(fill="x", padx=12, pady=(0, 10))

    def _build_peaks_card(self, parent) -> None:
        card = self._card(parent, "本幀峰值", row=1, column=0, sticky="ew", pady=(0, 8))
        self._peak_list = tk.Listbox(card, height=5, bg=THEME["surface2"],
                                     fg=THEME["spectrum"], font=mono(9),
                                     selectbackground=THEME["accent"],
                                     relief="flat", borderwidth=0,
                                     highlightthickness=0)
        self._peak_list.pack(fill="x", padx=12, pady=(6, 10))

    def _build_history_card(self, parent) -> None:
        card = self._card(parent, "歷史記錄", row=2, column=0, sticky="nsew", pady=(0, 8))
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)

        btns = tk.Frame(card, bg=THEME["surface"])
        btns.grid(row=0, column=0, sticky="ew", padx=12, pady=(6, 6))
        self._button(btns, "記錄此刻", self._record_peaks, "primary").pack(
            side="left", padx=(0, 5))
        # ★ 依頻率排序（重複頻率取平均）
        self._sort_by_freq = tk.BooleanVar(value=False)
        self._btn_sort = self._button(btns, "依頻率排序", self._toggle_sort)
        self._btn_sort.pack(side="left", padx=(0, 5))
        self._button(btns, "匯出", self._export_csv).pack(side="left", padx=(0, 5))
        self._button(btns, "清除", self._clear_history).pack(side="left")

        table = tk.Frame(card, bg=THEME["surface"])
        table.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        cols = ("時間", "目標", "主峰", "dBSPL", "R(Ω)", "I(mA)", "N")
        widths = {"時間": 56, "目標": 54, "主峰": 54, "dBSPL": 48,
                  "R(Ω)": 56, "I(mA)": 52, "N": 24}
        self._hist_tree = ttk.Treeview(table, columns=cols, show="headings", height=9)
        for c in cols:
            self._hist_tree.heading(c, text=c)
            self._hist_tree.column(c, width=widths[c], anchor="center", stretch=False)
        sb = ttk.Scrollbar(table, orient="vertical", command=self._hist_tree.yview)
        self._hist_tree.configure(yscrollcommand=sb.set)
        self._hist_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        # 細掃來的列與合併列用不同顏色標示
        self._hist_tree.tag_configure("fine", background="#1f2a3d")
        self._hist_tree.tag_configure("merged", foreground=THEME["electric"])

    def _build_sweep_card(self, parent) -> None:
        card = self._card(parent, "自動掃頻", row=3, column=0, sticky="ew")
        g = tk.Frame(card, bg=THEME["surface"])
        g.pack(fill="x", padx=12, pady=(6, 10))
        for c in range(4):
            g.columnconfigure(c, weight=1)

        for c, txt in enumerate(("起始 (Hz)", "結束 (Hz)", "間隔 (Hz)", "停留 (秒)")):
            self._label(g, txt, dim=True, size=8).grid(row=0, column=c, sticky="w")

        self._sweep_start_var = tk.IntVar(value=100)
        self._sweep_end_var   = tk.IntVar(value=2000)
        self._sweep_step_var  = tk.IntVar(value=5)
        self._sweep_dwell_var = tk.DoubleVar(value=3.0)
        self._spin(g, self._sweep_start_var, 20, 20000, width=7).grid(
            row=1, column=0, sticky="w", pady=(1, 0))
        self._spin(g, self._sweep_end_var, 20, 20000, width=7).grid(
            row=1, column=1, sticky="w", pady=(1, 0))
        self._spin(g, self._sweep_step_var, 1, 1000, width=7).grid(
            row=1, column=2, sticky="w", pady=(1, 0))
        self._spin(g, self._sweep_dwell_var, 0.5, 30, 0.5, 7).grid(
            row=1, column=3, sticky="w", pady=(1, 0))

        # 起始 > 結束時自動反向遞減
        self._dir_hint = tk.Label(g, text="", bg=THEME["surface"],
                                  fg=THEME["text_dim"], font=fnt(8), anchor="w")
        self._dir_hint.grid(row=2, column=0, columnspan=4, sticky="w", pady=(5, 0))
        for v in (self._sweep_start_var, self._sweep_end_var, self._sweep_step_var):
            v.trace_add("write", lambda *_: self._update_dir_hint())
        self._update_dir_hint()

        self._btn_sweep = self._button(g, "開始掃描", self._toggle_sweep, "primary")
        self._btn_sweep.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(7, 0))

        save_row = tk.Frame(g, bg=THEME["surface"])
        save_row.grid(row=3, column=2, columnspan=2, sticky="ew", pady=(7, 0), padx=(6, 0))
        self._sweep_auto_save = tk.BooleanVar(value=True)
        self._sweep_save_dir: str | None = os.path.join(os.path.expanduser("~"), "Desktop")
        tk.Checkbutton(save_row, text="掃完自動存檔", variable=self._sweep_auto_save,
                       command=self._on_sweep_auto_save_toggle,
                       bg=THEME["surface"], fg=THEME["text"],
                       selectcolor=THEME["surface2"], activebackground=THEME["surface"],
                       activeforeground=THEME["text"], font=fnt(8),
                       highlightthickness=0, bd=0).pack(anchor="w")
        disp = self._sweep_save_dir
        self._sweep_save_dir_label = tk.Label(
            save_row, text=disp if len(disp) <= 26 else "…" + disp[-23:],
            bg=THEME["surface"], fg=THEME["ok"], font=fnt(8), cursor="hand2")
        self._sweep_save_dir_label.pack(anchor="w")
        self._sweep_save_dir_label.bind("<Button-1>", lambda _: self._choose_sweep_save_dir())

    def _build_status_bar(self) -> None:
        self._status_var = tk.StringVar(value="待機中 — 請選擇麥克風並按「開始」")
        bar = tk.Frame(self.root, bg=THEME["surface"],
                       highlightbackground=THEME["border"], highlightthickness=1)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 12))
        tk.Label(bar, textvariable=self._status_var, bg=THEME["surface"],
                 fg=THEME["text_dim"], font=fnt(9), anchor="w").pack(
            fill="x", padx=12, pady=5)

    def _update_dir_hint(self) -> None:
        """顯示目前設定會往哪個方向掃、共幾點。"""
        try:
            s, e, st = (self._sweep_start_var.get(), self._sweep_end_var.get(),
                        self._sweep_step_var.get())
        except Exception:
            self._dir_hint.config(text="")
            return
        n = len(build_freq_list(s, e, st))
        arrow = "↑ 由低到高" if e >= s else "↓ 由高到低"
        self._dir_hint.config(text=f"{arrow}　共 {n} 點")

    def _set_pill(self, text, color) -> None:
        self._run_pill.config(text=f"  {text}  ", fg=color)

    # ══════════════════════════════════════════════════════════════════════
    #  裝置管理
    # ══════════════════════════════════════════════════════════════════════

    def _populate_devices(self) -> None:
        try:
            tmp = AudioProcessor()
            ins = tmp.list_input_devices()
            outs = tmp.list_output_devices()
            del tmp
            self._device_combo["values"] = [f"[{d['index']}] {d['name']}" for d in ins]
            if ins:
                self._device_combo.current(0)
            self._out_device_combo["values"] = [f"[{d['index']}] {d['name']}" for d in outs]
            if outs:
                self._out_device_combo.current(0)
        except Exception as exc:
            logger.error("無法列舉裝置：%s", exc)
            messagebox.showerror("裝置錯誤", f"無法取得音訊裝置清單：\n{exc}")

    def _get_selected_device_index(self) -> int | None:
        sel = self._device_var.get()
        return int(sel.split("]")[0].replace("[", "").strip()) if sel else None

    def _get_selected_out_device_index(self) -> int | None:
        sel = self._out_device_var.get()
        return int(sel.split("]")[0].replace("[", "").strip()) if sel else None

    # ══════════════════════════════════════════════════════════════════════
    #  序列埠 / 直流量測
    # ══════════════════════════════════════════════════════════════════════

    def _populate_serial_ports(self) -> None:
        if not serial_available():
            return
        ports = list_serial_ports()
        self._serial_ports = ports
        self._serial_combo["values"] = [p["desc"] for p in ports]
        if ports:
            idx = 0
            for i, p in enumerate(ports):
                if any(k in p["desc"].lower()
                       for k in ("arduino", "ch340", "usb serial", "wch")):
                    idx = i
                    break
            self._serial_combo.current(idx)
        else:
            self._serial_var.set("")

    def _get_selected_port(self) -> str | None:
        sel = self._serial_var.get()
        for p in getattr(self, "_serial_ports", []):
            if p["desc"] == sel:
                return p["device"]
        return None

    def _toggle_serial(self) -> None:
        self._disconnect_meter() if self._meter else self._connect_meter()

    def _connect_meter(self) -> None:
        port = self._get_selected_port()
        if not port:
            messagebox.showinfo("提示", "請先選擇序列埠。若清單為空請按「↻」重新掃描。")
            return
        try:
            meter = InaMeter(port)
            meter.open()
            info = meter.set_param("bits", self._bits_var.get())
        except MeterError as exc:
            messagebox.showerror("連線失敗", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("連線失敗", f"{port}：{exc}")
            return

        self._meter = meter
        self._btn_serial.config(text="斷線", bg=THEME["danger"], fg="#fff")
        self._btn_serial._base = THEME["danger"]
        self._btn_serial._hover = THEME["danger_dk"]
        self._serial_status.config(
            text=f"● 已連線 {port}　shunt {info.get('shunt')}Ω　"
                 f"{_fmt(info.get('bits'), 0)}-bit",
            fg=THEME["ok"])
        logger.info("直流量測已連線：%s", info)

        self._elec_running = True
        self._elec_thread = threading.Thread(target=self._elec_loop, daemon=True)
        self._elec_thread.start()

    def _disconnect_meter(self) -> None:
        self._elec_running = False
        if self._elec_thread and self._elec_thread.is_alive():
            self._elec_thread.join(timeout=4.0)
        self._elec_thread = None
        if self._meter:
            self._meter.close()
        self._meter = None
        self._latest_elec = None
        self._btn_serial.config(text="連線", bg=THEME["accent"], fg="#fff")
        self._btn_serial._base = THEME["accent"]
        self._btn_serial._hover = THEME["accent_dk"]
        self._serial_status.config(text="未連線", fg=THEME["text_dim"])

    def _apply_bits(self) -> None:
        """直流量測不怕轉換窗長，位元越高解析度越好（12-bit：0.1 mA @0.1Ω）。"""
        if not self._meter:
            return
        try:
            info = self._meter.set_param("bits", self._bits_var.get())
            self._status_var.set(f"ADC 已切換為 {self._bits_var.get()}-bit"
                                 f"（轉換窗 {_fmt(info.get('tconv'), 0)} µs）")
        except Exception as exc:
            logger.warning("切換 bits 失敗：%s", exc)

    def _zero_meter(self) -> None:
        """歸零：扣掉分流路徑的直流偏移。必須先斷開負載。"""
        if not self._meter:
            messagebox.showinfo("提示", "請先連線量測儀。")
            return
        if not messagebox.askyesno(
                "歸零確認",
                "歸零會把「目前讀到的電流」當成偏移值扣掉。\n\n"
                "請先斷開負載（讓分流電阻上沒有真實電流），\n確認已斷開後再按「是」。"):
            return

        was = self._elec_running
        self._elec_running = False
        if self._elec_thread and self._elec_thread.is_alive():
            self._elec_thread.join(timeout=4.0)
        try:
            info = self._meter.zero(800)
            off = info.get("off")
            self._status_var.set(f"已歸零，偏移 {_fmt(off, 3)} mA")
            messagebox.showinfo("歸零完成", f"已扣除直流偏移：{_fmt(off, 3)} mA\n請接回負載。")
        except Exception as exc:
            messagebox.showerror("歸零失敗", str(exc))
        finally:
            if was:
                self._elec_running = True
                self._elec_thread = threading.Thread(target=self._elec_loop, daemon=True)
                self._elec_thread.start()

    def _set_baseline(self) -> None:
        """記錄靜音基線，之後每筆記錄會算出 ΔI / ΔP（扣掉模組靜態消耗）。"""
        res = self._latest_elec
        if not res:
            messagebox.showinfo("提示", "還沒有量測資料，請先連線量測儀。")
            return
        if self._is_sweeping:
            messagebox.showinfo("提示", "掃頻進行中，請先停止掃描再記錄基線。")
            return
        self._baseline = {"idc": res.get("idc"), "p": res.get("p"), "r": res.get("r")}
        self._status_var.set(
            f"已記錄靜音基線：Idc {_fmt(res.get('idc'), 2)} mA、"
            f"P {_fmt(res.get('p'), 1)} mW、R {_fmt(res.get('r'), 3)} Ω")
        logger.info("靜音基線：%s", self._baseline)

    def _elec_loop(self) -> None:
        """背景執行緒：持續向 Arduino 要直流量測結果，附上起訖時間戳。"""
        while self._elec_running and self._meter:
            t_start = time.time()
            try:
                window = int(self._elec_window_var.get())
            except Exception:
                window = 500
            try:
                res = self._meter.measure(window)
                res["t_start"] = t_start
                res["t_end"] = time.time()
                self._latest_elec = res
            except Exception as exc:
                logger.warning("直流量測失敗：%s", exc)
                time.sleep(0.5)

    def _wait_fresh_elec(self, after_ts: float, timeout: float) -> dict | None:
        """等一筆「開始時間晚於發聲時刻」的量測，確保和目標頻率同步。"""
        if not self._meter:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self._latest_elec
            if res and res.get("t_start", 0) >= after_ts:
                return res
            time.sleep(0.05)
        return self._latest_elec

    def _update_elec_label(self) -> None:
        res = self._latest_elec
        if res:
            r = res.get("r")
            self._r_label.config(text=f"{r:.2f} Ω" if r is not None else "-- Ω")
            lines = [
                f"V {_fmt(res.get('vdc'), 3):>7} V   I {_fmt(res.get('idc'), 2):>7} mA"
                f"   P {_fmt(res.get('p'), 1):>7} mW",
                f"漣波 {_fmt(res.get('iac'), 2)} mA ({_fmt(res.get('ripple'), 1)}%)"
                f"   fs {_fmt(res.get('fs'), 0)} Hz",
            ]
            if self._baseline and self._baseline.get("idc") is not None \
                    and res.get("idc") is not None:
                di = res["idc"] - self._baseline["idc"]
                dp = (res.get("p") or 0) - (self._baseline.get("p") or 0)
                lines.append(f"ΔI {di:+.2f} mA   ΔP {dp:+.1f} mW")
            self._elec_detail.config(text="\n".join(lines))

            rip = res.get("ripple")
            self._elec_warn.config(
                text=f"⚠ 漣波 {rip:.0f}% 偏高，請加長平均窗或加濾波電容"
                if (rip is not None and rip > RIPPLE_WARN_PCT) else "")
        elif self._meter is None:
            self._r_label.config(text="-- Ω")
            self._elec_detail.config(text="未連線量測儀")
            self._elec_warn.config(text="")
        self.root.after(ELEC_REFRESH_MS, self._update_elec_label)

    # ══════════════════════════════════════════════════════════════════════
    #  音訊分析
    # ══════════════════════════════════════════════════════════════════════

    def _start(self) -> None:
        if self._is_running:
            return
        try:
            self._processor = AudioProcessor(device_index=self._get_selected_device_index())
            self._processor.open_stream()
        except Exception as exc:
            messagebox.showerror("串流錯誤", f"無法開啟麥克風：\n{exc}")
            return

        self._is_running = True
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._set_pill("錄音中", THEME["ok"])
        self._status_var.set("錄音中 — 即時分析頻譜…")

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._update_plot()

    def _stop(self) -> None:
        if not self._is_running:
            return
        self._is_running = False
        if self._processor:
            self._processor.close_stream()
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")
        self._set_pill("待機中", THEME["text_dim"])
        self._status_var.set("已停止 — 點擊「開始」重新啟動")

    def _worker_loop(self) -> None:
        """背景執行緒：持續讀幀並送入 Queue，避免 UI 卡頓。"""
        while self._is_running and self._processor:
            try:
                fa, mag, db = self._processor.read_frame()
                if self._frame_queue.full():
                    try:
                        self._frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                self._frame_queue.put_nowait((fa, mag, db))
            except Exception as exc:
                logger.warning("讀幀錯誤：%s", exc)
                time.sleep(0.01)

    def _update_plot(self) -> None:
        if not self._is_running:
            return
        try:
            freq_axis, magnitude, db = self._frame_queue.get_nowait()
        except queue.Empty:
            self.root.after(REFRESH_MS, self._update_plot)
            return

        fmin, fmax = self._freq_min_var.get(), self._freq_max_var.get()
        prom = self._prominence_var.get()

        mask = (freq_axis >= fmin) & (freq_axis <= fmax)
        x, y = freq_axis[mask], magnitude[mask]
        self._line.set_data(x, y)
        self._ax.set_xlim(fmin, fmax)
        y_max = max(y.max() * 1.3, 0.01) if len(y) else 0.01
        self._ax.set_ylim(0, y_max)

        peaks = AudioProcessor.detect_peaks(freq_axis, magnitude, min_freq=fmin,
                                            max_freq=fmax, prominence=prom, top_n=8)
        self._current_peaks = peaks
        self._current_db = db
        self._current_freq_axis = freq_axis
        self._current_magnitude = magnitude

        if peaks:
            self._peak_scatter.set_offsets(np.column_stack(
                [[p["freq"] for p in peaks], [p["amplitude"] for p in peaks]]))
        else:
            self._peak_scatter.set_offsets(np.empty((0, 2)))

        for t in self._peak_texts:
            t.remove()
        self._peak_texts.clear()
        for p in peaks[:3]:
            self._peak_texts.append(self._ax.text(
                p["freq"], p["amplitude"] + y_max * 0.03, f"{p['freq']:.1f} Hz",
                color=THEME["peak"], fontsize=8, ha="center"))

        self._canvas.draw_idle()
        self._db_label.config(text=f"{db:.1f}")

        self._peak_list.delete(0, tk.END)
        for i, p in enumerate(peaks):
            self._peak_list.insert(
                tk.END,
                f"{'★' if i == 0 else '◆'} {p['freq']:>8.1f} Hz │ {p['amplitude']:.5f}")

        self.root.after(REFRESH_MS, self._update_plot)

    # ══════════════════════════════════════════════════════════════════════
    #  記錄、排序與合併
    # ══════════════════════════════════════════════════════════════════════

    def _build_row(self, ts: str, top_freq: float, db: float, elec: dict | None) -> dict:
        elec = elec or {}
        idc, p = elec.get("idc"), elec.get("p")
        di = dp = None
        if self._baseline:
            b_i, b_p = self._baseline.get("idc"), self._baseline.get("p")
            if idc is not None and b_i is not None:
                di = idc - b_i
            if p is not None and b_p is not None:
                dp = p - b_p
        return {
            "time": ts, "target_freq": self._current_target_freq,
            "top_freq": top_freq, "db": db, "peaks": list(self._current_peaks),
            "r": elec.get("r"), "vdc": elec.get("vdc"), "idc": idc, "p": p,
            "di": di, "dp": dp, "iac": elec.get("iac"), "ripple": elec.get("ripple"),
            "fs": elec.get("fs"), "off": elec.get("off"),
            "fine": self._sweep_is_fine, "n_merge": 1,
        }

    def _merge_group(self, rows: list[dict], key: float) -> dict:
        """把同一頻率的多筆記錄合併成一筆（數值欄位取平均）。"""
        if len(rows) == 1:
            out = dict(rows[0])
            out["n_merge"] = 1
            return out
        out = {
            "time": rows[-1]["time"], "target_freq": key,
            "peaks": rows[-1]["peaks"], "n_merge": len(rows),
            "fine": any(r.get("fine") for r in rows),
        }
        for k in AVG_KEYS:
            vals = [r[k] for r in rows if r.get(k) is not None]
            out[k] = sum(vals) / len(vals) if vals else None
        return out

    def _merged_rows(self) -> list[dict]:
        """依頻率排序並把相同頻率合併平均（圖表與排序檢視共用）。"""
        groups: dict[float, list[dict]] = {}
        for r in self._record_history:
            f = r.get("target_freq")
            if f is None:
                f = r.get("top_freq")
            groups.setdefault(round(float(f), MERGE_DECIMALS), []).append(r)
        return [self._merge_group(groups[k], k) for k in sorted(groups)]

    def _display_rows(self) -> list[dict]:
        """目前檢視模式下要顯示／匯出的列。"""
        return self._merged_rows() if self._sort_by_freq.get() else list(self._record_history)

    def _toggle_sort(self) -> None:
        """切換「原始順序」與「依頻率排序（重複取平均）」。"""
        self._sort_by_freq.set(not self._sort_by_freq.get())
        on = self._sort_by_freq.get()
        self._btn_sort.config(text="原始順序" if on else "依頻率排序",
                              bg=THEME["accent"] if on else THEME["surface2"],
                              fg="#fff" if on else THEME["text"])
        self._btn_sort._base = THEME["accent"] if on else THEME["surface2"]
        self._btn_sort._hover = THEME["accent_dk"] if on else THEME["border"]
        self._refresh_tree()
        if on:
            merged = self._merged_rows()
            dup = sum(1 for r in merged if r["n_merge"] > 1)
            self._status_var.set(
                f"已依頻率排序：{len(self._record_history)} 筆 → {len(merged)} 個頻率"
                f"（其中 {dup} 個頻率有重複量測，已取平均）")
        else:
            self._status_var.set("已切回原始記錄順序")

    def _refresh_tree(self) -> None:
        """重建歷史表格（排序切換或新增記錄後呼叫）。"""
        for item in self._hist_tree.get_children():
            self._hist_tree.delete(item)
        for row in self._display_rows():
            tags = []
            if row.get("fine"):
                tags.append("fine")
            if row.get("n_merge", 1) > 1:
                tags.append("merged")
            tf = row.get("target_freq")
            self._hist_tree.insert("", "end", tags=tuple(tags), values=(
                row.get("time", ""),
                f"{tf:.1f}" if tf is not None else "—",
                _fmt(row.get("top_freq"), 1),
                _fmt(row.get("db"), 1),
                _fmt(row.get("r"), 2),
                _fmt(row.get("idc"), 2),
                row.get("n_merge", 1),
            ))
        kids = self._hist_tree.get_children()
        if kids and not self._sort_by_freq.get():
            self._hist_tree.see(kids[-1])

    @staticmethod
    def _elec_summary(row: dict) -> str:
        parts = []
        if row.get("r") is not None:
            parts.append(f"R {row['r']:.2f} Ω")
        if row.get("idc") is not None:
            parts.append(f"I {row['idc']:.2f} mA")
        if row.get("dp") is not None:
            parts.append(f"ΔP {row['dp']:+.1f} mW")
        return ("｜" + "｜".join(parts)) if parts else ""

    def _record_peaks(self, elec: dict | None = None) -> None:
        """記錄目前峰值＋電氣讀值。elec 由掃頻執行緒先取好時會帶入。"""
        if not self._current_peaks:
            # 沒偵測到峰就退而取 ROI 內最高點
            if hasattr(self, "_current_freq_axis") and len(self._current_magnitude):
                fmin, fmax = self._freq_min_var.get(), self._freq_max_var.get()
                m = (self._current_freq_axis >= fmin) & (self._current_freq_axis <= fmax)
                f_roi, m_roi = self._current_freq_axis[m], self._current_magnitude[m]
                if len(m_roi):
                    i = int(np.argmax(m_roi))
                    self._current_peaks = [{"freq": float(f_roi[i]),
                                            "amplitude": float(m_roi[i])}]
        if not self._current_peaks:
            return

        ts = time.strftime("%H:%M:%S")
        top = self._current_peaks[0]["freq"]
        row = self._build_row(ts, top, self._current_db,
                              elec if elec is not None else self._latest_elec)
        self._record_history.append(row)
        self._refresh_tree()
        self._redraw_sweep_curve()
        self._status_var.set(
            f"已記錄 {ts}｜主峰 {top:.1f} Hz｜{self._current_db:.1f} dBSPL"
            f"{self._elec_summary(row)}")

    def _clear_history(self) -> None:
        if not self._record_history:
            return
        if messagebox.askyesno("確認", "確定要清除所有記錄嗎？"):
            self._record_history.clear()
            self._refresh_tree()
            self._sweep_db_line.set_data([], [])
            self._sweep_e_line.set_data([], [])
            self._fine_marker.set_alpha(0.0)
            self._canvas2.draw_idle()
            self._status_var.set("記錄已清除")

    # ══════════════════════════════════════════════════════════════════════
    #  掃頻曲線
    # ══════════════════════════════════════════════════════════════════════

    def _redraw_sweep_curve(self) -> None:
        """曲線一律用合併後的資料，細掃點會自動插進對應位置。"""
        pts = [r for r in self._merged_rows() if r.get("target_freq") is not None]
        if not pts:
            return
        f = [r["target_freq"] for r in pts]
        y = [r["db"] for r in pts]
        self._sweep_db_line.set_data(f, y)

        key, label = Y2_CHOICES[self._y2_var.get()]
        self._ax_e.set_ylabel(label, color=THEME["electric"], fontsize=9)
        e = [(r["target_freq"], r[key]) for r in pts if r.get(key) is not None]
        if e:
            ys = [p[1] for p in e]
            self._sweep_e_line.set_data([p[0] for p in e], ys)
            pad = max((max(ys) - min(ys)) * 0.15, abs(max(ys)) * 0.02, 1e-3)
            self._ax_e.set_ylim(min(ys) - pad, max(ys) + pad)
        else:
            self._sweep_e_line.set_data([], [])

        self._ax_db.set_xlim(min(f) - 1, max(f) + 1)
        pad = max((max(y) - min(y)) * 0.15, 1.0)
        self._ax_db.set_ylim(min(y) - pad, max(y) + pad)
        self._fig2.tight_layout(pad=1.4)
        self._canvas2.draw_idle()

    def _on_sweep_click(self, event) -> None:
        """★ 點擊掃頻曲線上的峰 → 以 1 Hz 細掃該點 ±10 Hz。"""
        if event.inaxes not in (self._ax_db, self._ax_e) or event.xdata is None:
            return
        if self._is_sweeping:
            self._status_var.set("掃頻進行中，請先停止再點擊細掃。")
            return

        f0 = float(event.xdata)
        # 吸附到最近的已記錄頻率，避免點歪
        recorded = [r["target_freq"] for r in self._merged_rows()
                    if r.get("target_freq") is not None]
        snapped = False
        if recorded:
            near = min(recorded, key=lambda x: abs(x - f0))
            if abs(near - f0) <= SNAP_HZ:
                f0, snapped = near, True
        f0 = round(f0)

        lo, hi = f0 - FINE_SPAN, f0 + FINE_SPAN
        n = len(build_freq_list(lo, hi, FINE_STEP))
        try:
            dwell = float(self._sweep_dwell_var.get())
        except Exception:
            dwell = 3.0

        if not messagebox.askyesno(
                "細掃確認",
                f"以 {FINE_STEP} Hz 細掃 {lo}–{hi} Hz（{n} 點）？\n\n"
                f"{'已吸附到最近的記錄點：' if snapped else '使用點擊位置：'}{f0} Hz\n"
                f"每點停留 {dwell:.1f} 秒，預估約 {n * dwell / 60:.1f} 分鐘。\n\n"
                "結果會併入現有記錄；按「依頻率排序」可看到細掃點排進對應位置。"):
            return

        self._fine_marker.set_xdata([f0, f0])
        self._fine_marker.set_alpha(0.8)
        self._canvas2.draw_idle()
        self._start_sweep(lo, hi, FINE_STEP, fine=True)

    # ══════════════════════════════════════════════════════════════════════
    #  自動掃頻
    # ══════════════════════════════════════════════════════════════════════

    def _toggle_sweep(self) -> None:
        self._stop_sweep() if self._is_sweeping else self._start_sweep()

    def _start_sweep(self, start_f=None, end_f=None, step=None, fine=False) -> None:
        """啟動掃頻。參數省略時讀取面板設定；start > end 會自動反向遞減。"""
        if self._is_sweeping:
            return
        if not self._is_running:
            messagebox.showinfo("提示", "請先點擊上方「開始」開啟麥克風，再執行掃頻。")
            return

        if start_f is None:
            start_f = self._sweep_start_var.get()
            end_f = self._sweep_end_var.get()
            step = self._sweep_step_var.get()

        try:
            dwell_ms = float(self._sweep_dwell_var.get()) * 1000.0
            win_ms = float(self._elec_window_var.get())
        except Exception:
            dwell_ms, win_ms = 3000.0, 500.0
        if self._meter and dwell_ms < win_ms + 500:
            messagebox.showinfo(
                "提示",
                f"停留時間（{dwell_ms / 1000:.1f} 秒）太短。\n"
                f"直流平均窗是 {win_ms:.0f} ms，停留至少要 "
                f"{(win_ms + 500) / 1000:.1f} 秒才抓得到同步資料。")
            return

        freqs = build_freq_list(start_f, end_f, step)
        if not freqs:
            messagebox.showinfo("提示", "掃頻範圍無效。")
            return

        self._is_sweeping = True
        self._sweep_is_fine = fine
        self._btn_sweep.config(text="停止掃描", bg=THEME["danger"], fg="#fff")
        self._btn_sweep._base = THEME["danger"]
        self._btn_sweep._hover = THEME["danger_dk"]
        self._set_pill("細掃中" if fine else "掃頻中", THEME["peak"])

        if self._generator:
            self._generator.stop_tone()
        self._generator = AudioGenerator(
            output_device_index=self._get_selected_out_device_index())

        self._sweep_thread = threading.Thread(
            target=self._sweep_worker_loop, args=(freqs, fine), daemon=True)
        self._sweep_thread.start()

    def _stop_sweep(self) -> None:
        self._is_sweeping = False
        self._sweep_is_fine = False
        self._btn_sweep.config(text="開始掃描", bg=THEME["accent"], fg="#fff")
        self._btn_sweep._base = THEME["accent"]
        self._btn_sweep._hover = THEME["accent_dk"]
        if self._generator:
            self._generator.stop_tone()
        self._current_target_freq = None
        self._set_pill("錄音中" if self._is_running else "待機中",
                       THEME["ok"] if self._is_running else THEME["text_dim"])
        self._status_var.set("掃頻已停止")

    def _sweep_worker_loop(self, freqs: list[float], fine: bool) -> None:
        """
        背景執行緒：依序播放每個頻率，等穩定後截取一次資料。

        時序（停留 T 秒）：
            0        開始播放
            0~T-1    等機械與量測穩定，同時等一筆「起始晚於發聲」的直流資料
            T-1      截取並寫入記錄
            T        下一個頻率
        """
        total = len(freqs)
        tag = "細掃" if fine else "掃頻"
        for i, f in enumerate(freqs, 1):
            if not self._is_sweeping:
                break
            self._current_target_freq = float(f)
            self._status_var.set(f"[{tag} {i}/{total}] 播放 {f:g} Hz，等待數據穩定…")

            tone_start = time.time()
            if self._generator:
                self._generator.start_tone(f)

            try:
                dwell = max(float(self._sweep_dwell_var.get()), 0.5)
            except Exception:
                dwell = 3.0
            settle = max(dwell - 1.0, 0.3)

            elec = None
            if self._meter:
                elec = self._wait_fresh_elec(tone_start, settle)
                remain = settle - (time.time() - tone_start)
                if remain > 0:
                    time.sleep(remain)
            else:
                time.sleep(settle)

            if not self._is_sweeping:
                break
            self.root.after(0, lambda e=elec: self._record_peaks(e))

            rest = dwell - (time.time() - tone_start)
            if rest > 0:
                time.sleep(rest)

        # 細掃不自動存檔（避免每次點擊都產生一個檔案），只在狀態列提示
        if fine:
            self.root.after(0, lambda: self._status_var.set(
                f"細掃完成（{total} 點）— 按「依頻率排序」可看到細掃點排進對應位置"))
        elif self._sweep_auto_save.get():
            self.root.after(0, self._auto_save_sweep_csv)
        self.root.after(0, self._stop_sweep)

    # ══════════════════════════════════════════════════════════════════════
    #  CSV
    # ══════════════════════════════════════════════════════════════════════

    def _write_csv(self, path: str) -> None:
        """依目前檢視模式輸出（排序模式下會是合併平均後的資料）。"""
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for row in self._display_rows():
                peaks = [f"{p['freq']:.1f}" for p in row.get("peaks", [])[:5]]
                peaks += [""] * (5 - len(peaks))
                tf = row.get("target_freq")
                w.writerow([
                    row.get("time", ""),
                    f"{tf:.1f}" if tf is not None else "N/A",
                    _fmt(row.get("top_freq"), 2), _fmt(row.get("db"), 1),
                    _fmt(row.get("r"), 4), _fmt(row.get("vdc"), 4),
                    _fmt(row.get("idc"), 3), _fmt(row.get("p"), 2),
                    _fmt(row.get("di"), 3), _fmt(row.get("dp"), 2),
                    _fmt(row.get("iac"), 3), _fmt(row.get("ripple"), 2),
                    _fmt(row.get("fs"), 0), _fmt(row.get("off"), 3),
                    row.get("n_merge", 1), *peaks,
                ])

    def _export_csv(self) -> None:
        if not self._record_history:
            messagebox.showinfo("提示", "尚無記錄資料，請先記錄一些峰值。")
            return
        from tkinter.filedialog import asksaveasfilename
        mode = "sorted" if self._sort_by_freq.get() else "raw"
        path = asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV 檔案", "*.csv"), ("所有檔案", "*.*")],
            title="儲存記錄",
            initialfile=f"chladni_{mode}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        if not path:
            return
        try:
            self._write_csv(path)
            n = len(self._display_rows())
            messagebox.showinfo(
                "匯出成功",
                f"已儲存至：\n{path}\n\n"
                f"模式："
                f"{'依頻率排序（重複取平均）' if self._sort_by_freq.get() else '原始順序'}"
                f"　共 {n} 列")
        except Exception as exc:
            messagebox.showerror("匯出失敗", f"無法寫入檔案：\n{exc}")
            logger.error("CSV 匯出失敗：%s", exc)

    def _on_sweep_auto_save_toggle(self) -> None:
        if self._sweep_auto_save.get() and self._sweep_save_dir is None:
            self._choose_sweep_save_dir()

    def _choose_sweep_save_dir(self) -> None:
        from tkinter.filedialog import askdirectory
        chosen = askdirectory(title="選擇掃頻 CSV 自動儲存資料夾", mustexist=True)
        if chosen:
            self._sweep_save_dir = chosen
            self._sweep_save_dir_label.config(
                text=chosen if len(chosen) <= 26 else "…" + chosen[-23:])
            logger.info("掃頻自動存檔目錄：%s", chosen)

    def _auto_save_sweep_csv(self) -> None:
        if not self._record_history:
            return
        save_dir = self._sweep_save_dir or os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(save_dir, f"sweep_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            self._write_csv(path)
            self._status_var.set(f"[自動存檔] 掃頻結果已儲存至：{path}")
            messagebox.showinfo("自動存檔完成", f"掃頻 CSV 已儲存至：\n{path}")
        except Exception as exc:
            messagebox.showerror("自動存檔失敗", f"無法寫入檔案：\n{exc}")
            logger.error("掃頻 CSV 自動存檔失敗：%s", exc)

    # ══════════════════════════════════════════════════════════════════════

    def _on_close(self) -> None:
        self._stop_sweep()
        self._stop()
        self._disconnect_meter()
        self.root.destroy()


# ── ttk 樣式 ─────────────────────────────────────────────────────────────────

def apply_ttk_style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("Treeview", background=THEME["surface2"], foreground=THEME["text"],
                    fieldbackground=THEME["surface2"], rowheight=23,
                    borderwidth=0, font=fnt(9))
    style.configure("Treeview.Heading", background=THEME["surface"],
                    foreground=THEME["text_dim"], relief="flat", font=fnt(8, True))
    style.map("Treeview",
              background=[("selected", THEME["accent"])],
              foreground=[("selected", "#ffffff")])
    style.map("Treeview.Heading", background=[("active", THEME["border"])])

    style.configure("TNotebook", background=THEME["surface"], borderwidth=0)
    style.configure("TNotebook.Tab", background=THEME["surface"],
                    foreground=THEME["text_dim"], padding=(18, 7),
                    borderwidth=0, font=fnt(10))
    style.map("TNotebook.Tab",
              background=[("selected", THEME["surface2"])],
              foreground=[("selected", THEME["text"])])

    style.configure("TCombobox", fieldbackground=THEME["surface2"],
                    background=THEME["surface2"], foreground=THEME["text"],
                    arrowcolor=THEME["text_dim"], borderwidth=0,
                    selectbackground=THEME["surface2"],
                    selectforeground=THEME["text"])
    style.map("TCombobox",
              fieldbackground=[("readonly", THEME["surface2"])],
              foreground=[("readonly", THEME["text"])])
    root.option_add("*TCombobox*Listbox.background", THEME["surface2"])
    root.option_add("*TCombobox*Listbox.foreground", THEME["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", THEME["accent"])

    style.configure("Vertical.TScrollbar", background=THEME["border"],
                    troughcolor=THEME["surface"], borderwidth=0,
                    arrowcolor=THEME["text_dim"])


def main() -> None:
    root = tk.Tk()
    init_fonts(root)
    apply_ttk_style(root)
    ChladniAnalyzerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()