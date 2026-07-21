import os
import json
import time
import re
import serial
import serial.tools.list_ports
import statistics
import threading
import urllib.request

FIREBASE_URL = "https://lowcostdronedetect-default-rtdb.asia-southeast1.firebasedatabase.app"

# ==============================================================
# CATATAN SINKRONISASI DENGAN WEBSITE (lowcostdrone dashboard)
# --------------------------------------------------------------
# app.py sekarang SATU-SATUNYA penulis ke Firebase:
#
# 1) detection_system/{node1|node2|node3}   <- dibaca website
#    - data mentah: data_hex, rssi, snr, timestamp_wib, captured_at
#    - hasil deteksi: prediction_id (0=AMAN, 1=DRONE),
#      prediction_label, prediction_time
#
# 2) Timeseries/{Node1|Node2|Node3}
#    - prediction: "Drone Terdeteksi" / "Aman"
#    - timestamp : "YYYY-MM-DD HH:MM:SS"
#
# 3) qos/live/{node1|node2|node3}  <- panel QoS di website
#    - satu record QoS per frame: seq, rssi, snr, poll_rtt_ms,
#      node_proc_ms, link_delay_ms, decision_ms, fb_raw_ms,
#      backend_rx_at, backend_sent_at, server_ts (jam server Firebase),
#      rx_count, timeout_count, loss_pct
#    qos/meta -> backend_offset_ms (selisih jam laptop backend
#    terhadap jam server Firebase, untuk koreksi lintas-perangkat)
# ==============================================================

def firebase_patch(path, data):
    """PATCH ke Firebase. Return (ok, latency_ms) — latency dipakai
    sebagai metrik QoS segmen Backend -> Firebase (RTT HTTP penuh)."""
    url = f"{FIREBASE_URL}/{path}.json"
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="PATCH")
        req.add_header('Content-Type', 'application/json')
        jsondata = json.dumps(data).encode('utf-8')
        req.add_header('Content-Length', len(jsondata))
        urllib.request.urlopen(req, jsondata, timeout=4)
        return True, (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        print(f"-> Gagal PATCH {path}: {e}")
        return False, (time.perf_counter() - t0) * 1000.0

def send_raw_to_firebase(node_key, data_hex, timestamp_wib, rssi, snr):
    """Kirim data mentah tiap frame -> kartu node & hex viewer di web hidup.
    Return (ok, latency_ms) untuk dicatat di log QoS."""
    ok, ms = firebase_patch(f"detection_system/{node_key}", {
        "node": node_key,
        "data_hex": data_hex,
        "timestamp_wib": timestamp_wib,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rssi": rssi,
        "snr": snr,
    })
    if ok:
        print(f"-> Raw frame {node_key} -> detection_system (rssi={rssi}, snr={snr}, {ms:.0f} ms)")
    return ok, ms

def send_prediction_to_firebase(node_id, node_key, is_drone):
    """Kirim hasil deteksi ke dua path: web (detection_system) + arsip (Timeseries).
    Return (ok, latency_ms) — latency total kedua PATCH (jalur publikasi keputusan)."""
    pred_id = 1 if is_drone else 0
    label = "DRONE TERDETEKSI" if is_drone else "AMAN"

    ok1, ms1 = firebase_patch(f"detection_system/{node_key}", {
        "prediction_id": pred_id,
        "prediction_label": label,
        "prediction_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    ok2, ms2 = firebase_patch(f"Timeseries/{node_id}", {
        "prediction": "Drone Terdeteksi" if is_drone else "Aman",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    print(f"-> Prediksi {node_key} terkirim (id={pred_id}, {label}, {ms1 + ms2:.0f} ms)")
    return (ok1 and ok2), ms1 + ms2

# ==============================================================
# INSTRUMENTASI QoS
# --------------------------------------------------------------
# Semua pengukuran QoS backend dicatat ke qos_log.csv (satu baris
# per frame / per timeout) dan dipublikasikan ke Firebase (qos/live)
# agar website bisa mengukur segmen Firebase -> Frontend memakai
# jam server yang sama. Analisis akhir: qos_analysis/analyze_qos.py
# ==============================================================

QOS_CSV = "qos_log.csv"
QOS_CSV_HEADER = ("t_rx_iso,t_rx_epoch_ms,event,node,seq,rssi,snr,"
                  "poll_rtt_ms,node_proc_ms,link_delay_ms,payload_len,"
                  "fb_raw_ms,fb_raw_ok,decision,decision_ms,fb_pred_ms,"
                  "fb_pred_ok,fb_qos_ms,backend_total_ms,"
                  "rx_count,timeout_count,loss_pct\n")

backend_offset_ms = 0.0   # jam server Firebase - jam laptop (estimasi)
rx_count = {}             # frame data diterima per node
timeout_count = {}        # poll timeout per node

def log_qos_csv(row):
    """Tulis satu baris QoS ke CSV (header dibuat otomatis)."""
    try:
        new_file = not os.path.exists(QOS_CSV)
        with open(QOS_CSV, "a", encoding="utf-8") as f:
            if new_file:
                f.write(QOS_CSV_HEADER)
            f.write(",".join("" if v is None else str(v) for v in row) + "\n")
    except Exception as e:
        print(f"-> Gagal menulis {QOS_CSV}: {e}")

def loss_pct_of(node_id):
    total = rx_count.get(node_id, 0) + timeout_count.get(node_id, 0)
    if total == 0:
        return 0.0
    return round(100.0 * timeout_count.get(node_id, 0) / total, 2)

def send_qos_to_firebase(node_key, record):
    """Publikasikan record QoS frame ini ke qos/live/{node}.
    server_ts diisi jam server Firebase (sentinel .sv) sehingga website
    dapat menghitung latency Backend->Firebase dan Firebase->Frontend
    terhadap referensi jam yang sama."""
    record["server_ts"] = {".sv": "timestamp"}
    ok, ms = firebase_patch(f"qos/live/{node_key}", record)
    return ok, ms

def estimate_clock_offset_loop():
    """Thread latar: estimasi offset jam laptop terhadap jam server Firebase.
    Metode NTP sederhana: tulis sentinel timestamp server (PUT), baca nilai
    yang tertulis, offset = server_ts - titik tengah (t0, t1). Dipublikasikan
    ke qos/meta agar website bisa mengoreksi timestamp backend."""
    global backend_offset_ms
    while True:
        try:
            url = f"{FIREBASE_URL}/qos/clock_probe.json"
            t0 = time.time() * 1000.0
            req = urllib.request.Request(url, method="PUT")
            req.add_header('Content-Type', 'application/json')
            body = json.dumps({".sv": "timestamp"}).encode('utf-8')
            resp = urllib.request.urlopen(req, body, timeout=4)
            server_ts = float(json.loads(resp.read().decode('utf-8')))
            t1 = time.time() * 1000.0
            backend_offset_ms = server_ts - (t0 + t1) / 2.0
            firebase_patch("qos/meta", {
                "backend_offset_ms": round(backend_offset_ms, 1),
                "probe_rtt_ms": round(t1 - t0, 1),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            print(f"-> Offset jam backend vs Firebase: {backend_offset_ms:+.1f} ms (RTT probe {t1 - t0:.0f} ms)")
        except Exception as e:
            print(f"-> Gagal estimasi offset jam: {e}")
        time.sleep(60)

# ==============================================================
# KONFIGURASI DETEKSI ADAPTIF (Z-SCORE)
# --------------------------------------------------------------
WINDOW_SIZE = 5
STRIDE = 2
CALIB_SAMPLES = 20

# Aturan Empiris
# K_FACTOR: Pengali standar deviasi. 3.0 berarti mentoleransi 
# fluktuasi noise hingga 99.7% dari variansi normal lingkungan.
K_FACTOR = 3.0

# MIN_STDEV: Pengaman batas bawah agar threshold tidak terjepit ke 0
# jika lingkungan kalibrasi terlalu sepi/statis.
MIN_STDEV_BURST = 0.5
MIN_STDEV_ACTIVE = 1.0
# ==============================================================

LOG_CSV = True
CSV_FILE = "fitur_log.csv"

data_windows = {}
stride_counter = {}
calib_pool = {}
frozen = {}

def get_burst_count(bits):
    """Jumlah grup kanal aktif kontigu (transisi 0->1) pada vektor biner."""
    count = 0
    prev = 0
    for b in bits:
        if b == 1 and prev == 0:
            count += 1
        prev = b
    return count

def hex_to_bits(hex_str):
    """Binarisasi: kanal dengan nilai hex >= 1 dianggap aktif."""
    bits = []
    for char in hex_str:
        try:
            bits.append(1 if int(char, 16) >= 1 else 0)
        except ValueError:
            bits.append(0)
    return bits

def auto_detect_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        d = port.device.lower()
        if "usbserial" in d or "usbmodem" in d or "ttyusb" in d or "ttyacm" in d:
            return port.device
    if ports:
        return ports[0].device
    return None

def log_csv(node_id, burst_mean, act_mean, base_b, base_a, pred, fase):
    if not LOG_CSV:
        return
    try:
        new_file = not os.path.exists(CSV_FILE)
        with open(CSV_FILE, "a", encoding="utf-8") as f:
            if new_file:
                f.write("timestamp,node,burst_count_mean,active_ch_mean,baseline_burst,baseline_active,prediksi,fase\n")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{node_id},{burst_mean:.3f},{act_mean:.3f},{base_b:.3f},{base_a:.3f},{pred},{fase}\n")
    except Exception as e:
        print(f"-> Gagal menulis CSV: {e}")

def read_from_port(ser):
    global data_windows, stride_counter, calib_pool, frozen

    current_node = "Unknown"
    current_rssi = 0
    current_snr = 0.0
    current_ts_wib = "00:00:00"
    current_poll_rtt = None   # RTT poll->balasan dari gateway (ms, jam gateway)
    current_payload_len = None  # ukuran paket LoRa (byte) utk hitung throughput

    print(f"FASE 1: KALIBRASI DINAMIS - {CALIB_SAMPLES} frame pertama per node, pastikan TANPA drone!")
    print(f"FASE 2: DETEKSI ADAPTIF berjalan (Window={WINDOW_SIZE}, Stride={STRIDE}).")
    print("Baseline & Threshold Adaptif TIDAK berubah sampai program di-restart (SOP lokasi baru = restart).")

    while True:
        try:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').strip()

                rssi_match = re.search(r'"rssi"\s*:\s*(-?\d+)', line)
                if rssi_match and '"data_hex"' not in line:
                    current_rssi = int(rssi_match.group(1))

                snr_match = re.search(r'"snr"\s*:\s*(-?\d+\.?\d*)', line)
                if snr_match and '"data_hex"' not in line:
                    current_snr = float(snr_match.group(1))

                rtt_match = re.search(r'"poll_rtt_ms"\s*:\s*(\d+)', line)
                if rtt_match and '"data_hex"' not in line:
                    current_poll_rtt = int(rtt_match.group(1))

                plen_match = re.search(r'"payload_len"\s*:\s*(\d+)', line)
                if plen_match and '"data_hex"' not in line:
                    current_payload_len = int(plen_match.group(1))

                # ====== EVENT QoS: POLL TIMEOUT (paket hilang Node->GW) ======
                if '"poll_timeout"' in line:
                    to_match = re.search(r'"node"\s*:\s*"([^"]+)"', line)
                    if to_match:
                        to_node = to_match.group(1)
                        timeout_count[to_node] = timeout_count.get(to_node, 0) + 1
                        t_rx = time.time()
                        print(f"-> [QoS] Poll timeout {to_node} "
                              f"(loss sementara: {loss_pct_of(to_node):.1f}%)")
                        log_qos_csv([
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_rx)),
                            int(t_rx * 1000), "timeout", to_node,
                            None, None, None, None, None, None, None,
                            None, None, None, None, None, None, None, None,
                            rx_count.get(to_node, 0),
                            timeout_count.get(to_node, 0),
                            loss_pct_of(to_node),
                        ])
                    continue

                node_match = re.search(r'"node"\s*:\s*"([^"]+)"', line)
                if node_match:
                    current_node = node_match.group(1)

                ts_match = re.search(r'"timestamp_wib"\s*:\s*"([^"]+)"', line)
                if ts_match:
                    current_ts_wib = ts_match.group(1)

                # seq & proc dikirim node di dalam payload (baris data_hex)
                seq_match = re.search(r'"seq"\s*:\s*(\d+)', line)
                current_seq = int(seq_match.group(1)) if seq_match else None
                proc_match = re.search(r'"proc"\s*:\s*(\d+)', line)
                current_proc = int(proc_match.group(1)) if proc_match else None

                match = re.search(r'"data_hex"\s*:\s*"([0-9a-fA-F]+)"', line)
                if match:
                    # Titik ukur QoS: frame utuh tiba di backend (jam laptop)
                    t_rx = time.time()
                    hex_str = match.group(1)
                    node_id = current_node
                    node_key = node_id.lower()

                    if len(hex_str) == 125:
                        spectrum_bits = hex_to_bits(hex_str)

                        if node_id not in calib_pool:
                            calib_pool[node_id] = []
                            data_windows[node_id] = []
                            stride_counter[node_id] = 0

                        rx_count[node_id] = rx_count.get(node_id, 0) + 1

                        # Delay link LoRa murni = RTT gateway - waktu proses node
                        # (RTT & proc sama-sama diukur satu jam lokal, bebas skew)
                        link_delay = None
                        if current_poll_rtt is not None and current_proc is not None:
                            link_delay = current_poll_rtt - current_proc

                        # SINKRON WEB (latency PATCH = QoS Backend -> Firebase)
                        fb_raw_ok, fb_raw_ms = send_raw_to_firebase(
                            node_key, hex_str, current_ts_wib,
                            current_rssi, current_snr)

                        frame_burst = get_burst_count(spectrum_bits)
                        frame_active = sum(spectrum_bits)

                        # Hasil keputusan frame ini (kosong bila frame ini tidak
                        # menghasilkan keputusan: kalibrasi / window belum penuh)
                        decision = None
                        decision_ms = None
                        fb_pred_ok = None
                        fb_pred_ms = None
                        fase = "deteksi"

                        # ====== FASE 1: KALIBRASI DINAMIS ======
                        if node_id not in frozen:
                            fase = "kalibrasi"
                            pool = calib_pool[node_id]
                            pool.append((frame_burst, frame_active))
                            n = len(pool)
                            print(f"[KALIBRASI {node_id}] frame {n}/{CALIB_SAMPLES} | burst={frame_burst} | active={frame_active}")
                            log_csv(node_id, frame_burst, frame_active, 0, 0, 0, "kalibrasi")

                            if n == CALIB_SAMPLES:
                                # Ekstrak nilai burst dan active ke dalam list terpisah
                                burst_list = [p[0] for p in pool]
                                active_list = [p[1] for p in pool]

                                # Hitung Nilai Rata-Rata (Mean)
                                mean_b = statistics.mean(burst_list)
                                mean_a = statistics.mean(active_list)

                                # Hitung Standar Deviasi (StDev) dengan batas minimum
                                std_b = max(MIN_STDEV_BURST, statistics.stdev(burst_list))
                                std_a = max(MIN_STDEV_ACTIVE, statistics.stdev(active_list))

                                # Hitung Threshold Adaptif: Mean + (K * StDev)
                                thr_burst = mean_b + (K_FACTOR * std_b)
                                thr_active = mean_a + (K_FACTOR * std_a)

                                frozen[node_id] = {
                                    "baseline_burst": mean_b,
                                    "baseline_active": mean_a,
                                    "std_burst": std_b,
                                    "std_active": std_a,
                                    "thr_burst": thr_burst,
                                    "thr_active": thr_active,
                                }
                                fz = frozen[node_id]

                                print("\n" + "#"*60)
                                print(f"KALIBRASI {node_id} SELESAI - BASELINE ADAPTIF DIKUNCI")
                                print(f"Rata-Rata Lingkungan: burst={mean_b:.2f} | active={mean_a:.2f}")
                                print(f"Standar Deviasi (σ) : burst={std_b:.2f} | active={std_a:.2f}")
                                print(f"Threshold (Mean+3σ) : burst={fz['thr_burst']:.2f} | active={fz['thr_active']:.2f}")
                                print(f"Mulai FASE DETEKSI dengan Window={WINDOW_SIZE}, Stride={STRIDE}.")
                                print("#"*60 + "\n")

                        # ====== FASE 2: DETEKSI ======
                        else:
                            data_windows[node_id].append(spectrum_bits)
                            if len(data_windows[node_id]) > WINDOW_SIZE:
                                data_windows[node_id].pop(0)

                            stride_counter[node_id] += 1
                            print(f"-> Data Hex diterima dari {node_id} ({len(data_windows[node_id])}/{WINDOW_SIZE})")

                            if len(data_windows[node_id]) == WINDOW_SIZE and stride_counter[node_id] >= STRIDE:
                                stride_counter[node_id] = 0
                                window_data = data_windows[node_id]

                                # Titik ukur QoS: lama komputasi keputusan murni
                                t_dec0 = time.perf_counter()

                                burst_counts = [get_burst_count(row) for row in window_data]
                                burst_count_mean = sum(burst_counts) / WINDOW_SIZE
                                active_ch_mean = sum([sum(row) for row in window_data]) / WINDOW_SIZE

                                fz = frozen[node_id]
                                thr_burst_eff = fz["thr_burst"]
                                thr_active_eff = fz["thr_active"]

                                cond_burst = burst_count_mean >= thr_burst_eff
                                cond_active = active_ch_mean >= thr_active_eff
                                predicted_as_drone = cond_burst or cond_active

                                decision_ms = round((time.perf_counter() - t_dec0) * 1000.0, 3)
                                decision = "DRONE" if predicted_as_drone else "AMAN"

                                status = "DRONE TERDETEKSI !!!" if predicted_as_drone else "AMAN (Tidak ada Drone)"

                                print("\n" + "="*60)
                                print(f"[{time.strftime('%H:%M:%S')}] ANALISIS TIME-SERIES LOKASI: {node_id}")
                                print(f"Baseline (Mean): burst={fz['baseline_burst']:.2f} | active={fz['baseline_active']:.2f}  (TERKUNCI)")
                                print(f"Burst Mean     : {burst_count_mean:.2f} \t(Threshold: {thr_burst_eff:.2f}) \t{'[V]' if cond_burst else '[ ]'}")
                                print(f"Active Mean    : {active_ch_mean:.2f} \t(Threshold: {thr_active_eff:.2f}) \t{'[V]' if cond_active else '[ ]'}")
                                print(f"Aturan Deteksi : OR - minimal satu parameter melewati Threshold Adaptif")
                                print(f"-> KEPUTUSAN   : {status} DI LOKASI {node_id}")
                                print(f"-> Lama komputasi keputusan: {decision_ms:.3f} ms")
                                print("="*60 + "\n")

                                log_csv(node_id, burst_count_mean, active_ch_mean,
                                        fz["baseline_burst"], fz["baseline_active"],
                                        1 if predicted_as_drone else 0, "deteksi")

                                # SINKRON WEB + ARSIP (latency PATCH keputusan)
                                fb_pred_ok, fb_pred_ms = send_prediction_to_firebase(
                                    node_id, node_key, predicted_as_drone)

                        # ====== CATAT & PUBLIKASIKAN QoS FRAME INI ======
                        # backend_sent_at = keluar dari pipeline backend; selisih
                        # dengan backend_rx_at = total waktu proses backend/frame
                        backend_sent_at = time.time() * 1000.0
                        qos_record = {
                            "node": node_id,
                            "event": fase,
                            "seq": current_seq,
                            "rssi": current_rssi,
                            "snr": current_snr,
                            "poll_rtt_ms": current_poll_rtt,
                            "node_proc_ms": current_proc,
                            "link_delay_ms": link_delay,
                            "payload_len": current_payload_len,
                            "fb_raw_ms": round(fb_raw_ms, 1),
                            "decision": decision if decision is not None else "",
                            "decision_ms": decision_ms,
                            "fb_pred_ms": round(fb_pred_ms, 1) if fb_pred_ms is not None else None,
                            "backend_rx_at": int(t_rx * 1000),
                            "backend_sent_at": int(backend_sent_at),
                            "rx_count": rx_count.get(node_id, 0),
                            "timeout_count": timeout_count.get(node_id, 0),
                            "loss_pct": loss_pct_of(node_id),
                        }
                        fb_qos_ok, fb_qos_ms = send_qos_to_firebase(node_key, qos_record)

                        log_qos_csv([
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_rx)),
                            int(t_rx * 1000), fase, node_id, current_seq,
                            current_rssi, current_snr, current_poll_rtt,
                            current_proc, link_delay, current_payload_len,
                            round(fb_raw_ms, 1), int(fb_raw_ok),
                            decision if decision is not None else "",
                            decision_ms,
                            round(fb_pred_ms, 1) if fb_pred_ms is not None else None,
                            int(fb_pred_ok) if fb_pred_ok is not None else None,
                            round(fb_qos_ms, 1),
                            round(time.time() * 1000 - t_rx * 1000, 1),
                            rx_count.get(node_id, 0),
                            timeout_count.get(node_id, 0),
                            loss_pct_of(node_id),
                        ])
            else:
                time.sleep(0.01)
        except Exception as e:
            print(f"Serial reading error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    # Thread estimasi offset jam laptop vs server Firebase (untuk QoS
    # lintas-perangkat: Backend -> Firebase -> Frontend memakai jam server)
    threading.Thread(target=estimate_clock_offset_loop, daemon=True).start()

    port_name = auto_detect_port()
    if not port_name:
        print("Warning: No serial port detected! Please plug in your ESP32 Gateway.")
    else:
        print(f"Connecting to serial port: {port_name} at 115200 baud...")
        try:
            ser = serial.Serial(port_name, 115200, timeout=1)
            print("Serial connection established successfully. Waiting for data...")
            read_from_port(ser)
        except Exception as e:
            print(f"Failed to open serial port: {e}")