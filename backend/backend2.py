import os
import json
import time
import re
import serial
import serial.tools.list_ports
import statistics
import urllib.request

FIREBASE_URL = "https://lowcostdronedetect-default-rtdb.asia-southeast1.firebasedatabase.app"

# Threshold Default (Statis) berdasarkan Time Series
DEFAULT_THRESHOLD_BURST = 7.0
DEFAULT_THRESHOLD_ACTIVE = 20.30

# State Global
op_mode = "default"        # "default" atau "calibrated"
calib_state = "none"       # "none", "stage1_normal", "stage2_pending", "stage2_drone", "done"

# Threshold Aktif yang digunakan untuk deteksi
active_thr_burst = DEFAULT_THRESHOLD_BURST
active_thr_active = DEFAULT_THRESHOLD_ACTIVE

# Data tampungan kalibrasi
calib_pool_normal = {}
calib_pool_drone = {}
frozen = {}

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

def send_raw_to_firebase(node_id, data_hex, timestamp_wib, rssi, snr):
    """Kirim data mentah tiap frame ke Timeseries."""
    current_date = time.strftime("%Y-%m-%d ")
    ok = firebase_patch(f"Timeseries/{node_id}", {
        "node": node_id,
        "data_hex": data_hex,
        "timestamp": current_date + timestamp_wib,
        "rssi": rssi,
        "snr": snr,
    })
    if ok:
        print(f"-> Raw frame {node_id} -> Timeseries (rssi={rssi}, snr={snr})")

def send_prediction_to_firebase(node_id, is_drone):
    """Kirim hasil deteksi ke Timeseries."""
    status_str = "Drone Terdeteksi" if is_drone else "Aman"
    firebase_patch(f"Timeseries/{node_id}", {
        "prediction": status_str,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    print(f"-> Prediksi {node_id} terkirim (Prediction: {status_str})")

# ==============================================================
# KONFIGURASI DETEKSI ADAPTIF (Z-SCORE)
# --------------------------------------------------------------
WINDOW_SIZE = 5
STRIDE = 2
CALIB_SAMPLES = 20

# MIN_STDEV: Pengaman batas bawah agar pembagian stdev tidak error/div 0
MIN_STDEV_BURST = 0.5
MIN_STDEV_ACTIVE = 1.0
# ==============================================================

LOG_CSV = True
CSV_FILE = "fitur_log.csv"

data_windows = {}
stride_counter = {}

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

def run_menu():
    global op_mode, calib_state, calib_pool_normal, calib_pool_drone, frozen, active_thr_burst, active_thr_active
    print("\n" + "="*60)
    print("     LOW COST DRONE DETECTOR (LCDD) - SYSTEM BACKEND V2")
    print("="*60)
    print("Silakan pilih mode awal:")
    print("1. Langsung Detect (Menggunakan Threshold Default)")
    print("2. Kalibrasi Terlebih Dahulu (2 Tahap: Tanpa Drone & Ada Drone)")
    print("-"*60)
    
    choice = input("Pilihan Anda (1/2): ").strip()
    if choice == "2":
        op_mode = "calibrated"
        calib_state = "stage1_normal"
        calib_pool_normal.clear()
        calib_pool_drone.clear()
        frozen.clear()
        print("\n>>> MEMULAI KALIBRASI TAHAP 1: TANPA DRONE <<<")
        print("Pastikan drone dalam kondisi MATI/JAUH. Menunggu data masuk...")
    else:
        op_mode = "default"
        calib_state = "none"
        active_thr_burst = DEFAULT_THRESHOLD_BURST
        active_thr_active = DEFAULT_THRESHOLD_ACTIVE
        print(f"\n>>> MODE DETEKSI LANGSUNG (Threshold Statis: Burst={active_thr_burst}, Active={active_thr_active}) <<<")

def read_from_port(ser):
    global data_windows, stride_counter, calib_pool_normal, calib_pool_drone, frozen, calib_state, op_mode, active_thr_burst, active_thr_active

    current_node = "Unknown"
    current_rssi = 0
    current_snr = 0.0
    current_ts_wib = "00:00:00"

    # Jalankan menu pertama kali
    ser.close()
    run_menu()
    try:
        ser.open()
        ser.reset_input_buffer()
    except Exception as e:
        print(f"Error reopening serial port: {e}")

    while True:
        try:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').strip()

                # Print status/debug log dari Gateway ESP32
                if line.startswith(">>>") or "Timeout" in line or "Balasan" in line or "gagal" in line.lower() or "koneksi" in line.lower():
                    print(f"[Gateway] {line}")

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
                    hex_str = match.group(1)
                    node_id = current_node            
                    node_key = node_id.lower()        

                    if len(hex_str) == 125:
                        spectrum_bits = hex_to_bits(hex_str)

                        if node_id not in data_windows:
                            data_windows[node_id] = []
                            stride_counter[node_id] = 0

                        # SINKRONISASI KE WEB (Timeseries)
                        send_raw_to_firebase(node_id, hex_str, current_ts_wib, current_rssi, current_snr)

                        frame_burst = get_burst_count(spectrum_bits)
                        frame_active = sum(spectrum_bits)

                        # ====== PROSES KALIBRASI ======
                        if calib_state == "stage1_normal":
                            if node_id not in calib_pool_normal:
                                calib_pool_normal[node_id] = []
                            pool = calib_pool_normal[node_id]
                            if len(pool) < CALIB_SAMPLES:
                                pool.append((frame_burst, frame_active))
                                print(f"[KALIBRASI TAHAP 1 (NORMAL) - {node_id}] frame {len(pool)}/{CALIB_SAMPLES} | burst={frame_burst} | active={frame_active}")
                                log_csv(node_id, frame_burst, frame_active, 0, 0, 0, "kalibrasi_normal")

                            # Cek jika semua node yang aktif/terdaftar sudah mencapai CALIB_SAMPLES
                            if len(calib_pool_normal) > 0 and all(len(p) >= CALIB_SAMPLES for p in calib_pool_normal.values()):
                                for nid, p_data in calib_pool_normal.items():
                                    burst_list = [p[0] for p in p_data]
                                    active_list = [p[1] for p in p_data]
                                    
                                    mean_bn = statistics.mean(burst_list)
                                    std_bn = max(MIN_STDEV_BURST, statistics.stdev(burst_list))
                                    mean_an = statistics.mean(active_list)
                                    std_an = max(MIN_STDEV_ACTIVE, statistics.stdev(active_list))

                                    frozen[nid] = {
                                        "normal_mean_burst": mean_bn,
                                        "normal_std_burst": std_bn,
                                        "normal_mean_active": mean_an,
                                        "normal_std_active": std_an,
                                    }
                                    print(f"\n[OK] Kalibrasi Tahap 1 (Normal) Selesai untuk {nid}.")
                                    print(f"-> Baseline Normal: burst={mean_bn:.2f} (std={std_bn:.2f}) | active={mean_an:.2f} (std={std_an:.2f})")

                                print("\n" + "="*60)
                                print("PERSIAPAN KALIBRASI TAHAP 2 (DENGAN DRONE)")
                                print("Silakan nyalakan drone Anda dan posisikan dekat dengan sensor.")
                                print("="*60)
                                
                                # Tutup serial agar tidak hang/overflow saat menunggu input
                                ser.close()
                                input("Tekan ENTER jika drone sudah aktif dan siap untuk kalibrasi...")
                                print("Membuka kembali serial port...")
                                try:
                                    ser.open()
                                    ser.reset_input_buffer()
                                except Exception as e:
                                    print(f"Error reopening serial port: {e}")
                                
                                calib_state = "stage2_drone"
                                print("\n>>> MEMULAI KALIBRASI TAHAP 2: DENGAN DRONE. Menunggu data...")
                            continue

                        elif calib_state == "stage2_drone":
                            if node_id not in calib_pool_drone:
                                calib_pool_drone[node_id] = []
                            pool = calib_pool_drone[node_id]
                            if len(pool) < CALIB_SAMPLES:
                                pool.append((frame_burst, frame_active))
                                print(f"[KALIBRASI TAHAP 2 (DRONE) - {node_id}] frame {len(pool)}/{CALIB_SAMPLES} | burst={frame_burst} | active={frame_active}")
                                log_csv(node_id, frame_burst, frame_active, 0, 0, 0, "kalibrasi_drone")

                            # Cek jika semua node yang terdaftar di Stage 1 sudah selesai di Stage 2
                            if all(nid in calib_pool_drone and len(calib_pool_drone[nid]) >= CALIB_SAMPLES for nid in calib_pool_normal.keys()):
                                for nid in calib_pool_normal.keys():
                                    p_data = calib_pool_drone[nid]
                                    burst_list = [p[0] for p in p_data]
                                    active_list = [p[1] for p in p_data]
                                    
                                    mean_bd = statistics.mean(burst_list)
                                    std_bd = max(MIN_STDEV_BURST, statistics.stdev(burst_list))
                                    mean_ad = statistics.mean(active_list)
                                    std_ad = max(MIN_STDEV_ACTIVE, statistics.stdev(active_list))

                                    fz = frozen[nid]
                                    fz["drone_mean_burst"] = mean_bd
                                    fz["drone_std_burst"] = std_bd
                                    fz["drone_mean_active"] = mean_ad
                                    fz["drone_std_active"] = std_ad

                                    # Hitung Threshold Adaptif rekomendasi
                                    thr_burst_calib = (fz["normal_mean_burst"] + mean_bd) / 2
                                    thr_active_calib = (fz["normal_mean_active"] + mean_ad) / 2

                                    fz["thr_burst_calib"] = thr_burst_calib
                                    fz["thr_active_calib"] = thr_active_calib

                                    # Hitung Jarak Standar Deviasi Threshold Lama terhadap Baseline Normal (Z-Score)
                                    z_burst_old = (DEFAULT_THRESHOLD_BURST - fz["normal_mean_burst"]) / fz["normal_std_burst"]
                                    z_active_old = (DEFAULT_THRESHOLD_ACTIVE - fz["normal_mean_active"]) / fz["normal_std_active"]

                                    print("\n" + "#"*60)
                                    print(f"KALIBRASI {nid} SELESAI - HASIL ANALISIS")
                                    print("#"*60)
                                    print(f"Baseline TANPA Drone : burst={fz['normal_mean_burst']:.2f} (std={fz['normal_std_burst']:.2f}) | active={fz['normal_mean_active']:.2f} (std={fz['normal_std_active']:.2f})")
                                    print(f"Baseline DENGAN Drone: burst={mean_bd:.2f} (std={std_bd:.2f}) | active={mean_ad:.2f} (std={std_ad:.2f})")
                                    print("-"*60)
                                    print(f"Threshold Default     : burst={DEFAULT_THRESHOLD_BURST:.2f} | active={DEFAULT_THRESHOLD_ACTIVE:.2f}")
                                    print(f"Jarak Threshold Lama terhadap Baseline Normal (Z-Score):")
                                    print(f"  - Jarak Burst  : {z_burst_old:.2f} σ")
                                    print(f"  - Jarak Active : {z_active_old:.2f} σ")
                                    print("-"*60)
                                    print(f"Threshold Kalibrasi Baru (Rekomendasi Tengah):")
                                    print(f"  - RECOMMENDED BURST  : {thr_burst_calib:.2f}")
                                    print(f"  - RECOMMENDED ACTIVE : {thr_active_calib:.2f}")
                                    print("#"*60 + "\n")

                                print("PILIHAN TINDAKAN:")
                                print("1. Lanjut Deteksi (Menggunakan Threshold Kalibrasi Baru)")
                                print("2. Kalibrasi Ulang")
                                print("3. Kembali ke Menu Utama / Gunakan Threshold Default")
                                
                                # Tutup serial agar tidak hang/overflow saat menunggu input
                                ser.close()
                                post_choice = input("Pilihan (1/2/3): ").strip()
                                print("Membuka kembali serial port...")
                                try:
                                    ser.open()
                                    ser.reset_input_buffer()
                                except Exception as e:
                                    print(f"Error reopening serial port: {e}")

                                if post_choice == "1":
                                    op_mode = "calibrated"
                                    calib_state = "done"
                                    print(f"\n>>> MULAI DETEKSI (Menggunakan Threshold Kalibrasi Hasil Analisis) <<<")
                                elif post_choice == "2":
                                    calib_state = "stage1_normal"
                                    calib_pool_normal.clear()
                                    calib_pool_drone.clear()
                                    frozen.clear()
                                    print("\n>>> MEMULAI ULANG KALIBRASI TAHAP 1: TANPA DRONE <<<")
                                else:
                                    run_menu()
                            continue

                        # ====== FASE DETEKSI ======
                        data_windows[node_id].append(spectrum_bits)
                        if len(data_windows[node_id]) > WINDOW_SIZE:
                            data_windows[node_id].pop(0)

                        stride_counter[node_id] += 1
                        
                        if len(data_windows[node_id]) == WINDOW_SIZE and stride_counter[node_id] >= STRIDE:
                            stride_counter[node_id] = 0
                            window_data = data_windows[node_id]

                            burst_counts = [get_burst_count(row) for row in window_data]
                            burst_count_mean = sum(burst_counts) / WINDOW_SIZE
                            active_ch_mean = sum([sum(row) for row in window_data]) / WINDOW_SIZE

                            # Tentukan threshold yang digunakan (adaptif per node atau default)
                            if op_mode == "calibrated" and node_id in frozen:
                                thr_b = frozen[node_id]["thr_burst_calib"]
                                thr_a = frozen[node_id]["thr_active_calib"]
                            else:
                                thr_b = DEFAULT_THRESHOLD_BURST
                                thr_a = DEFAULT_THRESHOLD_ACTIVE

                            cond_burst = burst_count_mean >= thr_b
                            cond_active = active_ch_mean >= thr_a
                            predicted_as_drone = cond_burst or cond_active

                            status = "DRONE TERDETEKSI !!!" if predicted_as_drone else "AMAN (Tidak ada Drone)"

                            print("\n" + "="*60)
                            print(f"[{time.strftime('%H:%M:%S')}] ANALISIS TIME-SERIES LOKASI: {node_id}")
                            print(f"Mode Operasi   : {'KALIBRASI BARU' if op_mode == 'calibrated' else 'THRESHOLD DEFAULT'}")
                            print(f"Burst Mean     : {burst_count_mean:.2f} \t(Threshold: {thr_b:.2f}) \t{'[V]' if cond_burst else '[ ]'}")
                            print(f"Active Mean    : {active_ch_mean:.2f} \t(Threshold: {thr_a:.2f}) \t{'[V]' if cond_active else '[ ]'}")
                            print(f"Aturan Deteksi : OR - minimal satu parameter melewati Threshold")
                            print(f"-> KEPUTUSAN   : {status} DI LOKASI {node_id}")
                            print("="*60 + "\n")

                            log_csv(node_id, burst_count_mean, active_ch_mean,
                                    frozen.get(node_id, {}).get("normal_mean_burst", 0.0),
                                    frozen.get(node_id, {}).get("normal_mean_active", 0.0),
                                    1 if predicted_as_drone else 0, "deteksi")

                            # SINKRONISASI KE WEB (Timeseries)
                            send_prediction_to_firebase(node_id, predicted_as_drone)
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