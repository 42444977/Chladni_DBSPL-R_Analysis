#include <Wire.h>
#include <Adafruit_INA219.h>

Adafruit_INA219 ina219;

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);
  if (!ina219.begin()) {        // 用 Qwiic 請改成 ina219.begin(&Wire1)
    Serial.println("找不到 INA219，請檢查接線");
    while (1) delay(10);
  }
}

void loop() {
  float busV    = ina219.getBusVoltage_V();      // VIN− 對 GND
  float shuntmV = ina219.getShuntVoltage_mV();   // 分流電阻壓降
  float current = ina219.getCurrent_mA();
  float power   = ina219.getPower_mW();

  Serial.print("負載電壓: "); Serial.print(busV + shuntmV / 1000.0); Serial.println(" V");
  Serial.print("電流: ");     Serial.print(current); Serial.println(" mA");
  Serial.print("功率: ");     Serial.print(power);   Serial.println(" mW");
  Serial.print("阻抗: ");     Serial.print(busV + shuntmV /current);
  Serial.println();
  delay(1000);
}