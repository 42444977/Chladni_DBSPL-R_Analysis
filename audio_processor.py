"""
audio_processor.py — 音訊處理核心模組
負責麥克風輸入、FFT 頻譜計算與共振峰值偵測
"""

import logging
import numpy as np
import pyaudio
from scipy.signal import find_peaks

logger = logging.getLogger(__name__)

# ── 預設常數 ──────────────────────────────────────────────────────────────────
DEFAULT_SAMPLE_RATE = 44100   # 取樣率（Hz）
# 2026-04-20 更新：chunk 由 4096 → 16384
#   舊設定 Δf = 44100/4096 ≈ 10.77 Hz：實驗上觀察到主峰頻率全被量子化到 10.77 Hz 的
#   格點（75.37, 86.13, 96.9, 107.67, ... Hz），低頻共振頻率的相對誤差可達 5% 以上，
#   且驅動頻率永遠落不在真實共振點上 → 克拉尼圖案模糊。
#   新設定 Δf = 44100/16384 ≈ 2.69 Hz；再配合下方 detect_peaks 的拋物線內插
#   （parabolic interpolation），有效頻率精度約 0.1–0.3 Hz。
#   代價：每幀延遲 16384/44100 ≈ 372 ms，對撒沙掃頻足夠即時。
DEFAULT_CHUNK_SIZE  = 16384   # 每次讀取的樣本數（越大頻率解析度越高，但延遲也越高）
DEFAULT_CHANNELS    = 1       # 單聲道
REFERENCE_PRESSURE  = 20e-6   # 聲壓參考值（20μPa），用於換算 dBSPL


class AudioProcessor:
    """
    封裝 PyAudio 麥克風輸入與 FFT 分析邏輯。
    使用者透過 read_frame() 取得每幀的頻率與振幅資料。
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        device_index: int | None = None,
    ) -> None:
        self.sample_rate  = sample_rate
        self.chunk_size   = chunk_size
        self.device_index = device_index

        self._pa     = pyaudio.PyAudio()
        self._stream = None

        # 預先計算 FFT 頻率軸（只取正頻率部分）
        self.freq_axis = np.fft.rfftfreq(chunk_size, d=1.0 / sample_rate)

    # ── 裝置管理 ─────────────────────────────────────────────────────────────

    def list_input_devices(self) -> list[dict]:
        """
        回傳所有可用輸入裝置的清單，供 UI 顯示選擇。
        """
        devices = []
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                devices.append({"index": i, "name": info["name"]})
        return devices

    def list_output_devices(self) -> list[dict]:
        """
        回傳所有可用輸出裝置的清單，供 UI 顯示選擇。
        """
        devices = []
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info["maxOutputChannels"] > 0:
                devices.append({"index": i, "name": info["name"]})
        return devices

    def open_stream(self) -> None:
        """
        開啟麥克風串流。若已開啟則先關閉再重新開啟。
        """
        self.close_stream()
        try:
            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=DEFAULT_CHANNELS,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.chunk_size,
                input_device_index=self.device_index,
            )
            logger.info("麥克風串流已開啟，裝置索引：%s", self.device_index)
        except Exception as exc:
            logger.error("無法開啟麥克風串流：%s", exc)
            raise

    def close_stream(self) -> None:
        """關閉並釋放麥克風串流資源。"""
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
            logger.info("麥克風串流已關閉")

    def __del__(self) -> None:
        self.close_stream()
        self._pa.terminate()

    # ── 音訊讀取與分析 ────────────────────────────────────────────────────────

    def read_frame(self) -> tuple[np.ndarray, np.ndarray, float]:
        """
        從麥克風讀取一幀資料並進行 FFT 分析。

        Returns:
            freq_axis  : 頻率軸陣列（Hz）
            magnitude  : 對應每個頻率的振幅（線性，已加 Hann 窗）
            db_level   : 本幀聲音的 dBSPL 整體音量
        """
        if self._stream is None:
            raise RuntimeError("串流尚未開啟，請先呼叫 open_stream()")

        # 讀取原始 PCM 資料並轉換為 float32 陣列
        raw = self._stream.read(self.chunk_size, exception_on_overflow=False)
        samples = np.frombuffer(raw, dtype=np.float32)

        # 套用 Hann 窗以減少頻譜洩漏（spectral leakage）
        window   = np.hanning(len(samples))
        windowed = samples * window

        # FFT → 取正頻率部分 → 計算振幅（歸一化）
        spectrum  = np.fft.rfft(windowed)
        magnitude = np.abs(spectrum) / self.chunk_size

        # 計算整體 RMS → dBSPL
        rms      = np.sqrt(np.mean(samples ** 2))
        db_level = 20 * np.log10(rms / REFERENCE_PRESSURE + 1e-12)

        return self.freq_axis, magnitude, db_level

    # ── 峰值偵測 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parabolic_interp(
        freq_axis: np.ndarray,
        magnitude: np.ndarray,
        k: int,
    ) -> tuple[float, float]:
        """
        用三點拋物線內插（log-magnitude domain）求 FFT 主峰的 sub-bin 頻率與振幅。

        原理：在對數振幅下，真實峰形狀近似拋物線。
        設相鄰三個 bin 的 log-amp 分別為 α (k-1)、β (k)、γ (k+1)，
        峰中心相對於中心 bin 的位移 δ = 0.5 * (α - γ) / (α - 2β + γ)  (-0.5 ≤ δ ≤ 0.5)
        插值頻率 = f[k] + δ * Δf
        插值振幅 = β - 0.25 * (α - γ) * δ

        Args:
            freq_axis  : FFT 頻率軸（完整，不是 ROI 切片）
            magnitude  : 線性振幅
            k          : 主峰 bin 索引（在 freq_axis 內的絕對索引）

        Returns:
            (f_peak, amp_peak)：sub-bin 內插後的頻率（Hz）與線性振幅
        """
        if k <= 0 or k >= len(freq_axis) - 1:
            return float(freq_axis[k]), float(magnitude[k])

        # 用 log 振幅更接近拋物線；小量避免 log(0)
        eps = 1e-20
        a = np.log(magnitude[k - 1] + eps)
        b = np.log(magnitude[k    ] + eps)
        c = np.log(magnitude[k + 1] + eps)

        denom = a - 2.0 * b + c
        if abs(denom) < 1e-12:
            return float(freq_axis[k]), float(magnitude[k])

        delta = 0.5 * (a - c) / denom
        # 保護：理論上 |delta| ≤ 0.5；若超出表示 k 不是真正的局部極大
        delta = float(np.clip(delta, -0.5, 0.5))

        df      = freq_axis[1] - freq_axis[0]
        f_peak  = float(freq_axis[k]) + delta * df
        amp_log = b - 0.25 * (a - c) * delta
        amp_peak = float(np.exp(amp_log))
        return f_peak, amp_peak

    @staticmethod
    def detect_peaks(
        freq_axis: np.ndarray,
        magnitude: np.ndarray,
        min_freq: float = 20.0,
        max_freq: float = 4000.0,
        prominence: float = 0.005,
        top_n: int = 10,
        interpolate: bool = True,
    ) -> list[dict]:
        """
        在指定頻率範圍內偵測振幅峰值，回傳由強到弱排序的前 N 個峰值。

        2026-04-20：新增 sub-bin 拋物線內插，解決 FFT bin 量子化誤差
        （Δf=2.69 Hz 時，內插後頻率精度約 0.1–0.3 Hz）。

        Args:
            freq_axis    : AudioProcessor.freq_axis
            magnitude    : AudioProcessor.read_frame() 回傳的 magnitude
            min_freq     : 偵測下限（Hz）
            max_freq     : 偵測上限（Hz）
            prominence   : scipy find_peaks 的 prominence 門檻（越高越嚴格）
            top_n        : 最多回傳幾個峰值
            interpolate  : 是否對每個峰做拋物線 sub-bin 內插（預設 True）

        Returns:
            list of dict，每個 dict 含 'freq'（Hz，已內插）、'amplitude'（線性，已內插）
            以及 'freq_bin'（內插前的 bin 中心頻率，用於除錯比對）
        """
        # 只分析指定頻率範圍
        mask = (freq_axis >= min_freq) & (freq_axis <= max_freq)
        roi_indices = np.where(mask)[0]
        if len(roi_indices) == 0:
            return []

        freq_roi = freq_axis[mask]
        mag_roi  = magnitude[mask]

        # NOTE: prominence 防止把噪音小起伏誤判為峰值
        peak_local, _ = find_peaks(mag_roi, prominence=prominence)

        if len(peak_local) == 0:
            return []

        # 依振幅由大到小排序，取前 N 個
        sorted_local = peak_local[np.argsort(mag_roi[peak_local])[::-1]][:top_n]

        results: list[dict] = []
        for i_local in sorted_local:
            f_bin   = float(freq_roi[i_local])
            a_bin   = float(mag_roi[i_local])
            if interpolate:
                # 轉回完整 freq_axis 的絕對 index 做內插（需要左右相鄰 bin）
                i_abs = int(roi_indices[i_local])
                f_int, a_int = AudioProcessor._parabolic_interp(freq_axis, magnitude, i_abs)
            else:
                f_int, a_int = f_bin, a_bin

            results.append({
                "freq":      f_int,
                "amplitude": a_int,
                "freq_bin":  f_bin,
            })

        return results


class AudioGenerator:
    """
    非同步產生純音（正弦波）的音訊播放器。
    使用 PyAudio 的 callback 模式，不阻塞主執行緒。
    """

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE, output_device_index: int | None = None):
        self.sample_rate = sample_rate
        self.output_device_index = output_device_index
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self._is_playing = False
        self._frequency = 440.0
        self._amplitude = 0.5
        self._phase = 0.0

    def start_tone(self, frequency: float, amplitude: float = 0.5) -> None:
        """
        開始播放指定頻率的純音。
        如果已經在播放，則無縫平滑過渡到新頻率。
        """
        self._frequency = frequency
        self._amplitude = max(0.0, min(1.0, amplitude))

        if self._is_playing and self._stream and self._stream.is_active():
            return

        # 開啟 callback 串流
        self._is_playing = True
        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=self.sample_rate,
            output=True,
            output_device_index=self.output_device_index,
            stream_callback=self._audio_callback
        )
        self._stream.start_stream()
        logger.info("開始播放純音：%.1f Hz", frequency)

    def stop_tone(self) -> None:
        """停止播放。"""
        self._is_playing = False
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        logger.info("停止播放純音")

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """PyAudio 供檔回呼函數。"""
        if not self._is_playing:
            return (np.zeros(frame_count, dtype=np.float32).tobytes(), pyaudio.paComplete)

        # 產生正弦波，並保持相位連續
        t = (np.arange(frame_count) / self.sample_rate)
        wave = self._amplitude * np.sin(2 * np.pi * self._frequency * t + self._phase)
        
        # 更新相位並取模以避免數值溢出
        self._phase += 2 * np.pi * self._frequency * (frame_count / self.sample_rate)
        self._phase %= 2 * np.pi

        return (wave.astype(np.float32).tobytes(), pyaudio.paContinue)

    def __del__(self) -> None:
        self.stop_tone()
        self._pa.terminate()
