# Chladni dBSPL-R Analysis

克拉尼圖形音訊分析儀：以麥克風量測聲壓（dBSPL），同時透過 Arduino + INA219 量測直流電阻 R，進行雙通道掃頻分析。

## 功能

- 即時 FFT 頻譜顯示與共振峰值自動偵測（拋物線內插提高頻率精度）
- Arduino + INA219 直流量測：電阻、電壓、電流、功率、漣波
- 自動掃頻（由低到高或由高到低）
- 在掃頻曲線上點擊峰值，自動以 1 Hz 細掃該點 ±10 Hz
- 歷史記錄依頻率排序，重複頻率自動取平均
- 匯出 CSV

## 檔案結構

| 檔案 | 說明 |
|------|------|
| `main.py` | Tkinter GUI 主程式與 Matplotlib 視覺化 |
| `audio_processor.py` | 麥克風輸入、訊號產生、FFT 與峰值偵測 |
| `serial_meter.py` | 與 Arduino + INA219 韌體的序列埠通訊 |
| `INA219/INA219meter/INA219meter.ino` | Arduino 直流量測韌體（需安裝 Adafruit INA219 函式庫） |
| `INA219/INA219.ino` | INA219 簡易測試程式 |

## 安裝

需要 Python 3.10 以上。

```bash
pip install numpy scipy matplotlib pyaudio pyserial
```

`pyserial` 為選用；未安裝時仍可進行音訊分析，但無法量測電阻。

## 使用

```bash
python main.py
```

1. 選擇輸入（麥克風）與輸出（喇叭）裝置
2. 若要量測電阻，選擇 Arduino 序列埠並連線
3. 設定頻率範圍後開始掃頻，或在即時頻譜中觀察共振峰
4. 完成後匯出 CSV
