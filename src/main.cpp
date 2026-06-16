#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Arduino.h>
#include <HTTPClient.h>
#include <LoRa.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <Wire.h>
#include <time.h>
#include <ArduinoJson.h>

// --- KREDENSIAL WIFI ---
const char *ssid = "Byu";
const char *password = "123456789";

// --- KONFIGURASI NTP (WIB = UTC+7) ---
const char *ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 7 * 3600; // UTC +7 jam
const int daylightOffset_sec = 0;    // Tidak ada DST di Indonesia

// --- KONFIGURASI FIREBASE ---
const char *firebaseDatabaseUrl =
    "https://"
    "lowcostdronedetect-default-rtdb.asia-southeast1.firebasedatabase.app";

// --- PIN BUTTONS ---
#define BTN1 15
#define BTN2 16

// --- OLED SETUP ---
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// --- PIN LORA ---
#define SS 5
#define RST 14
#define DIO0 26

// --- PIN SPI ---
#define SCK 18
#define MISO 19
#define MOSI 23

// --- STATE DATA ---
int lastLoraRssi = 0;
String lastLoraMsg = "";
bool loraActive = false;
int lastFirebaseResponseCode = 0;

// Variabel Polling Gateway
int pollingNode = 3;
unsigned long pollStartTime = 0;
const unsigned long POLL_TIMEOUT = 3000;
bool waitingForReply = false;

// --- BUFFER DIHAPUS (Menggunakan Polling Langsung) ---

enum GatewayMode { MODE_STANDBY, MODE_LISTENING };
GatewayMode currentMode =
    MODE_STANDBY; // Dimulai dari Standby saat awal booting

// State Layar Informasi
bool showingInfoScreen = false;
unsigned long infoScreenStartTime = 0;

// Fungsi untuk menampilkan info sistem di OLED
void showGatewayInfoScreen() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  
  // Header
  display.setCursor(0, 0);
  display.println("   [ GATEWAY INFO ]");
  display.println("---------------------");
  
  // Status WiFi
  display.print("WiFi: ");
  if (WiFi.status() == WL_CONNECTED) {
    display.println("CONNECTED");
    display.print("SSID: "); display.println(ssid);
    display.print("RSSI: "); display.print(WiFi.RSSI()); display.println(" dBm");
  } else {
    display.println("DISCONNECTED");
  }
  
  // Status Firebase
  display.print("FB  : ");
  if (WiFi.status() == WL_CONNECTED) {
    if (lastFirebaseResponseCode == 200 || lastFirebaseResponseCode == 201) {
      display.println("CONNECTED (OK)");
    } else if (lastFirebaseResponseCode != 0) {
      display.print("ERR (code: "); display.print(lastFirebaseResponseCode); display.println(")");
    } else {
      display.println("READY (Idle)");
    }
  } else {
    display.println("NO NETWORK");
  }
  
  // Status LoRa
  display.print("LoRa: ");
  if (loraActive) {
    display.println("ACTIVE (433MHz)");
  } else {
    display.println("FAILED / ERROR");
  }
  
  display.display();
}

// Fungsi untuk menyebarkan (broadcast) waktu lokal ke seluruh Node
void broadcastTimeSync() {
  time_t now = time(nullptr);
  if (now < 1000000000) {
    Serial.println("Gagal mendapatkan waktu valid untuk broadcast.");
    return;
  }

  display.clearDisplay();
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("Broadcasting Time...");
  display.print("Epoch: ");
  display.println(now);
  display.display();

  // Mengirim data epoch timestamp dalam format JSON via LoRa
  LoRa.beginPacket();
  LoRa.print("{\"type\":\"sync\",\"time\":");
  LoRa.print(now);
  LoRa.print("}");
  LoRa.endPacket(); // Di sinilah transmisi fisik terjadi

  Serial.print("Waktu sinkronisasi terkirim ke Node: ");
  Serial.println(now);
  delay(1500);
}

// Fungsi untuk memancarkan komando jarak jauh (Remote Control) ke seluruh Node
void broadcastCommand(String cmd) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("Broadcasting CMD...");
  display.print("-> ");
  display.println(cmd);
  display.display();

  LoRa.beginPacket();
  LoRa.print("{\"type\":\"command\",\"command\":\"" + cmd + "\"}");
  LoRa.endPacket();

  Serial.print("Komando terkirim ke Node: ");
  Serial.println(cmd);
  delay(1000); // Tahan layar sebentar agar terbaca
}

// Fungsi untuk mengirim data tunggal Node ke Firebase
void sendToFirebaseSingle(String nodeKey, String data_hex, String timestamp_wib, String captured_at, int rssi, float snr) {
  if (WiFi.status() == WL_CONNECTED) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(15, 25);
    display.println("Sending data");
    display.setCursor(15, 40);
    display.println("To Firebase...");
    display.display();

    WiFiClientSecure client;
    client.setInsecure();
    HTTPClient http;
    String url = String(firebaseDatabaseUrl) + "/detection_system.json";
    http.begin(client, url);
    http.addHeader("Content-Type", "application/json");

    JsonDocument doc;
    JsonObject nodeObj = doc[nodeKey].to<JsonObject>();
    nodeObj["data_hex"] = "RAW_" + data_hex;
    nodeObj["captured_at"] = captured_at;
    nodeObj["timestap_wib"] = timestamp_wib;
    nodeObj["node"] = nodeKey;
    nodeObj["rssi"] = rssi;
    nodeObj["snr"] = snr;

    String jsonPayload;
    serializeJson(doc, jsonPayload);

    Serial.print("Mengirim data ");
    Serial.print(nodeKey);
    Serial.println(" ke Firebase...");
    int httpResponseCode = http.PATCH(jsonPayload); 
    lastFirebaseResponseCode = httpResponseCode;

    if (httpResponseCode > 0) {
      Serial.print("Firebase Response Code: ");
      Serial.println(httpResponseCode);
    } else {
      Serial.print("Error saat kirim ke Firebase: ");
      Serial.println(httpResponseCode);
    }
    http.end();
  } else {
    Serial.println("Koneksi WiFi Terputus, Gagal Kirim ke Firebase.");
  }
}

// Fungsi untuk memperbarui tampilan OLED dengan waktu dan status terkini
void updateOLEDDisplay(int loraRssi, String loraMsg) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  // Ambil waktu dari NTP lokal
  struct tm timeinfo;
  char timeStr[9] = "00:00:00";
  if (getLocalTime(&timeinfo)) {
    strftime(timeStr, sizeof(timeStr), "%H:%M:%S", &timeinfo);
  }

  // Baris 1: Header & Jam Digital
  display.setCursor(0, 0);
  if (currentMode == MODE_LISTENING) {
    display.print("LSTN  | ");
  } else {
    display.print("STDBY | ");
  }
  display.println(timeStr);
  display.println("---------------------");

  if (currentMode == MODE_LISTENING) {
    // Baris 2: Informasi WiFi
    display.print("WiFi: ");
    display.print(ssid);
    display.print(" (");
    display.print(WiFi.RSSI());
    display.println("dBm)");

    // Baris 3: Status LoRa RSSI
    display.print("LoRa RSSI: ");
    if (loraRssi != 0) {
      display.print(loraRssi);
      display.println(" dBm");
    } else {
      display.println("-");
    }

    // Baris 4: Pesan LoRa Masuk
    display.print("Msg : ");
    if (loraMsg.length() > 0) {
      display.println(loraMsg.substring(0, 12)); // Potong agar muat di layar
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
  while (!Serial)
    ;

  // Inisialisasi Tombol
  pinMode(BTN1, INPUT_PULLUP);
  pinMode(BTN2, INPUT_PULLUP);

  // Inisialisasi OLED
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println(F("OLED Gagal!"));
  } else {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);

    // Tampilan Selamat Datang
    display.setTextSize(1);
    display.setCursor(15, 10);
    display.println("Welcome To");
    display.setTextSize(1);
    display.setCursor(15, 25);
    display.println("Gateway LCDD");

    // Animasi loading bar
    for (int i = 0; i <= 100; i += 2) {
      display.drawRect(14, 45, 100, 8, SSD1306_WHITE);
      display.fillRect(16, 47, (i * 96) / 100, 4, SSD1306_WHITE);
      display.display();
      delay(30);
    }
    delay(500);

    // Menghubungkan ke WiFi dengan Animasi
    WiFi.setTxPower(WIFI_POWER_8_5dBm); // Turunkan daya TX WiFi untuk mencegah restart/brownout
    WiFi.begin(ssid, password);
    int animCounter = 0;
    while (WiFi.status() != WL_CONNECTED) {
      display.clearDisplay();
      display.setTextSize(1);
      display.setCursor(0, 5);
      display.println("Connecting to wifi");
      display.print("SSID: ");
      display.println(ssid);

      // Animasi titik-titik berjalan
      display.setCursor(0, 35);
      for (int i = 0; i <= animCounter; i++) {
        display.print(".");
      }
      display.display();

      animCounter = (animCounter + 1) % 6;
      delay(500);
    }

    // Konfigurasi NTP Time Sync
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);

    // Tunggu sampai waktu benar-benar sinkron (maksimal 10 detik)
    Serial.println("Menunggu sinkronisasi waktu dari NTP...");
    struct tm timeinfo;
    int retry = 0;
    while (!getLocalTime(&timeinfo) && retry < 20) {
      delay(500);
      retry++;
    }

    // Berhasil terhubung ke WiFi
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
    while (1)
      ;
  }
  loraActive = true;

  // Konfigurasi harus sama dengan Node 1
  LoRa.setSpreadingFactor(9); // Akselerasi: Diubah dari SF12 menjadi SF9 untuk kecepatan transmisi ekstra
  LoRa.setSyncWord(0x34);

  // TURUNKAN DAYA LORA TX UNTUK MENCEGAH BROWNOUT/RESET RESET KARENA ARUS DROP
  LoRa.setTxPower(10); // Diturunkan lebih ekstrim dari 14 ke 10 agar lebih stabil di daya rendah

  Serial.println("Gateway LoRa Siap Menerima...");

  // SINKRONISASI AWAL: Kirim jam ke Node terlebih dahulu via LoRa
  broadcastTimeSync();

  // Set mode ke Listening setelah broadcast waktu selesai
  currentMode = MODE_LISTENING;

  // Tampilkan status siap awal
  updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
}

void loop() {
  static unsigned long lastTimeUpdate = 0;

  // Proses Polling LoRa berjalan saat MODE_LISTENING
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
      Serial.println(">>> Mengirim Komando Panggil: " + cmd);

      waitingForReply = true;
      pollStartTime = millis();
    } else {
      // Tunggu balasan
      if (millis() - pollStartTime > POLL_TIMEOUT) {
        Serial.println(">>> Timeout! Node " + String(pollingNode) + " tidak merespon.");
        pollingNode = 3; // Hanya Node 3
        waitingForReply = false;
      } else {
        int packetSize = LoRa.parsePacket();
        if (packetSize) {
          String receivedData = "";
          while (LoRa.available()) {
            receivedData += (char)LoRa.read();
          }

          // Tampilkan dalam format JSON yang valid untuk record_data.py
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

          struct tm timeinfo;
          char timeStr[25] = "Unknown";
          if (getLocalTime(&timeinfo)) {
            strftime(timeStr, sizeof(timeStr), "%Y-%m-%d %H:%M:%S", &timeinfo);
          }

          JsonDocument doc;
          DeserializationError error = deserializeJson(doc, receivedData);
          String nodeName = "";
          String dataHex = "";
          String capturedAt = "";

          if (!error) {
            nodeName = doc["node"] | "";
            nodeName.replace(" ", ""); 
            dataHex = doc["data_hex"] | receivedData;
            capturedAt = doc["timestamp_wib"] | timeStr; 
          } else {
            dataHex = receivedData;
            capturedAt = timeStr;
            if (receivedData.indexOf("Node1") >= 0) nodeName = "Node1";
            else if (receivedData.indexOf("Node2") >= 0) nodeName = "Node2";
            else if (receivedData.indexOf("Node3") >= 0) nodeName = "Node3";
          }

          String expectedNode = "Node" + String(pollingNode);
          if (nodeName == expectedNode) {
            Serial.println("-> Balasan diterima dari " + expectedNode);
            sendToFirebaseSingle(expectedNode, dataHex, capturedAt, timeStr, lastLoraRssi, LoRa.packetSnr());
            pollingNode = 3; // Hanya Node 3
            waitingForReply = false;
          }
        }
      }
    }
  }

  // Update jam digital di OLED setiap 1 detik (jika tidak sedang menampilkan layar info)
  if (millis() - lastTimeUpdate >= 1000) {
    lastTimeUpdate = millis();
    if (!showingInfoScreen) {
      updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
    }
  }

  // Sembunyikan layar informasi setelah 5 detik dan kembali ke menu utama
  if (showingInfoScreen && (millis() - infoScreenStartTime > 5000)) {
    showingInfoScreen = false;
    updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
  }

  // BUTTON 1 (Pin 15): Paksa Sinkronisasi Ulang NTP & Broadcast Waktu
  if (digitalRead(BTN1) == LOW) {
    Serial.println("Tombol 1 Ditekan: Force NTP Sync & Broadcast!");

    // Tampilkan status sinkronisasi di layar OLED
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 20);
    display.println("Forcing Sync...");
    display.display();

    // Tarik ulang waktu dari server NTP
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    delay(1000);

    // Kirim waktu via LoRa
    broadcastTimeSync();

    // Refresh OLED
    showingInfoScreen = false; // Batalkan info screen jika sedang aktif
    updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
    delay(200); // Debounce
  }

  // BUTTON 2 (Pin 16): Deteksi Ketukan Tunggal / Ganda (Double Click)
  static unsigned long btn2PressTime = 0;
  static bool btn2PendingSingleClick = false;

  if (digitalRead(BTN2) == LOW) {
    unsigned long now_ms = millis();
    if (now_ms - btn2PressTime > 250) { // Debounce tombol
      if (now_ms - btn2PressTime < 450) { // Rentang waktu ketukan ganda (double-click)
        // Double Click terdeteksi!
        btn2PendingSingleClick = false;
        showingInfoScreen = true;
        infoScreenStartTime = millis();
        showGatewayInfoScreen();
      } else {
        // Tandai sebagai kemungkinan single-click
        btn2PendingSingleClick = true;
      }
      btn2PressTime = now_ms;
    }
  }

  // Jalankan perintah single-click jika jeda waktu double-click terlampaui
  if (btn2PendingSingleClick && (millis() - btn2PressTime > 450)) {
    btn2PendingSingleClick = false;
    if (!showingInfoScreen) {
      // Toggle mode Standby / Listening
      if (currentMode == MODE_LISTENING) {
        currentMode = MODE_STANDBY;
        Serial.println("Mode: STANDBY");
        Serial.println("Berhenti Polling.");
        
        // Reset sinkronisasi agar bersih saat mulai listen lagi
        
      } else {
        currentMode = MODE_LISTENING;
        Serial.println("Mode: LISTENING");
        pollingNode = 3; waitingForReply = false; // Reset polling
        Serial.println("Mulai Polling Node...");
      }
      updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
    } else {
      // Jika info screen sedang aktif, menekan tombol sekali akan menutup info screen lebih cepat
      showingInfoScreen = false;
      updateOLEDDisplay(lastLoraRssi, lastLoraMsg);
    }
  }
}