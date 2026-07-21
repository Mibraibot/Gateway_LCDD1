# Panduan Pengujian QoS End-to-End — Sistem LCDD

Dokumen ini menjelaskan cara mengukur, menganalisis, dan **menyajikan QoS
(Quality of Service) di setiap tahap** sistem Low Cost Drone Detection untuk
laporan skripsi: dari node (nRF24 + LoRa + ESP32) → gateway → backend
(keputusan) → Firebase → website, serta QoS keseluruhan (end-to-end).

---

## 1. Peta Segmen Pengukuran

```
┌─────────────┐  LoRa 433MHz   ┌─────────────┐  Serial USB  ┌──────────────┐
│ NODE ESP32  │ ─────────────► │  GATEWAY    │ ───────────► │  BACKEND     │
│ scan nRF24  │   SEGMEN 1     │  ESP32      │  (±30 ms,    │  app.py      │
│ 125 kanal   │  poll_rtt_ms   │             │  determinis) │  SEGMEN 2    │
└─────────────┘                └─────────────┘              │  decision_ms │
                                                            └──────┬───────┘
                                                                   │ HTTP PATCH
                                                                   │ SEGMEN 3
                                                                   ▼
┌─────────────┐   WebSocket    ┌──────────────────────────────────────────┐
│  WEBSITE    │ ◄───────────── │            FIREBASE RTDB                 │
│  (browser)  │   SEGMEN 4     │  detection_system/ Timeseries/ qos/      │
└─────────────┘   fb_to_fe_ms  └──────────────────────────────────────────┘
```

| Segmen | Jalur | Metrik utama | Diukur oleh | Cara ukur |
|---|---|---|---|---|
| 1 | Node → Gateway (LoRa) | delay (RTT), jitter, packet loss, throughput, RSSI, SNR | Gateway (`poll_rtt_ms`) + Node (`seq`, `proc`) | Jam lokal gateway: poll dikirim → balasan utuh diterima. Bebas masalah sinkronisasi jam antar perangkat. |
| 2 | Proses keputusan backend | `decision_ms` (komputasi murni), `backend_total_ms` (frame masuk → pipeline selesai) | Backend (`qos_log.csv`) | `time.perf_counter()` di sekitar blok analisis window z-score. |
| 3 | Backend → Firebase | `fb_raw_ms`, `fb_pred_ms` (RTT PATCH); `backend_to_fb_ms` (satu arah) | Backend + jam server Firebase | RTT HTTP diukur langsung; satu-arah dihitung dari `server_ts` (sentinel `.sv`) dikurangi waktu kirim backend terkoreksi offset. |
| 4 | Firebase → Website | `fb_to_fe_ms`, jitter | Browser (tab **QoS** di dashboard) | `.info/serverTimeOffset` menyamakan jam browser dengan jam server; latency = waktu tiba − `server_ts`. |
| E2E | Poll gateway → tampil di browser | delay, jitter, loss | Gabungan | `poll_rtt + (waktu tiba di browser − waktu frame masuk backend)`, semua pada referensi jam server. |

**Prinsip kunci lintas perangkat:** jam node/gateway/laptop/browser tidak
pernah dibandingkan langsung. Segmen 1 memakai satu jam (gateway). Segmen
3–4 memakai satu referensi bersama, yaitu **jam server Firebase**: browser
mendapat offsetnya dari `.info/serverTimeOffset`, backend mengestimasi
offsetnya dengan metode NTP sederhana (tulis sentinel timestamp server, baca
kembali, ambil titik tengah) dan mempublikasikannya ke `qos/meta`.

**Packet loss dua arah dibedakan:**
- **Timeout poll** (event `poll_timeout` dari gateway) = siklus gagal total.
- **Celah nomor urut `seq`** = node sempat mengirim balasan tetapi hilang di
  udara. Timeout **tanpa** celah seq berarti justru paket *poll* gateway yang
  tidak sampai ke node. Ini analisis yang bagus untuk pembahasan skripsi.

## 2. Definisi Metrik & Rumus

- **Delay rata-rata**: `D̄ = (1/N) Σ Dᵢ`
- **Jitter** (variasi delay): `J = (1/(N−1)) Σ |Dᵢ − Dᵢ₋₁|`
- **Packet loss**: `PL = (paket hilang / paket dikirim) × 100%`
- **Throughput**: `T = (Σ ukuran paket × 8) / durasi pengamatan` (bps)

Kategorisasi memakai **ETSI TIPHON TR 101 329** (standar yang lazim dipakai
skripsi QoS di Indonesia):

| Kategori | Indeks | Delay | Jitter | Packet Loss |
|---|---|---|---|---|
| Sangat Bagus | 4 | < 150 ms | 0 ms | 0 % |
| Bagus | 3 | 150–300 ms | < 75 ms | < 3 % |
| Sedang | 2 | 300–450 ms | 75–125 ms | < 15 % |
| Jelek | 1 | > 450 ms | 125–225 ms | < 25 % |

> **Penting untuk pembahasan:** TIPHON dirancang untuk layanan kelas VoIP.
> Delay segmen LoRa (±700 ms) didominasi *airtime* SF7 + waktu scan nRF24
> ±330 ms — itu **konsekuensi desain sensing**, bukan degradasi jaringan.
> Sajikan kategori TIPHON apa adanya, lalu jelaskan konteks ini; tunjukkan
> juga `link_delay` (RTT − proses node) yang memisahkan kedua komponen.

## 3. Instrumentasi yang Sudah Ditambahkan

| Komponen | Perubahan | Yang harus Anda lakukan |
|---|---|---|
| Node (`LCDD/src/main.cpp`) | Payload kini membawa `"seq"` (nomor urut) dan `"proc"` (ms scan+proses) | **Flash ulang ketiga node** (ubah `NODE_ID` seperti biasa) |
| Gateway (`src/main.cpp`) | Blok JSON serial kini membawa `"poll_rtt_ms"` dan `"payload_len"`; timeout dicetak sebagai event JSON `poll_timeout` | **Flash ulang gateway** |
| Backend (`backend/app.py`) | Menulis `qos_log.csv` (1 baris/frame + baris timeout), mengukur latency tiap PATCH Firebase, mempublikasikan record QoS ke `qos/live/{node}` + offset jam ke `qos/meta` | Jalankan seperti biasa: `python backend/app.py` |
| Website (repo `lowcostdrone`) | Tab **QoS** baru: monitor real-time per segmen + tombol **Export CSV** | Buka dashboard → tab QoS, biarkan terbuka selama pengujian |
| Analisis (`qos_analysis/analyze_qos.py`) | Menggabungkan CSV backend + frontend → laporan md, ringkasan csv, grafik png | Jalankan setelah pengujian selesai |

## 4. Prosedur Pengujian (SOP)

1. Flash firmware terbaru ke ketiga node dan gateway.
2. Nyalakan gateway, tunggu NTP sync, nyalakan node (tunggu "SYNC SUCCESS").
3. Di laptop: `python backend/app.py`. Pastikan muncul log
   `Offset jam backend vs Firebase: ±xx ms` (artinya koreksi jam aktif).
4. Buka dashboard website → tab **QoS**. Pastikan peringatan "offset jam
   backend belum diterima" TIDAK muncul.
5. Klik **Reset** di tab QoS tepat saat mulai skenario (agar sampel bersih).
6. Jalankan skenario minimal **30 menit atau ±500 siklus** (mengikuti pola
   `record_3nodes.py` yang sudah Anda pakai — jumlah sampel besar membuat
   rata-rata stabil dan bisa dipertanggungjawabkan di sidang).
7. Selesai skenario: klik **Export CSV** di tab QoS → dapat
   `qos_frontend_<waktu>.csv`. Hentikan backend → `qos_log.csv` tersimpan.
   **Ganti nama** kedua file sesuai skenario (mis. `qos_log_wifiramai.csv`).
8. Analisis:
   ```bash
   python qos_analysis/analyze_qos.py \
       --backend qos_log_wifiramai.csv \
       --frontend qos_frontend_wifiramai.csv \
       --label wifiramai
   ```
9. Hasil di folder `hasil_qos_wifiramai_<waktu>/`:
   `laporan_qos.md` (tabel siap salin), `ringkasan_qos.csv` (untuk Excel),
   `grafik_*.png` (siap tempel di laporan).

## 4b. Uji per Segmen Secara Terpisah (`uji_qos_segmen.py`)

Selain SOP lengkap di atas, tiap segmen bisa diuji **sendiri-sendiri** dengan
runner `qos_analysis/uji_qos_segmen.py` — berguna saat ingin mengisolasi
masalah atau saat tidak semua komponen tersedia:

| Perintah | Menguji | Kebutuhan |
|---|---|---|
| `python qos_analysis/uji_qos_segmen.py segmen1 --durasi 300 --label jarak20m` | Node→Gateway: RTT, RSSI/SNR, loss, dari serial gateway langsung | Gateway + node menyala (backend **jangan** jalan bersamaan — rebutan port serial) |
| `python qos_analysis/uji_qos_segmen.py segmen2 --sumber dataset_x.json` | Pipeline keputusan: replay dataset rekaman melalui kalibrasi+deteksi, ukur `decision_ms` | Tidak butuh alat (offline) |
| `python qos_analysis/uji_qos_segmen.py segmen2 --sumber qos_log.csv` | Rangkuman `decision_ms`/`backend_total_ms` dari pengukuran live | File `qos_log.csv` |
| `python qos_analysis/uji_qos_segmen.py segmen3 --jumlah 100` | Backend→Firebase: probe PATCH beban nyata, RTT + estimasi satu-arah | Internet saja |
| `python qos_analysis/uji_qos_segmen.py segmen4 --durasi 300` | Firebase→penerima realtime (simulasi frontend via Python SSE); keluaran kompatibel `--frontend` di analyze_qos.py | Backend + alat berjalan |
| `python qos_analysis/uji_qos_segmen.py loopback --jumlah 50` | Jalur Firebase terisolasi (naik + turun), tanpa alat sama sekali | Internet saja |

Keluaran `segmen1` memakai format kolom `qos_log.csv` dan keluaran `segmen4`
memakai format export tab QoS website, jadi keduanya bisa langsung dianalisis:
`python qos_analysis/analyze_qos.py --backend uji_segmen1_*.csv ...`.
Mode `segmen3/4/loopback` hanya menulis ke path uji `qos/uji/*` — data
deteksi produksi tidak disentuh. Untuk laporan, angka segmen 4 dari browser
sungguhan (tab QoS → Export CSV) tetap jadi data utama; mode `segmen4`
Python adalah pembanding/otomasinya.

## 5. Matriks Skenario yang Disarankan

Sesuai arahan dosen ("tidak cukup hanya RF; modul lain, QoS, website,
komunikasi GW juga diuji"), minimal ulangi pengukuran QoS pada skenario:

| # | Skenario | Variabel yang diuji | Yang diharapkan terlihat |
|---|---|---|---|
| 1 | Baseline: 1 node, dekat (±1 m), lingkungan sepi | referensi | delay/loss terbaik |
| 2 | 3 node aktif round-robin | beban polling gateway | siklus penuh ±3× lebih lama |
| 3 | Jarak bertingkat (5 m, 20 m, 50 m, …) | link LoRa (RSSI/SNR vs delay/loss) | korelasi RSSI ↔ loss |
| 4 | Lingkungan WiFi ramai vs sepi | interferensi terhadap scan & WiFi backend | loss segmen 1 & latency segmen 3 |
| 5 | Dengan drone aktif (deteksi berjalan) | jalur keputusan lengkap | `decision_ms`, `fb_pred_ms` terisi |
| 6 | Koneksi internet backend berbeda (WiFi kampus / hotspot HP) | segmen 3–4 | latency Firebase berubah |
| 7 | Node dimatikan di tengah uji | mekanisme timeout/backoff gateway | loss & recovery tercatat |

Setiap skenario menghasilkan satu folder `hasil_qos_*` — tabel TIPHON
antar-skenario itulah inti bab pengujian Anda.

## 6. Cara Menyajikan di Laporan Skripsi

Struktur yang disarankan untuk bab Pengujian & Analisis:

1. **Metodologi pengukuran** — salin diagram peta segmen (Bagian 1), jelaskan
   titik ukur & cara menangani perbedaan jam antar perangkat (Bagian 1),
   definisi metrik & rumus (Bagian 2). Ini menjawab pertanyaan penguji
   "diukur di mana dan bagaimana caranya".
2. **Skenario pengujian** — tabel matriks skenario (Bagian 5) + foto/denah
   penempatan node.
3. **Hasil per segmen** — untuk tiap segmen: tabel statistik (N, rata-rata,
   min, maks, stdev, jitter) dari `laporan_qos.md`, lalu satu grafik:
   - Segmen 1: `grafik_delay_timeseries.png` + tabel RSSI/SNR + `grafik_loss_per_node.png`
   - Segmen 2: tabel `decision_ms` (tunjukkan bahwa komputasi < 1 ms —
     bukti backend bukan bottleneck)
   - Segmen 3–4: tabel latency Firebase naik/turun
4. **QoS end-to-end** — `grafik_delay_per_segmen.png` (menunjukkan segmen
   mana yang dominan) + `grafik_e2e_cdf.png` + tabel TIPHON rekap.
5. **Perbandingan antar skenario** — satu tabel: baris = skenario, kolom =
   delay E2E, jitter, loss, kategori TIPHON. Data diambil dari
   `ringkasan_qos.csv` tiap skenario (gabungkan di Excel).
6. **Pembahasan** — jelaskan komposisi delay (scan nRF ±330 ms + airtime
   LoRa ±330 ms + Firebase ±200–400 ms), arah packet loss (poll vs balasan,
   dari analisis celah `seq`), dan konteks kategori TIPHON (Bagian 2).

## 7. Batasan Pengukuran (tulis di laporan agar jujur & kuat di sidang)

- Delay serial gateway→laptop (±26 ms untuk ±300 byte pada 115200 baud)
  tidak terukur terpisah karena jam gateway dan laptop berbeda; nilainya
  deterministik dan kecil, dicantumkan sebagai estimasi.
- Latency satu-arah segmen 3 bergantung akurasi estimasi offset jam backend
  (±½ RTT probe, biasanya ±50–150 ms); RTT PATCH (`fb_raw_ms`) adalah
  ukuran yang paling pasti untuk segmen ini.
- Tab QoS membaca record `qos/live` terbaru per node; jika dua frame tiba
  hampir bersamaan, browser bisa melewatkan satu record (coalescing
  WebSocket) — kehilangan sampel di frontend ≠ packet loss jaringan.
- Keputusan deteksi baru keluar setiap `STRIDE` frame setelah window penuh
  (`WINDOW_SIZE`); jeda agregasi ini adalah parameter desain deteksi dan
  dianalisis terpisah dari delay jaringan.
