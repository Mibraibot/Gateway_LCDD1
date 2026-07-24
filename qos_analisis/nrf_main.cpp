// ============================================================================
// FIRMWARE UJI QoS MODUL nRF24 (SEMENTARA - bukan firmware operasional!)
// Menjalankan scanNRF() terus-menerus dan mencetak hasil per sweep ke serial
// dalam format CSV: NRFQOS,<seq>,<durasi_us>,<panjang>,<valid>,<kanal_aktif>,<data125>
// Dianalisis oleh qos0_nrf_scan.py di laptop.
// ============================================================================
#include <Arduino.h>
#include "nrf_module.h"

uint32_t seq = 0;

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println(F("NRFQOS_START"));
  // Perilaku asli dipertahankan: jika modul tidak terdeteksi,
  // setupNRF() mencetak error JSON lalu berhenti (uji keandalan init).
  setupNRF();
  Serial.println(F("NRFQOS_INIT_OK"));
}

void loop() {
  uint32_t t0 = micros();
  String data = scanNRF();          // fungsi asli, TIDAK diubah sedikit pun
  uint32_t dur_us = micros() - t0;
  seq++;

  // Cek integritas di sisi node (dicek ulang juga di Python)
  bool valid = (data.length() == 125);
  int active = 0;
  for (uint16_t i = 0; i < data.length(); i++) {
    char c = data[i];
    if (c == '1') active++;
    else if (c != '0') valid = false;
  }

  Serial.print(F("NRFQOS,"));
  Serial.print(seq);          Serial.print(',');
  Serial.print(dur_us);       Serial.print(',');
  Serial.print(data.length());Serial.print(',');
  Serial.print(valid ? 1 : 0);Serial.print(',');
  Serial.print(active);       Serial.print(',');
  Serial.println(data);
}