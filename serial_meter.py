"""
serial_meter.py — 與 Arduino + INA219 韌體溝通的序列埠模組

搭配 ina219_meter.ino（v2.0，直流量測版）使用。負責：
  - 列舉可用序列埠
  - 開啟／關閉連線（含 AVR 自動重置等待）
  - 送出 MEAS / ZERO 指令並把回傳的 `OK k=v ...` 解析成 dict
  - 執行緒安全（GUI 執行緒與量測執行緒可共用同一個物件）

依賴：pip install pyserial
"""

from __future__ import annotations

import logging
import math
import threading
import time

try:
    import serial
    from serial.tools import list_ports
    _SERIAL_OK = True
except ImportError:  # 沒裝 pyserial 時讓主程式還能跑，只是無法量電阻
    serial = None       # type: ignore
    list_ports = None   # type: ignore
    _SERIAL_OK = False

logger = logging.getLogger(__name__)

# 韌體識別字串，用來確認接到的是正確的裝置
FIRMWARE_ID = "CHLADNI_INA219"

# 漣波比超過此值（%）就認為直流平均不可靠，GUI 會示警
RIPPLE_WARN_PCT = 20.0


def serial_available() -> bool:
    """pyserial 是否可用。"""
    return _SERIAL_OK


def list_serial_ports() -> list[dict]:
    """
    列出所有序列埠。

    Returns:
        [{"device": "COM3", "desc": "COM3 — Arduino Uno"}, ...]
    """
    if not _SERIAL_OK:
        return []
    return [
        {"device": p.device, "desc": f"{p.device} — {p.description}"}
        for p in list_ports.comports()
    ]


def _parse_kv_line(line: str) -> dict:
    """
    把 `OK n=940 fs=1880.0 r=48.5312 vdc=5.0123 ... tconv=532` 解析成 dict。
    數值欄位轉成 float，'nan' 轉成 None，其餘保留字串。
    """
    result: dict = {}
    parts = line.strip().split()
    if not parts:
        return result
    result["_status"] = parts[0]          # OK / ERR
    for token in parts[1:]:
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        if v.lower() in ("nan", "inf", "-inf"):
            result[k] = None
            continue
        try:
            f = float(v)
            result[k] = None if math.isnan(f) else f
        except ValueError:
            result[k] = v
    return result


class MeterError(RuntimeError):
    """序列埠量測相關錯誤。"""


class InaMeter:
    """
    Arduino + INA219 直流電阻量測儀的 Python 介面。

    典型用法：
        m = InaMeter("COM3")
        m.open()
        print(m.measure(500))   # {'r': 48.53, 'vdc': 5.012, 'idc': 103.3, ...}
        m.close()
    """

    def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0) -> None:
        if not _SERIAL_OK:
            raise MeterError("未安裝 pyserial，請先執行：pip install pyserial")
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser: "serial.Serial | None" = None
        self._lock = threading.Lock()
        self.info: dict = {}

    # ── 連線管理 ─────────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self) -> dict:
        """
        開啟序列埠並握手確認韌體。

        Returns:
            韌體資訊 dict（含 id / shunt / gain / bits / tconv / off）
        Raises:
            MeterError: 開啟失敗或韌體不符
        """
        self.close()
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        except Exception as exc:
            raise MeterError(f"無法開啟 {self.port}：{exc}") from exc

        # NOTE: Uno / Nano 在開埠時會因 DTR 自動重置，需等 bootloader 跑完
        time.sleep(2.0)
        self._ser.reset_input_buffer()

        info = self._command("ID", timeout=3.0)
        if info.get("id") != FIRMWARE_ID:
            got = info.get("id", "(無回應)")
            self.close()
            raise MeterError(f"{self.port} 回應的韌體是「{got}」，不是 {FIRMWARE_ID}")
        self.info = info
        logger.info("已連線 %s：%s", self.port, info)
        return info

    def close(self) -> None:
        """關閉序列埠。"""
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    # ── 指令 ────────────────────────────────────────────────────────────────

    def _command(self, cmd: str, timeout: float | None = None) -> dict:
        """送出一行指令並讀回一行結果（執行緒安全）。"""
        if self._ser is None or not self._ser.is_open:
            raise MeterError("序列埠尚未開啟")

        with self._lock:
            old_timeout = self._ser.timeout
            if timeout is not None:
                self._ser.timeout = timeout
            try:
                self._ser.reset_input_buffer()
                self._ser.write((cmd + "\n").encode("ascii"))
                self._ser.flush()
                raw = self._ser.readline().decode("ascii", errors="replace").strip()
            finally:
                self._ser.timeout = old_timeout

        if not raw:
            raise MeterError(f"指令「{cmd}」逾時無回應")
        parsed = _parse_kv_line(raw)
        if parsed.get("_status") != "OK":
            raise MeterError(f"韌體回報錯誤：{raw}")
        return parsed

    def measure(self, window_ms: int = 500) -> dict:
        """
        量測一次直流。

        Args:
            window_ms: 平均時間窗（毫秒）。越長越能濾掉電源漣波，建議 300–1000 ms。

        Returns:
            dict，欄位如下（缺值為 None）：
              n       取樣點數
              fs      實際取樣率 (Hz)
              r       電阻 R = Vdc / Idc (Ω)
              vdc     直流電壓 (V)
              idc     直流電流 (mA)，已扣除歸零偏移
              p       功率 (mW)
              iac     電流交流分量 RMS (mA) — 漣波
              vac     電壓交流分量 RMS (V)
              ripple  漣波比 iac/|idc| (%)
              ipk     電流瞬時峰值 (mA)
              off     目前套用的歸零偏移 (mA)
              tconv   ADC 轉換時間 (µs)
        """
        return self._command(f"MEAS {int(window_ms)}",
                             timeout=window_ms / 1000.0 + 2.0)

    def zero(self, window_ms: int = 500) -> dict:
        """
        歸零：把目前讀到的電流當成偏移扣掉。

        ★ 執行前務必先斷開負載（讓分流電阻上沒有真實電流），
          否則會把真實電流當成偏移扣掉。

        Returns:
            韌體資訊 dict（含新的 off 值，單位 mA）
        """
        info = self._command(f"ZERO {int(window_ms)}",
                             timeout=window_ms / 1000.0 + 3.0)
        self.info = info
        return info

    def zero_clear(self) -> dict:
        """清除歸零偏移。"""
        info = self._command("ZERO CLEAR", timeout=3.0)
        self.info = info
        return info

    def set_param(self, key: str, value) -> dict:
        """
        調整韌體參數。

        key 可為 bits(9~12)、gain(40/80/160/320)、shunt(Ω)、window(ms)。
        直流量測建議 bits=12（轉換窗最長、解析度最好）。
        """
        info = self._command(f"SET {key} {value}", timeout=3.0)
        self.info = info
        return info


# ── 獨立測試：python serial_meter.py COM3 ────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("可用序列埠：")
        for p in list_serial_ports():
            print("  ", p["desc"])
        print("\n用法：python serial_meter.py <PORT> [視窗ms]")
        sys.exit(0)

    meter = InaMeter(sys.argv[1])
    win = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    print("連線中…")
    print("韌體：", meter.open())
    try:
        while True:
            d = meter.measure(win)
            r = d.get("r")
            rip = d.get("ripple")
            print(
                f"R = {f'{r:8.3f} Ω' if r is not None else '    --   '}"
                f"   V = {d.get('vdc'):6.3f} V"
                f"   I = {d.get('idc'):8.2f} mA"
                f"   P = {d.get('p'):8.1f} mW"
                f"   漣波 {f'{rip:.1f}%' if rip is not None else '--'}"
                f"   fs = {d.get('fs'):.0f} Hz"
            )
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        meter.close()
        print("\n已關閉連線")