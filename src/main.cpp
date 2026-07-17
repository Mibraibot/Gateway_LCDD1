#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Arduino.h>
#include <LoRa.h>
#include <SPI.h>
#include <WiFi.h>
#include <Wire.h>
#include <time.h>

// ============================================================================
// KREDENSIAL WIFI (hanya untuk NTP, TIDAK ada koneksi Firebase di gateway)
// ============================================================================
const char *ssid = "AN-WIFI";
const char *password = "12345678";

// --- KONFIGURASI NTP (WIB = UTC+7) ---
const char *ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 7 * 3600;
const int daylightOffset_sec = 0;

// ============================================================================
// >>> KONFIGURASI POLLING NODE <<<
// ============================================================================
// MODE SAAT INI: UJI COBA 1 NODE (hanya Node3 yang dipanggil)
//
// #############################################################
// #  CARA MENGAKTIFKAN 3 NODE:                                #
// #  Cukup ubah SATU baris di bawah ini menjadi:              #
// #                                                           #
// #    const uint8_t POLL_NODES[] = {1, 2, 3};                #
// #                                                           #
// #  Tidak ada bagian kode lain yang perlu diubah.            #
// #  Gateway otomatis memutar giliran: 1 -> 2 -> 3 -> 1 ...   #
// #  (Node yang timeout dilewati, giliran lanjut ke node      #
// #   berikutnya.)                                            #
// #############################################################
const uint8_t POLL_NODES[] = {3};

const uint8_t NUM_POLL_NODES = sizeof(POLL_NODES) / sizeof(POLL_NODES[0]);
const unsigned long POLL_TIMEOUT = 3000;    // ms menunggu balasan node
const unsigned long POLL_GAP = 500;         // ms jeda antar giliran poll
const unsigned long SYNC_INTERVAL = 300000; // broadcast sync ulang tiap 5 menit

// ============================================================================
// PIN & HARDWARE
// ============================================================================
#define BTN1 15 // Force NTP sync + broadcast waktu
#define BTN2 16 // Single click: Standby/Listening | Double click: Info

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

// ============================================================================
// STATE
// ============================================================================
enum GatewayMode { MODE_STANDBY, MODE_LISTENING };
GatewayMode currentMode = MODE_STANDBY;

int lastLoraRssi = 0;
String lastLoraMsg = "";
bool loraActive = false;

uint8_t pollIndex = 0; // posisi giliran dalam POLL_NODES[]
bool waitingForReply = false;
unsigned long pollStartTime = 0;
unsigned long nextPollTime = 0; // kapan poll berikutnya boleh dikirim
unsigned long lastSyncBroadcast = 0;

bool showingInfoScreen = false;
unsigned long infoScreenStartTime = 0;

// Debounce BTN2 (edge detection, gaya sama dengan node)
bool btn2State = HIGH, btn2LastReading = HIGH;
unsigned long btn2DebounceTime = 0;
bool btn2WaitingDouble = false;
unsigned long btn2ClickTimer = 0;

// ============================================================================
// LAYAR INFO GATEWAY
// ============================================================================
void showGatewayInfoScreen() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println(F("   [ GATEWAY INFO ]"));
  display.println(F("---------------------"));

  display.print(F("WiFi: "));
  if (WiFi.status() == WL_CONNECTED) {
    display.print(WiFi.RSSI());
    display.println(F(" dBm"));
  } else {
    display.println(F("DISCONNECTED"));
  }

  display.print(F("NTP : "));
  struct tm timeinfo;
  display.println(getLocalTime(&timeinfo, 10) ? F("SYNCED") : F("NOT SYNCED"));

  display.print(F("LoRa: "));
  display.println(loraActive ? F("ACTIVE (433MHz)") : F("FAILED"));

  display.print(F("Poll: Node"));
  display.print(POLL_NODES[pollIndex]);
  display.print(F(" ("));
  display.print(NUM_POLL_NODES);
  display.println(F(" node)"));

  display.display();
}

// ============================================================================
// BROADCAST SYNC WAKTU KE SEMUA NODE
// showScreen=true : tampilkan proses di OLED (untuk boot & tombol manual)
// showScreen=false: silent (untuk broadcast periodik, layar tak terganggu)
// ============================================================================
void broadcastTimeSync(bool showScreen) {
  time_t now = time(nullptr);
  if (now < 1000000000) {
    Serial.println(F("Gagal mendapatkan waktu valid untuk broadcast."));
    return;
  }

  // time() mengembalikan epoch UTC; node mencetak apa adanya,
  // jadi offset WIB (+7 jam) ditambahkan di sini.
  time_t localEpoch = now + gmtOffset_sec;

  if (showScreen) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println(F("Broadcasting Time..."));
    display.print(F("Epoch: "));
    display.println((unsigned long)localEpoch);
    display.display();
  }

  LoRa.beginPacket();
  LoRa.print(F("{\"type\":\"sync\",\"time\":"));
  LoRa.print((unsigned long)localEpoch);
  LoRa.print(F("}"));
  LoRa.endPacket();

  lastSyncBroadcast = millis();
  Serial.print(F("Sync waktu terkirim ke Node: "));
  Serial.println((unsigned long)localEpoch);

  if (showScreen)
    delay(1200); // tahan layar agar terbaca (hanya saat manual/boot)
}

// ============================================================================
// TAMPILAN UTAMA OLED
// ============================================================================
void updateOLEDDisplay() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  struct tm timeinfo;
  char timeStr[9] = "00:00:00";
  if (getLocalTime(&timeinfo, 10))
    strftime(timeStr, sizeof(timeStr), "%H:%M:%S", &timeinfo);

  display.setCursor(0, 0);
  display.print(currentMode == MODE_LISTENING ? F("LSTN  | ") : F("STDBY | "));
  display.println(timeStr);
  display.println(F("---------------------"));

  if (currentMode == MODE_LISTENING) {
    display.print(F("Poll: Node"));
    display.println(POLL_NODES[pollIndex]);

    display.print(F("LoRa RSSI: "));
    if (lastLoraRssi != 0) {
      display.print(lastLoraRssi);
      display.println(F(" dBm"));
    } else {
      display.println(F("-"));
    }

    display.print(F("Msg : "));
    if (lastLoraMsg.length() > 0)
      display.println(lastLoraMsg.substring(0, 12));
    else
      display.println(F("No Data"));
  } else {
    display.println();
    display.println(F("   GATEWAY STANDBY"));
    display.println();
    display.println(F("Press BTN2 to Listen"));
  }

  display.display();
}

// ============================================================================
// MAJUKAN GILIRAN POLLING KE NODE BERIKUTNYA
// (Inilah satu-satunya logika rotasi: otomatis bekerja untuk 1 atau 3 node)
// ============================================================================
void advancePolling() {
  pollIndex = (pollIndex + 1) % NUM_POLL_NODES;
  waitingForReply = false;
  nextPollTime = millis() + POLL_GAP;
}

// ============================================================================
// KIRIM KOMANDO POLL KE NODE GILIRAN SAAT INI
// ============================================================================
void sendPoll() {
  String cmd = "POLL_Node" + String(POLL_NODES[pollIndex]);

  LoRa.beginPacket();
  LoRa.print("{\"type\":\"command\",\"command\":\"" + cmd + "\"}");
  LoRa.endPacket();

  Serial.println(">>> Mengirim Komando Panggil: " + cmd);
  waitingForReply = true;
  pollStartTime = millis();
}

// ============================================================================
// PROSES BALASAN DARI NODE
// ============================================================================
void handleNodeReply() {
  int packetSize = LoRa.parsePacket();
  if (!packetSize)
    return;

  String receivedData;
  receivedData.reserve(packetSize);
  while (LoRa.available())
    receivedData += (char)LoRa.read();

  // Cetak blok JSON untuk dibaca app.py (format tidak diubah)
  Serial.println(F("{"));
  Serial.println(F("  \"event\": \"data_received\","));
  Serial.print(F("  \"rssi\": "));
  Serial.print(LoRa.packetRssi());
  Serial.println(F(","));
  Serial.print(F("  \"snr\": "));
  Serial.print(LoRa.packetSnr());
  Serial.println(F(","));
  Serial.print(F("  \"payload\": "));
  Serial.println(receivedData);
  Serial.println(F("}"));
  Serial.println(F("--------------------------------"));

  // Verifikasi balasan berasal dari node yang sedang dipanggil
  String expectedTag = "\"node\":\"Node" + String(POLL_NODES[pollIndex]) + "\"";
  // Toleransi format lama (JSON multi-baris dengan spasi setelah ':')
  String expectedTagOld =
      "\"node\": \"Node" + String(POLL_NODES[pollIndex]) + "\"";

  if (receivedData.indexOf(expectedTag) >= 0 ||
      receivedData.indexOf(expectedTagOld) >= 0) {
    lastLoraRssi = LoRa.packetRssi();
    lastLoraMsg = receivedData;
    Serial.println("-> Balasan diterima dari Node" +
                   String(POLL_NODES[pollIndex]));
    if (!showingInfoScreen)
      updateOLEDDisplay();
    advancePolling();
  }
  // Balasan dari node lain diabaikan; tetap menunggu sampai timeout
}

// ============================================================================
// SETUP
// ============================================================================
void setup() {
  Serial.begin(115200);

  pinMode(BTN1, INPUT_PULLUP);
  pinMode(BTN2, INPUT_PULLUP);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println(F("OLED Gagal!"));
  } else {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(15, 10);
    display.println(F("Welcome To"));
    display.setCursor(15, 25);
    display.println(F("Gateway LCDD"));

    for (int i = 0; i <= 100; i += 4) {
      display.drawRect(14, 45, 100, 8, SSD1306_WHITE);
      display.fillRect(16, 47, (i * 96) / 100, 4, SSD1306_WHITE);
      display.display();
      delay(20);
    }
  }

  // --- WiFi hanya untuk NTP (tanpa Firebase) ---
  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_8_5dBm); // Cegah brownout
  WiFi.begin(ssid, password);

  int animCounter = 0;
  unsigned long wifiStart = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifiStart < 20000) {
    display.clearDisplay();
    display.setCursor(0, 5);
    display.println(F("Connecting to wifi"));
    display.print(F("SSID: "));
    display.println(ssid);
    display.setCursor(0, 35);
    for (int i = 0; i <= animCounter; i++)
      display.print('.');
    display.display();
    animCounter = (animCounter + 1) % 6;
    delay(500);
  }

  if (WiFi.status() == WL_CONNECTED) {
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    Serial.println(F("Menunggu sinkronisasi waktu dari NTP..."));
    struct tm timeinfo;
    int retry = 0;
    while (!getLocalTime(&timeinfo) && retry < 20) {
      delay(500);
      retry++;
    }
    display.clearDisplay();
    display.setCursor(0, 0);
    display.println(F("WiFi Connected!"));
    display.print(F("RSSI: "));
    display.print(WiFi.RSSI());
    display.println(F(" dBm"));
    display.print(F("IP  : "));
    display.println(WiFi.localIP());
    display.display();
    delay(2000);
  } else {
    Serial.println(F("WiFi gagal terhubung. Lanjut tanpa NTP."));
  }

  // --- Inisialisasi LoRa ---
  SPI.begin(SCK, MISO, MOSI, SS);
  LoRa.setPins(SS, RST, DIO0);

  if (!LoRa.begin(433E6)) {
    Serial.println(F("LoRa Gateway Gagal!"));
    display.clearDisplay();
    display.setCursor(0, 0);
    display.println(F("LoRa Gagal!"));
    display.display();
    while (1)
      ;
  }
  loraActive = true;

  // Konfigurasi HARUS sama dengan node
  LoRa.setSpreadingFactor(9);
  LoRa.setSyncWord(0x34);
  LoRa.setTxPower(10); // Daya rendah untuk stabilitas catu daya

  Serial.println(F("Gateway LoRa Siap."));

  // Sync awal ke node, lalu langsung mulai listening
  broadcastTimeSync(true);
  currentMode = MODE_LISTENING;
  nextPollTime = millis();
  updateOLEDDisplay();
}

// ============================================================================
// LOOP
// ============================================================================
void loop() {
  static unsigned long lastTimeUpdate = 0;
  unsigned long now = millis();

  // ------------------- SIKLUS POLLING -------------------
  if (currentMode == MODE_LISTENING) {
    if (!waitingForReply) {
      // Broadcast sync periodik saat idle (node yang restart bisa ikut lagi)
      if (now - lastSyncBroadcast > SYNC_INTERVAL)
        broadcastTimeSync(false);

      if (now >= nextPollTime)
        sendPoll();
    } else {
      if (now - pollStartTime > POLL_TIMEOUT) {
        Serial.println(">>> Timeout! Node" + String(POLL_NODES[pollIndex]) +
                       " tidak merespon.");
        advancePolling(); // lewati, lanjut giliran berikutnya
      } else {
        handleNodeReply();
      }
    }
  }

  // ------------------- REFRESH JAM OLED -------------------
  if (now - lastTimeUpdate >= 1000) {
    lastTimeUpdate = now;
    if (!showingInfoScreen)
      updateOLEDDisplay();
  }

  // Auto-tutup layar info setelah 5 detik
  if (showingInfoScreen && (now - infoScreenStartTime > 5000)) {
    showingInfoScreen = false;
    updateOLEDDisplay();
  }

  // ------------------- BTN1: FORCE NTP SYNC + BROADCAST -------------------
  if (digitalRead(BTN1) == LOW) {
    Serial.println(F("Tombol 1: Force NTP Sync & Broadcast!"));
    display.clearDisplay();
    display.setCursor(0, 20);
    display.println(F("Forcing Sync..."));
    display.display();

    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    delay(1000);
    broadcastTimeSync(true);

    showingInfoScreen = false;
    updateOLEDDisplay();
    delay(200); // debounce sederhana
  }

  // ------------------- BTN2: SINGLE / DOUBLE CLICK -------------------
  // Edge detection + debounce (konsisten dengan logika di node)
  bool reading = digitalRead(BTN2);
  if (reading != btn2LastReading)
    btn2DebounceTime = now;

  if ((now - btn2DebounceTime) > 50) {
    if (reading != btn2State) {
      btn2State = reading;
      if (btn2State == LOW) {
        if (btn2WaitingDouble && (now - btn2ClickTimer < 300)) {
          // DOUBLE CLICK -> layar info
          btn2WaitingDouble = false;
          showingInfoScreen = true;
          infoScreenStartTime = now;
          showGatewayInfoScreen();
        } else {
          btn2WaitingDouble = true;
          btn2ClickTimer = now;
        }
      }
    }
  }
  btn2LastReading = reading;

  // Eksekusi single click bila jendela double click habis
  if (btn2WaitingDouble && (now - btn2ClickTimer >= 300)) {
    btn2WaitingDouble = false;

    if (showingInfoScreen) {
      // Klik sekali saat layar info aktif = tutup layar info
      showingInfoScreen = false;
      updateOLEDDisplay();
    } else if (currentMode == MODE_LISTENING) {
      currentMode = MODE_STANDBY;
      Serial.println(F("Mode: STANDBY. Berhenti polling."));
      updateOLEDDisplay();
    } else {
      currentMode = MODE_LISTENING;
      pollIndex = 0;
      waitingForReply = false;
      nextPollTime = now;
      Serial.println(F("Mode: LISTENING. Mulai polling node..."));
      updateOLEDDisplay();
    }
  }
}
