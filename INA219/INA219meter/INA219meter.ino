/* ============================================================================
 * ina219_meter.ino — 克拉尼實驗：INA219 直流電阻／功率量測韌體
 * ----------------------------------------------------------------------------
 * 2026-09-27 改版：電路改為 INA219 串在「變壓模組（激振器驅動）的輸入側」，
 *   流過分流電阻的是直流，因此韌體全面改為直流量測：
 *
 *       R    = Vbus / Idc              模組輸入等效電阻 (Ω)
 *       P    = Vbus × Idc              模組輸入功率 (mW)
 *       漣波 = Iac_rms / |Idc| × 100%  監控音訊在電源線上的殘留
 *
 * 和舊版（交流阻抗）的差別：
 *   - Vbus 現在量的是直流電源軌（幾 V 到十幾 V），完全落在 INA219 的
 *     0～26 V 單極性範圍內，舊版負半週被截掉的問題不存在了。
 *   - 直流平均值不受 ADC 積分窗影響（平均一個常數還是常數），
 *     所以不需要 sinc 反補償，而且應該用「最長」的轉換時間換取最佳解析度
 *     → 預設改為 12-bit（532 µs）。
 *   - 仍然會算出交流分量（iac/vac）當作漣波指標：變壓模組與 D 類功放會在
 *     電源線上造成音訊頻率的脈動電流，漣波太大代表直流平均不可靠。
 *
 * 另外提供 ZERO 歸零指令：斷開負載後執行，可扣掉分流路徑的直流偏移。
 *
 * ── 接線（高側量測，Adafruit INA219 分流電阻 0.1 Ω）──────────────────────────
 *   直流電源 +  ──► VIN+
 *                   VIN− ──► 變壓模組 / 激振器驅動板 的 V+
 *   模組 GND    ──► 直流電源 −（且與 Arduino GND 共地）
 *
 *   INA219 VCC → 5V (或 3.3V)     GND → Arduino GND
 *          SDA → A4 (Uno/Nano) / 20 (Mega) / 21 (ESP32)
 *          SCL → A5 (Uno/Nano) / 21 (Mega) / 22 (ESP32)
 *
 * ── 序列埠指令 ──────────────────────────────────────────────────────────────
 *   ID                回報韌體資訊
 *   MEAS [ms]         量測一次，回一行 OK ...
 *   STREAM [ms]       連續量測（送任意一行即停止）
 *   ZERO [ms]         歸零：以目前讀值當直流偏移（執行前請斷開負載）
 *   ZERO CLEAR        清除歸零偏移
 *   SET bits 9|10|11|12
 *   SET gain 40|80|160|320        分流電壓滿刻度 (mV)
 *   SET shunt 0.1                 分流電阻 (Ω)
 *   SET window 500                預設量測窗 (ms)
 * ==========================================================================*/

#include <Wire.h>

// ── INA219 暫存器位址 ───────────────────────────────────────────────────────
#define INA219_ADDR   0x40
#define REG_CONFIG    0x00
#define REG_SHUNT     0x01
#define REG_BUS       0x02

// ── 可由序列埠調整的參數 ────────────────────────────────────────────────────
uint8_t  g_addr        = INA219_ADDR;
float    g_shuntOhm    = 0.1f;   // 分流電阻（Adafruit 板為 0.1Ω）
uint8_t  g_gainSel     = 3;      // 0:±40mV 1:±80mV 2:±160mV 3:±320mV
// 直流量測要的是解析度，不是速度 → 預設用最長的轉換時間
uint8_t  g_bitsSel     = 3;      // 0:9bit(84us) 1:10bit(148us) 2:11bit(276us) 3:12bit(532us)
uint32_t g_defWindowMs = 500;    // 預設量測窗（直流平均越久越穩）
int32_t  g_offsetCnt   = 0;      // ZERO 歸零偏移（分流計數，LSB 10 µV）

// ADC 轉換時間（µs），索引同 g_bitsSel
const uint16_t CONV_US[4] = {84, 148, 276, 532};

// ── 量測結果 ────────────────────────────────────────────────────────────────
// NOTE: 這個 struct 必須定義在「第一個函式定義」之前。
//   Arduino IDE 會自動產生所有函式的原型並插在第一個函式定義的位置，
//   若 struct 放在後面，產生出來的 `static bool measure(uint32_t, Result&)`
//   會找不到型別 → error: 'Result' has not been declared
struct Result {
  uint32_t n;
  float fs;        // 實際取樣率 (Hz)
  float vdc;       // 直流電壓 (V)
  float idc;       // 直流電流 (mA)，已扣除歸零偏移
  float r;         // 電阻 R = vdc / idc (Ω)
  float p;         // 功率 P = vdc × idc (mW)
  float iac;       // 電流交流分量 RMS (mA) — 漣波
  float vac;       // 電壓交流分量 RMS (V)  — 漣波
  float ripple;    // iac / |idc| × 100 (%)
  float ipk;       // 電流瞬時峰值 (mA)
};

// ── 低階 I2C 讀寫 ───────────────────────────────────────────────────────────
static bool writeReg(uint8_t reg, uint16_t val) {
  Wire.beginTransmission(g_addr);
  Wire.write(reg);
  Wire.write((uint8_t)(val >> 8));
  Wire.write((uint8_t)(val & 0xFF));
  return Wire.endTransmission() == 0;
}

static bool readReg(uint8_t reg, uint16_t &out) {
  Wire.beginTransmission(g_addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;   // repeated start
  if (Wire.requestFrom((int)g_addr, 2) != 2) return false;
  uint8_t hi = Wire.read();
  uint8_t lo = Wire.read();
  out = ((uint16_t)hi << 8) | lo;
  return true;
}

// 依目前 g_gainSel / g_bitsSel 寫入 CONFIG
// bit15 RST | bit13 BRNG | bit12:11 PG | bit10:7 BADC | bit6:3 SADC | bit2:0 MODE
static bool applyConfig() {
  uint16_t cfg = 0;
  cfg |= (1u << 13);                            // BRNG = 32V
  cfg |= ((uint16_t)(g_gainSel & 0x03) << 11);
  cfg |= ((uint16_t)(g_bitsSel & 0x0F) << 7);   // BADC
  cfg |= ((uint16_t)(g_bitsSel & 0x0F) << 3);   // SADC
  cfg |= 0x07;                                  // MODE = shunt & bus, continuous
  return writeReg(REG_CONFIG, cfg);
}

// ── 量測：在 windowMs 毫秒內連續取樣，累積一次/二次矩 ──────────────────────
// 直流值取平均（消掉漣波），交流分量取標準差（當漣波指標）
static bool measure(uint32_t windowMs, Result &r) {
  const uint32_t windowUs = windowMs * 1000UL;
  uint32_t t0 = micros();

  uint32_t n = 0;
  int64_t  sumS = 0;  uint64_t sumS2 = 0;  int32_t maxAbsS = 0;
  int64_t  sumB = 0;  uint64_t sumB2 = 0;
  uint16_t raw;

  while ((uint32_t)(micros() - t0) < windowUs) {
    if (!readReg(REG_SHUNT, raw)) return false;
    int16_t s = (int16_t)raw;                 // 有號，LSB = 10 µV
    if (!readReg(REG_BUS, raw)) return false;
    int16_t b = (int16_t)(raw >> 3);          // 13-bit，LSB = 4 mV

    sumS  += s;
    sumS2 += (uint32_t)((int32_t)s * (int32_t)s);
    int32_t a = (s < 0) ? -(int32_t)s : (int32_t)s;
    if (a > maxAbsS) maxAbsS = a;

    sumB  += b;
    sumB2 += (uint32_t)((int32_t)b * (int32_t)b);
    n++;

    // 取樣抖動：避免取樣週期和電源漣波鎖相，讓漣波估計不失真
    delayMicroseconds(5 + (uint8_t)(micros() & 0x1F));
  }

  if (n < 8) return false;

  const float fn = (float)n;
  r.n  = n;
  r.fs = fn * 1000.0f / (float)windowMs;

  const float cnt2mV = 0.01f;                 // 10 µV → mV
  const float mv2mA  = 1.0f / g_shuntOhm;     // mV / Ω = mA
  const float cnt2V  = 0.004f;                // 4 mV

  // ── 電流 ──
  float meanS = (float)((double)sumS  / fn) - (float)g_offsetCnt;
  float msS   = (float)((double)sumS2 / fn);
  float rawMeanS = (float)((double)sumS / fn);
  float varS  = msS - rawMeanS * rawMeanS;    // 變異數用未扣偏移的平均
  if (varS < 0) varS = 0;

  r.idc = meanS          * cnt2mV * mv2mA;
  r.iac = sqrt(varS)     * cnt2mV * mv2mA;
  r.ipk = (float)maxAbsS * cnt2mV * mv2mA;

  // ── 電壓 ──
  float meanB = (float)((double)sumB  / fn);
  float msB   = (float)((double)sumB2 / fn);
  float varB  = msB - meanB * meanB;
  if (varB < 0) varB = 0;

  r.vdc = meanB      * cnt2V;
  r.vac = sqrt(varB) * cnt2V;

  // ── 導出量 ──
  // 電流太小時 R 會爆掉，門檻設 0.5 mA
  if (fabs(r.idc) > 0.5f) {
    r.r      = r.vdc / (r.idc / 1000.0f);
    r.ripple = r.iac / fabs(r.idc) * 100.0f;
  } else {
    r.r      = NAN;
    r.ripple = NAN;
  }
  r.p = r.vdc * r.idc;                        // V × mA = mW
  return true;
}

// ── 歸零：以目前讀值當直流偏移（執行前應斷開負載）──────────────────────────
static bool zeroOffset(uint32_t windowMs, float &offsetMa) {
  int32_t saved = g_offsetCnt;
  g_offsetCnt = 0;
  Result r;
  if (!measure(windowMs, r)) {
    g_offsetCnt = saved;
    return false;
  }
  // 由 idc(mA) 反推計數：counts = mA × shuntOhm / 0.01mV
  g_offsetCnt = (int32_t)(r.idc * g_shuntOhm / 0.01f + (r.idc >= 0 ? 0.5f : -0.5f));
  offsetMa = (float)g_offsetCnt * 0.01f / g_shuntOhm;
  return true;
}

// ── 輸出 ────────────────────────────────────────────────────────────────────
static void printNum(const char* key, float v, uint8_t digits) {
  Serial.print(' '); Serial.print(key); Serial.print('=');
  if (isnan(v)) Serial.print("nan"); else Serial.print(v, digits);
}

static void printResult(const Result &r) {
  Serial.print("OK n="); Serial.print(r.n);
  printNum("fs",     r.fs,     1);
  printNum("r",      r.r,      4);
  printNum("vdc",    r.vdc,    4);
  printNum("idc",    r.idc,    3);
  printNum("p",      r.p,      2);
  printNum("iac",    r.iac,    3);
  printNum("vac",    r.vac,    4);
  printNum("ripple", r.ripple, 2);
  printNum("ipk",    r.ipk,    3);
  printNum("off",    (float)g_offsetCnt * 0.01f / g_shuntOhm, 3);
  Serial.print(" tconv="); Serial.print((int)CONV_US[g_bitsSel]);
  Serial.println("");
}

static void printInfo() {
  Serial.print(F("OK id=CHLADNI_INA219 ver=2.0 mode=dc"));
  Serial.print(F(" shunt=")); Serial.print(g_shuntOhm, 3);
  Serial.print(F(" gain=")); Serial.print(40 << g_gainSel);
  Serial.print(F("mV bits=")); Serial.print(9 + g_bitsSel);
  Serial.print(F(" tconv=")); Serial.print((int)CONV_US[g_bitsSel]);
  Serial.print(F(" window=")); Serial.print(g_defWindowMs);
  Serial.print(F(" off=")); Serial.print((float)g_offsetCnt * 0.01f / g_shuntOhm, 3);
  Serial.println("");
}

// ── 指令解析 ────────────────────────────────────────────────────────────────
static void handleCommand(char* line) {
  // 去掉前後空白
  while (*line == ' ' || *line == '\t') line++;
  char* p = line + strlen(line);
  while (p > line && (p[-1] == ' ' || p[-1] == '\r' || p[-1] == '\n')) *--p = 0;
  if (!*line) return;

  // 指令轉大寫（只轉第一個 token）
  char* sp = strchr(line, ' ');
  char* arg = sp ? sp + 1 : NULL;
  if (sp) *sp = 0;
  for (char* c = line; *c; c++) *c = toupper(*c);

  if (!strcmp(line, "ID")) {
    printInfo();

  } else if (!strcmp(line, "MEAS")) {
    uint32_t w = arg ? (uint32_t)atol(arg) : g_defWindowMs;
    if (w < 20)   w = 20;
    if (w > 5000) w = 5000;
    Result r;
    if (measure(w, r)) printResult(r);
    else               Serial.println(F("ERR measure_failed"));

  } else if (!strcmp(line, "STREAM")) {
    uint32_t w = arg ? (uint32_t)atol(arg) : g_defWindowMs;
    if (w < 20) w = 20;
    while (!Serial.available()) {
      Result r;
      if (measure(w, r)) printResult(r);
      else { Serial.println(F("ERR measure_failed")); break; }
    }
    while (Serial.available()) Serial.read();
    Serial.println(F("OK stream_stopped"));

  } else if (!strcmp(line, "ZERO")) {
    if (arg) { for (char* c = arg; *c; c++) *c = toupper(*c); }
    if (arg && !strcmp(arg, "CLEAR")) {
      g_offsetCnt = 0;
      printInfo();
      return;
    }
    uint32_t w = arg ? (uint32_t)atol(arg) : 500;
    if (w < 100)  w = 100;
    if (w > 5000) w = 5000;
    float offMa = 0;
    if (zeroOffset(w, offMa)) printInfo();
    else                      Serial.println(F("ERR zero_failed"));

  } else if (!strcmp(line, "SET")) {
    if (!arg) { Serial.println(F("ERR set_needs_args")); return; }
    char* key = arg;
    char* sp2 = strchr(arg, ' ');
    char* val = sp2 ? sp2 + 1 : NULL;
    if (sp2) *sp2 = 0;
    for (char* c = key; *c; c++) *c = tolower(*c);
    if (!val) { Serial.println(F("ERR set_needs_value")); return; }

    if (!strcmp(key, "bits")) {
      int b = atoi(val);
      if (b < 9 || b > 12) { Serial.println(F("ERR bad_bits")); return; }
      g_bitsSel = b - 9;
      applyConfig();
      printInfo();

    } else if (!strcmp(key, "gain")) {
      int g = atoi(val);
      if      (g == 40)  g_gainSel = 0;
      else if (g == 80)  g_gainSel = 1;
      else if (g == 160) g_gainSel = 2;
      else if (g == 320) g_gainSel = 3;
      else { Serial.println(F("ERR bad_gain")); return; }
      applyConfig();
      printInfo();

    } else if (!strcmp(key, "shunt")) {
      float s = atof(val);
      if (s <= 0.0f) { Serial.println(F("ERR bad_shunt")); return; }
      g_shuntOhm = s;
      printInfo();

    } else if (!strcmp(key, "window")) {
      long w = atol(val);
      if (w < 20 || w > 5000) { Serial.println(F("ERR bad_window")); return; }
      g_defWindowMs = (uint32_t)w;
      printInfo();

    } else {
      Serial.println(F("ERR unknown_key"));
    }

  } else {
    Serial.println(F("ERR unknown_cmd"));
  }
}

// ── 主程式 ──────────────────────────────────────────────────────────────────
static char    g_buf[64];
static uint8_t g_len = 0;

void setup() {
  Serial.begin(115200);
  Wire.begin();
  Wire.setClock(400000);
  delay(50);

  uint16_t probe;
  if (!readReg(REG_CONFIG, probe)) {
    Serial.println(F("ERR ina219_not_found"));
  } else {
    applyConfig();
    delay(10);
    printInfo();
  }
}

void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (g_len > 0) {
        g_buf[g_len] = 0;
        handleCommand(g_buf);
        g_len = 0;
      }
    } else if (g_len < sizeof(g_buf) - 1) {
      g_buf[g_len++] = c;
    }
  }
}