import os
import json
import time
import re
import serial
import serial.tools.list_ports
import statistics
import urllib.request

FIREBASE_URL = "https://lowcostdronedetect-default-rtdb.asia-southeast1.firebasedatabase.app"

# ==============================================================
# CATATAN SINKRONISASI DENGAN WEBSITE (lowcostdrone dashboard)
# --------------------------------------------------------------
# app.py sekarang SATU-SATUNYA penulis ke Firebase:
#
# 1) detection_system/{node1|node2|node3}   <- dibaca website
#    - data mentah: data_hex, rssi, snr, timestamp_wib, captured_at
#    - hasil deteksi: detection_id (0=AMAN, 1=DRONE),
#      detection_label, detection_time
#
# 2) Timeseries/{Node1|Node2|Node3}
#    - detection: "Drone Terdeteksi" / "Aman"
#    - timestamp : "YYYY-MM-DD HH:MM:SS"
# ==============================================================

def firebase_patch(path, data):
    url = f"{FIREBASE_URL}/{path}.json"
    try:
        req = urllib.request.Request(url, method="PATCH")
        req.add_header('Content-Type', 'application/json')
        jsondata = json.dumps(data).encode('utf-8')
        req.add_header('Content-Length', len(jsondata))
        urllib.request.urlopen(req, jsondata, timeout=4)
        return True
    except Exception as e:
        print(f"-> Gagal PATCH {path}: {e}")
        return False

def send_raw_to_firebase(node_key, data_hex, timestamp_wib, rssi, snr):
    """Kirim data mentah tiap frame -> kartu node & hex viewer di web hidup."""
    ok = firebase_patch(f"detection_system/{node_key}", {
        "node": node_key,
        "data_hex": data_hex,
        "timestamp_wib": timestamp_wib,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rssi": rssi,
        "snr": snr,
    })
    if ok:
        print(f"-> Raw frame {node_key} -> detection_system (rssi={rssi}, snr={snr})")

def send_detection_to_firebase(node_id, node_key, is_drone):
    """Kirim hasil deteksi ke dua path: web (detection_system) + arsip (Timeseries)."""
    det_id = 1 if is_drone else 0
    label = "DRONE TERDETEKSI" if is_drone else "AMAN"

    firebase_patch(f"detection_system/{node_key}", {
        "detection_id": det_id,
        "detection_label": label,
        "detection_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    firebase_patch(f"Timeseries/{node_id}", {
        "detection": "Drone Terdeteksi" if is_drone else "Aman",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    print(f"-> Deteksi {node_key} terkirim (id={det_id}, {label})")

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

def bits_from_payload(data_str):
    """Ubah string biner '0'/'1' dari node menjadi list int untuk analisis."""
    return [1 if c == '1' else 0 for c in data_str]

def auto_detect_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        d = port.device.lower()
        if "usbserial" in d or "usbmodem" in d or "ttyusb" in d or "ttyacm" in d:
            return port.device
    if ports:
        return ports[0].device
    return None

def log_csv(node_id, burst_mean, act_mean, base_b, base_a, hasil, fase):
    if not LOG_CSV:
        return
    try:
        new_file = not os.path.exists(CSV_FILE)
        with open(CSV_FILE, "a", encoding="utf-8") as f:
            if new_file:
                f.write("timestamp,node,burst_count_mean,active_ch_mean,baseline_burst,baseline_active,hasil_deteksi,fase\n")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{node_id},{burst_mean:.3f},{act_mean:.3f},{base_b:.3f},{base_a:.3f},{hasil},{fase}\n")
    except Exception as e:
        print(f"-> Gagal menulis CSV: {e}")

def read_from_port(ser):
    global data_windows, stride_counter, calib_pool, frozen

    current_node = "Unknown"
    current_rssi = 0
    current_snr = 0.0
    current_ts_wib = "00:00:00"

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

                node_match = re.search(r'"node"\s*:\s*"([^"]+)"', line)
                if node_match:
                    current_node = node_match.group(1)

                ts_match = re.search(r'"timestamp_wib"\s*:\s*"([^"]+)"', line)
                if ts_match:
                    current_ts_wib = ts_match.group(1)

                match = re.search(r'"data_hex"\s*:\s*"([0-9a-fA-F]+)"', line)
                if match:
                    data_str = match.group(1)
                    node_id = current_node
                    node_key = node_id.lower()

                    if len(data_str) == 125:
                        spectrum_bits = bits_from_payload(data_str)

                        if node_id not in calib_pool:
                            calib_pool[node_id] = []
                            data_windows[node_id] = []
                            stride_counter[node_id] = 0

                        # SINKRON WEB
                        send_raw_to_firebase(node_key, data_str, current_ts_wib,
                                             current_rssi, current_snr)

                        frame_burst = get_burst_count(spectrum_bits)
                        frame_active = sum(spectrum_bits)

                        # ====== FASE 1: KALIBRASI DINAMIS ======
                        if node_id not in frozen:
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
                            continue

                        # ====== FASE 2: DETEKSI ======
                        data_windows[node_id].append(spectrum_bits)
                        if len(data_windows[node_id]) > WINDOW_SIZE:
                            data_windows[node_id].pop(0)

                        stride_counter[node_id] += 1
                        print(f"-> Data Hex diterima dari {node_id} ({len(data_windows[node_id])}/{WINDOW_SIZE})")

                        if len(data_windows[node_id]) == WINDOW_SIZE and stride_counter[node_id] >= STRIDE:
                            stride_counter[node_id] = 0
                            window_data = data_windows[node_id]

                            burst_counts = [get_burst_count(row) for row in window_data]
                            burst_count_mean = sum(burst_counts) / WINDOW_SIZE
                            active_ch_mean = sum([sum(row) for row in window_data]) / WINDOW_SIZE

                            fz = frozen[node_id]
                            thr_burst_eff = fz["thr_burst"]
                            thr_active_eff = fz["thr_active"]

                            cond_burst = burst_count_mean >= thr_burst_eff
                            cond_active = active_ch_mean >= thr_active_eff
                            detected_as_drone = cond_burst or cond_active
                            status = "DRONE TERDETEKSI !!!" if detected_as_drone else "AMAN (Tidak ada Drone)"

                            print("\n" + "="*60)
                            print(f"[{time.strftime('%H:%M:%S')}] ANALISIS TIME-SERIES LOKASI: {node_id}")
                            print(f"Baseline (Mean): burst={fz['baseline_burst']:.2f} | active={fz['baseline_active']:.2f}  (TERKUNCI)")
                            print(f"Burst Mean     : {burst_count_mean:.2f} \t(Threshold: {thr_burst_eff:.2f}) \t{'[V]' if cond_burst else '[ ]'}")
                            print(f"Active Mean    : {active_ch_mean:.2f} \t(Threshold: {thr_active_eff:.2f}) \t{'[V]' if cond_active else '[ ]'}")
                            print(f"Aturan Deteksi : OR - minimal satu parameter melewati Threshold Adaptif")
                            print(f"-> KEPUTUSAN   : {status} DI LOKASI {node_id}")
                            print("="*60 + "\n")

                            log_csv(node_id, burst_count_mean, active_ch_mean,
                                    fz["baseline_burst"], fz["baseline_active"],
                                    1 if detected_as_drone else 0, "deteksi")

                            # SINKRON WEB + ARSIP
                            send_detection_to_firebase(node_id, node_key, detected_as_drone)
            else:
                time.sleep(0.01)
        except Exception as e:
            print(f"Serial reading error: {e}")
            time.sleep(1)

if __name__ == "__main__":
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