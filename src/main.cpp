#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Arduino.h>
#include <ArduinoJson.h>
#include <LoRa.h>
#include <SPI.h>
#include <WiFi.h>
#include <Wire.h>
#include <time.h>

// ============================================================================
// KREDENSIAL WIFI (HANYA UNTUK SINKRONISASI WAKTU NTP)
// ============================================================================
const char *ssid = "LT-3 POJOK";          // UBAH JIKA PERLU
const char *password = "R@nd0mNN208}";  // UBAH JIKA PERLU

const char *ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 7 * 3600; 
const int daylightOffset_sec = 0;    

#define BTN1 15
#define BTN2 16

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

#define SS 5
#define RST 14
#define DIO0 26

#define SCK 18
#define MISO 19
#define MOSI 23

int lastLoraRssi = 0;
String lastLoraMsg = "";
bool loraActive = false;

// OPTIMASI TIMEOUT AGRESIF (0.8 Detik) & ROTASI BERURUTAN
int pollingNode = 1; // Mulai dari Node 1
unsigned long pollStartTime = 0;
const unsigned long POLL_TIMEOUT = 800; 
bool waitingForReply = false;

enum GatewayMode { MODE_STANDBY, MODE_LISTENING };
GatewayMode currentMode = MODE_STANDBY; 

bool showingInfoScreen = false;
unsigned long infoScreenStartTime = 0;

void showGatewayInfoScreen() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  display.setCursor(0, 0);
  display.println("   [ GATEWAY INFO ]");
  display.println("---------------------");

  display.print("WiFi: ");
  if (WiFi.status() == WL_CONNECTED) {
    display.println("CONNECTED (NTP)");
    display.print("SSID: ");
    display.println(ssid);
    display.print("RSSI: ");
    display.print(WiFi.RSSI());
    display.println(" dBm");
  } else {
    display.println("DISCONNECTED");
  }

  // Status Firebase Dihapus untuk efisiensi layar

  display.print("LoRa: ");
  if (loraActive) {
    display.println("ACTIVE (433MHz)");
  } else {
    display.println("FAILED / ERROR");
  }

  display.display();
}

void broadcastTimeSync() {
  time_t now = time(nullptr);
  if (now < 1000000000) {
    Serial.println("Gagal mendapatkan waktu valid untuk broadcast.");
    return;
  }
  time_t localEpoch = now + gmtOffset_sec;

  display.clearDisplay();
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("Broadcasting Time...");
  display.print("Epoch: ");
  display.println(localEpoch);
  display.display();

  LoRa.beginPacket();
  LoRa.print("{\"type\":\"sync\",\"time\":");
  LoRa.print(localEpoch);
  LoRa.print("}");
  LoRa.endPacket();
  LoRa.receive(); // Paksa LoRa langsung kembali mendengar

  Serial.print("Waktu sinkronisasi terkirim ke Node: ");
  Serial.println(localEpoch);
  delay(1500);
}

void updateOLEDDisplay(int loraRssi, String loraMsg) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  struct tm timeinfo;
  char timeStr[9] = "00:00:00";
  if (getLocalTime(&timeinfo)) {
    strftime(timeStr, sizeof(timeStr), "%H:%M:%S", &timeinfo);
  }

  display.setCursor(0, 0);
  if (currentMode == MODE_LISTENING) {
    display.print("LSTN  | ");
  } else {
    display.print("STDBY | ");
  }
  display.println(timeStr);
  display.println("---------------------");

  if (currentMode == MODE_LISTENING) {
    display.print("WiFi: ");
    display.print(ssid);
    display.print(" (");
    display.print(WiFi.RSSI());
    display.println("dBm)");

    display.print("LoRa RSSI: ");
    if (loraRssi != 0) {
      display.print(loraRssi);
      display.println(" dBm");
    } else {
      display.println("-");
    }

    display.print("Msg : ");
    if (loraMsg.length() > 0) {
      display.println(loraMsg.substring(0, 12)); 
    } else {
      display.println("No Data");
    }
  } else {
    display.println();
    display.println("   GATEWAY STANDBY");
    display.println();
    display.println("Press BTN2 to Listen");
  }

  display.display();
}

void setup() {
  Serial.begin(115200);
  while (!Serial);

  pinMode(BTN1, INPUT_PULLUP);
  pinMode(BTN2, INPUT_PULLUP);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println(F("OLED Gagal!"));
  } else {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(15, 10);
    display.println("Welcome To");
    display.setTextSize(1);
    display.setCursor(15, 25);
    display.println("Gateway LCDD");

    for (int i = 0; i <= 100; i += 2) {
      display.drawRect(14, 45, 100, 8, SSD1306_WHITE);
      display.fillRect(16, 47, (i * 96) / 100, 4, SSD1306_WHITE);
      display.display();
      delay(30);
    }
    delay(500);

    // KONEKSI WIFI HANYA UNTUK NTP WAKTU LOKAL
    WiFi.setTxPower(WIFI_POWER_8_5dBm); 
    WiFi.begin(ssid, password);
    int animCounter = 0;
    while (WiFi.status() != WL_CONNECTED) {
      display.clearDisplay();
      display.setTextSize(1);
      display.setCursor(0, 5);
      display.println("Connecting to wifi");
      display.print("SSID: ");
      display.println(ssid);
      display.setCursor(0, 35);
      for (int i = 0; i <= animCounter; i++) display.print(".");
      display.display();
      animCounter = (animCounter + 1) % 6;
      delay(500);
    }

    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    Serial.println("Menunggu sinkronisasi waktu dari NTP...");
    struct tm timeinfo;
    int retry = 0;
    while (!getLocalTime(&timeinfo) && retry < 20) {
      delay(500);
      retry++;
    }

    display.clearDisplay();
    display.setCursor(0, 0);
    display.println("WiFi Connected!");
    display.println("---------------------");
    display.print("SSID: ");
    display.println(ssid);
    display.print("RSSI: ");
    display.print(WiFi.RSSI());
    display.println(" dBm");
    display.print("IP  : ");
    display.println(WiFi.localIP());
    display.display();
    delay(3000);
  }

  SPI.begin(SCK, MISO, MOSI, SS);
  LoRa.setPins(SS, RST, DIO0);

  if (!LoRa.begin(433E6)) {
    Serial.println("LoRa Gateway Gagal!");
    display.clearDisplay();
    display.setCursor(0, 0);
    display.println("LoRa Gagal!");
    display.display();
    while (1);
  }
  loraActive = true;

  // LORA GATEWAY HIGH-SPEED
  LoRa.setSpreadingFactor(7); 
  LoRa.setSignalBandwidth(250E3);
  LoRa.setSyncWord(0x34);
  LoRa.setTxPower(10); 

  Serial.println("Gateway LoRa Siap Menerima...");
  broadcastTimeSync();
  currentMode = MODE_LISTENING;
  updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
}

void loop() {
  static unsigned long lastTimeUpdate = 0;

  if (currentMode == MODE_LISTENING) {
    if (!waitingForReply) {
      String cmd = "POLL_Node" + String(pollingNode);

      display.clearDisplay();
      display.setTextSize(1);
      display.setCursor(0, 0);
      display.println("LSTN | Polling...");
      display.println("---------------------");
      display.println("Wait: " + cmd);
      display.display();

      LoRa.beginPacket();
      LoRa.print("{\"type\":\"command\",\"command\":\"" + cmd + "\"}");
      LoRa.endPacket();
      
      // MEMASTIKAN LORA GATEWAY LANGSUNG MEMBUKA TELINGA
      LoRa.receive(); 

      Serial.println(">>> Mengirim Komando Panggil: " + cmd);

      waitingForReply = true;
      pollStartTime = millis();
    } else {
      if (millis() - pollStartTime > POLL_TIMEOUT) {
        Serial.println(">>> Timeout! Node " + String(pollingNode) + " tidak merespon.");
        pollingNode = (pollingNode % 3) + 1; // Pindah otomatis: 1 -> 2 -> 3 -> 1
        waitingForReply = false;
      } else {
        int packetSize = LoRa.parsePacket();
        if (packetSize) {
          String receivedData = "";
          while (LoRa.available()) {
            receivedData += (char)LoRa.read();
          }

          // CETAK JSON KE PYTHON (app.py)
          Serial.println("{");
          Serial.println("  \"event\": \"data_received\",");
          Serial.print("  \"rssi\": ");
          Serial.print(LoRa.packetRssi());
          Serial.println(",");
          Serial.print("  \"snr\": ");
          Serial.print(LoRa.packetSnr());
          Serial.println(",");
          Serial.print("  \"payload\": ");
          Serial.println(receivedData);
          Serial.println("}");
          Serial.println("--------------------------------");

          lastLoraRssi = LoRa.packetRssi();
          lastLoraMsg = receivedData;
          updateOLEDDisplay(lastLoraRssi, lastLoraMsg);

          // Pengecekan apakah balasan berasal dari Node yang benar
          JsonDocument doc;
          DeserializationError error = deserializeJson(doc, receivedData);
          String nodeName = "";
          
          if (!error) {
            nodeName = doc["node"] | "";
            nodeName.replace(" ", "");
          } else {
            if (receivedData.indexOf("Node1") >= 0) nodeName = "Node1";
            else if (receivedData.indexOf("Node2") >= 0) nodeName = "Node2";
            else if (receivedData.indexOf("Node3") >= 0) nodeName = "Node3";
          }

          String expectedNode = "Node" + String(pollingNode);
          if (nodeName == expectedNode) {
            Serial.println("-> Balasan sukses diterima dari " + expectedNode);
            
            // GATEWAY LANGSUNG PINDAH KE NODE BERIKUTNYA TANPA MENUNGGU FIREBASE
            pollingNode = (pollingNode % 3) + 1; 
            waitingForReply = false;
          }
        }
      }
    }
  }

  // --- LOGIKA UPDATE LAYAR OLED ---
  if (millis() - lastTimeUpdate >= 1000) {
    lastTimeUpdate = millis();
    if (!showingInfoScreen) {
      updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
    }
  }

  if (showingInfoScreen && (millis() - infoScreenStartTime > 5000)) {
    showingInfoScreen = false;
    updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
  }

  // --- LOGIKA TOMBOL 1 (SINKRONISASI NTP) ---
  if (digitalRead(BTN1) == LOW) {
    Serial.println("Tombol 1 Ditekan: Force NTP Sync & Broadcast!");
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 20);
    display.println("Forcing Sync...");
    display.display();
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    delay(1000);
    broadcastTimeSync();
    showingInfoScreen = false; 
    updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
    delay(200); 
  }

  // --- LOGIKA TOMBOL 2 (MODE & INFO SCREEN) ---
  static unsigned long btn2PressTime = 0;
  static bool btn2PendingSingleClick = false;

  if (digitalRead(BTN2) == LOW) {
    unsigned long now_ms = millis();
    if (now_ms - btn2PressTime > 250) { 
      if (now_ms - btn2PressTime < 450) { 
        btn2PendingSingleClick = false;
        showingInfoScreen = true;
        infoScreenStartTime = millis();
        showGatewayInfoScreen();
      } else {
        btn2PendingSingleClick = true;
      }
      btn2PressTime = now_ms;
    }
  }

  if (btn2PendingSingleClick && (millis() - btn2PressTime > 450)) {
    btn2PendingSingleClick = false;
    if (!showingInfoScreen) {
      if (currentMode == MODE_LISTENING) {
        currentMode = MODE_STANDBY;
        Serial.println("Mode: STANDBY");
      } else {
        currentMode = MODE_LISTENING;
        Serial.println("Mode: LISTENING");
        waitingForReply = false; 
      }
      updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
    } else {
      showingInfoScreen = false;
      updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
    }
  }
}