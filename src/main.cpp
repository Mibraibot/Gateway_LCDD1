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
const char *ssid = "ULIN";
const char *password = "ulinppc245";

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
const uint8_t POLL_NODES[] = {1, 2, 3};

const uint8_t NUM_POLL_NODES = sizeof(POLL_NODES) / sizeof(POLL_NODES[0]);

// Anggaran waktu balasan node (SF7, data_hex tetap 125 karakter '0'/'1'):
//   airtime poll ~87ms + scan NRF ~330ms + OLED/serial node ~40ms +
//   airtime balasan 182B ~292ms  =  ~750ms  ->  timeout 1500ms margin ~2x.
// Node yang mati kini hanya menahan siklus 1,5 dtk (sebelumnya 3 dtk).
const unsigned long POLL_TIMEOUT = 1500;    // ms menunggu balasan node
// Node kembali siap RX beberapa ms setelah transmit; 100ms sudah cukup
// aman (sebelumnya 500ms yang membuang 0,4 dtk tiap giliran).
const unsigned long POLL_GAP = 100;         // ms jeda antar giliran poll
const unsigned long SYNC_INTERVAL = 300000; // broadcast sync ulang tiap 5 menit

// ============================================================================
// BACKOFF NODE OFFLINE (anti-macet antar node)
// Tanpa ini, satu node mati (kasus Node1) menahan siklus SETIAP putaran
// selama POLL_TIMEOUT — node sehat ikut melambat. Dengan backoff: setelah
// 2x timeout beruntun node ditandai offline dan hanya di-probe tiap 5 dtk;
// node sehat terus berputar dengan kecepatan penuh. Begitu node offline
// menjawab probe (dan langsung tersinkron dari "time" di paket poll), ia
// otomatis kembali masuk rotasi normal.
// ============================================================================
const uint8_t OFFLINE_AFTER_MISSES = 2;
const unsigned long OFFLINE_PROBE_MS = 5000;

// Pemulihan mandiri: bila TIDAK ADA SATU PUN balasan selama 30 dtk padahal
// sedang LISTENING, kemungkinan radio gateway sendiri yang macet — reset +
// konfigurasi ulang modul LoRa via software (tanpa sentuh kabel/pin).
const unsigned long RADIO_STALL_MS = 30000;

// ============================================================================
// KONFIGURASI RADIO — HARUS IDENTIK DENGAN NODE!
// SF7 (sebelumnya SF9): airtime turun ~4x. Dengan RSSI node -35..-47 dBm
// margin link masih sangat besar. Bila node ditempatkan jauh (RSSI di bawah
// sekitar -100 dBm), naikkan LORA_SF ke 8 atau 9 DI KEDUA SISI.
// ============================================================================
#define LORA_SF 7

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

// State backoff offline per node (paralel dengan POLL_NODES[])
uint8_t missCount[NUM_POLL_NODES] = {0};
unsigned long probeDueAt[NUM_POLL_NODES] = {0};
unsigned long lastReplyTime = 0; // balasan terakhir dari node mana pun

bool showingInfoScreen = false;
unsigned long infoScreenStartTime = 0;

// Debounce BTN2 (edge detection, gaya sama dengan node)
bool btn2State = HIGH, btn2LastReading = HIGH;
unsigned long btn2DebounceTime = 0;
bool btn2WaitingDouble = false;
unsigned long btn2ClickTimer = 0;

// ============================================================================
// KONFIGURASI & PEMULIHAN MANDIRI RADIO
// ============================================================================
void configureLoRaRadio() {
  // Konfigurasi HARUS sama dengan node
  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSyncWord(0x34);
  LoRa.setTxPower(10); // Daya rendah untuk stabilitas catu daya
  // CRC aktif: paket korup dibuang radio, tidak lolos ke parser dan tidak
  // membuang satu giliran polling untuk data rusak
  LoRa.enableCrc();
}

// LoRa.begin() memicu reset hardware modul lewat pin RST yang memang sudah
// terpasang, lalu semua register dikonfigurasi ulang — murni software.
void recoverLoRa() {
  LoRa.sleep();
  if (LoRa.begin(433E6)) {
    configureLoRaRadio();
    loraActive = true;
    Serial.println(F(">>> Radio LoRa di-reset & dikonfigurasi ulang."));
  } else {
    loraActive = false;
    Serial.println(F(">>> Re-init radio LoRa GAGAL!"));
  }
}

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
// (Satu-satunya logika rotasi; otomatis bekerja untuk 1 atau 3 node.)
// Node offline dilewati sampai jadwal probe-nya tiba, jadi node sehat tidak
// pernah ikut menunggu POLL_TIMEOUT milik node yang mati.
// ============================================================================
void advancePolling() {
  waitingForReply = false;
  unsigned long now = millis();

  uint8_t fallbackIdx = pollIndex;
  unsigned long fallbackDue = 0;
  bool haveFallback = false;

  for (uint8_t hop = 1; hop <= NUM_POLL_NODES; hop++) {
    uint8_t idx = (pollIndex + hop) % NUM_POLL_NODES;
    bool offline = missCount[idx] >= OFFLINE_AFTER_MISSES;
    // (long) cast: aman terhadap wrap-around millis()
    if (!offline || (long)(now - probeDueAt[idx]) >= 0) {
      pollIndex = idx;
      nextPollTime = now + POLL_GAP;
      return;
    }
    if (!haveFallback || (long)(probeDueAt[idx] - fallbackDue) < 0) {
      fallbackIdx = idx;
      fallbackDue = probeDueAt[idx];
      haveFallback = true;
    }
  }

  // Semua node sedang offline: langsung tidur sampai jadwal probe terdekat
  pollIndex = fallbackIdx;
  nextPollTime = fallbackDue;
}

// ============================================================================
// KIRIM KOMANDO POLL KE NODE GILIRAN SAAT INI
// ============================================================================
void sendPoll() {
  String cmd = "POLL_Node" + String(POLL_NODES[pollIndex]);

  // Paket ringkas (field "type" dibuang; node mencocokkan substring
  // "command":"POLL_NodeX" jadi tetap kompatibel) + epoch WIB dititipkan di
  // SETIAP poll: node yang baru menyala/restart langsung tersinkron dari
  // poll pertama yang didengarnya, tanpa menunggu broadcast 5-menit.
  String pkt = "{\"command\":\"" + cmd + "\"";
  time_t nowEpoch = time(nullptr);
  if (nowEpoch >= 1000000000)
    pkt += ",\"time\":" + String((unsigned long)(nowEpoch + gmtOffset_sec));
  pkt += "}";

  LoRa.beginPacket();
  LoRa.print(pkt);
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

  // QoS: RTT poll -> balasan diukur dengan jam gateway sendiri (millis),
  // bebas masalah sinkronisasi jam antar perangkat. Mencakup airtime poll
  // + proses node (field "proc" di payload) + airtime balasan.
  unsigned long pollRtt = millis() - pollStartTime;

  // Cetak blok JSON untuk dibaca app.py (data_hex dari node adalah murni
  // 125 karakter '0'/'1', diteruskan apa adanya; field QoS ditambahkan)
  Serial.println(F("{"));
  Serial.println(F("  \"event\": \"data_received\","));
  Serial.print(F("  \"rssi\": "));
  Serial.print(LoRa.packetRssi());
  Serial.println(F(","));
  Serial.print(F("  \"snr\": "));
  Serial.print(LoRa.packetSnr());
  Serial.println(F(","));
  Serial.print(F("  \"poll_rtt_ms\": "));
  Serial.print(pollRtt);
  Serial.println(F(","));
  Serial.print(F("  \"payload_len\": "));
  Serial.print(packetSize);
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
    lastReplyTime = millis();
    if (missCount[pollIndex] >= OFFLINE_AFTER_MISSES)
      Serial.println("-> Node" + String(POLL_NODES[pollIndex]) +
                     " KEMBALI ONLINE, masuk rotasi normal lagi.");
    missCount[pollIndex] = 0;
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
  configureLoRaRadio();

  Serial.println(F("Gateway LoRa Siap."));

  // Sync awal ke node, lalu langsung mulai listening
  broadcastTimeSync(true);
  currentMode = MODE_LISTENING;
  nextPollTime = millis();
  lastReplyTime = millis();
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
    // Pemulihan mandiri: 30 dtk tanpa satu balasan pun bisa berarti radio
    // gateway sendiri yang macet (bukan node) — reset & konfigurasi ulang.
    // Kalau ternyata semua node memang mati, re-init ini tidak merugikan.
    if (now - lastReplyTime > RADIO_STALL_MS) {
      Serial.println(F(">>> 30 dtk tanpa balasan dari node mana pun — "
                       "re-init radio LoRa (pemulihan mandiri)."));
      recoverLoRa();
      lastReplyTime = now;
      waitingForReply = false;
      nextPollTime = now + POLL_GAP;
    }

    if (!waitingForReply) {
      // Broadcast sync periodik saat idle (node yang restart bisa ikut lagi)
      if (now - lastSyncBroadcast > SYNC_INTERVAL) {
        broadcastTimeSync(false);
        // Beri jeda sebelum poll berikutnya: node butuh waktu memproses
        // paket sync dan kembali siap RX; poll yang dikirim beruntun di
        // iterasi yang sama bisa hilang dan berujung timeout palsu
        nextPollTime = millis() + POLL_GAP;
      }

      if (now >= nextPollTime)
        sendPoll();
    } else {
      if (now - pollStartTime > POLL_TIMEOUT) {
        Serial.println(">>> Timeout! Node" + String(POLL_NODES[pollIndex]) +
                       " tidak merespon.");
        // Event QoS satu baris: backend menghitung packet loss dari sini
        // (paket dianggap hilang di segmen Node -> Gateway)
        Serial.print(F("{\"event\":\"poll_timeout\",\"node\":\"Node"));
        Serial.print(POLL_NODES[pollIndex]);
        Serial.println(F("\"}"));
        Serial.println(F("--------------------------------"));
        bool wasOnline = missCount[pollIndex] < OFFLINE_AFTER_MISSES;
        if (missCount[pollIndex] < OFFLINE_AFTER_MISSES)
          missCount[pollIndex]++;
        if (missCount[pollIndex] >= OFFLINE_AFTER_MISSES) {
          probeDueAt[pollIndex] = now + OFFLINE_PROBE_MS;
          if (wasOnline)
            Serial.println(">>> Node" + String(POLL_NODES[pollIndex]) +
                           " ditandai OFFLINE; di-probe ulang tiap " +
                           String(OFFLINE_PROBE_MS / 1000) +
                           " dtk tanpa menahan node lain.");
        }
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
      lastReplyTime = now; // jangan langsung memicu pemulihan mandiri
      Serial.println(F("Mode: LISTENING. Mulai polling node..."));
      updateOLEDDisplay();
    }
  }
}