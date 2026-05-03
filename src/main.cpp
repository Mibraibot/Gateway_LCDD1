//Gateway

#include <Arduino.h>
#include <SPI.h>
#include <LoRa.h>

// --- PIN LORA ---
#define SS    5
#define RST   14
#define DIO0  26

// --- PIN SPI ---
#define SCK   18
#define MISO  19
#define MOSI  23

void setup() {
  Serial.begin(115200);
  while (!Serial);

  SPI.begin(SCK, MISO, MOSI, SS);
  LoRa.setPins(SS, RST, DIO0);

  if (!LoRa.begin(433E6)) {
    Serial.println("LoRa Gateway Gagal!");
    while (1);
  }

  // Konfigurasi harus sama dengan Node 1
  LoRa.setSpreadingFactor(12);
  LoRa.setSyncWord(0x34);
  
  Serial.println("Gateway LoRa Siap Menerima...");
}

void loop() {
  int packetSize = LoRa.parsePacket();
  
  if (packetSize) {
    String receivedData = "";
    while (LoRa.available()) {
      receivedData += (char)LoRa.read();
    }

    // Tampilkan dalam format JSON yang valid
    // Karena payload dari TX sudah berwujud JSON, kita bisa langsung melampirkannya (nesting)
    Serial.println("{");
    Serial.println("  \"event\": \"data_received\",");
    Serial.print("  \"rssi\": "); Serial.print(LoRa.packetRssi()); Serial.println(",");
    Serial.print("  \"snr\": "); Serial.print(LoRa.packetSnr()); Serial.println(",");
    Serial.print("  \"payload\": "); Serial.println(receivedData);
    Serial.println("}");
    Serial.println("--------------------------------");
  }
}