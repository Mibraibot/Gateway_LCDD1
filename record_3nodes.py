import time
import json
import serial
import serial.tools.list_ports
import datetime
import os

# Konfigurasi jumlah siklus yang ingin direkam
# 1 siklus = perjalanan dari Node 1 -> Node 2 -> Node 3 -> Node 1 (kembali ke awal)
TARGET_CYCLES = 500


def auto_detect_port():
    """Auto-detect port ESP32."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if "usbserial" in port.device.lower() or "usbmodem" in port.device.lower() or "ttyusb" in port.device.lower() or "ttyacm" in port.device.lower():
            return port.device
    if ports:
        return ports[0].device
    return None

def main():
    print("Mencari port Serial Gateway...")
    port_name = auto_detect_port()
    if not port_name:
        print("Error: Port Serial ESP32 tidak terdeteksi. Pastikan alat terhubung.")
        return

    print(f"Menghubungkan ke {port_name} (115200 baud)...")
    try:
        ser = serial.Serial(port_name, 115200, timeout=1)
    except Exception as e:
        print(f"Gagal membuka port serial: {e}")
        return

    print("\n==========================================")
    print(f"MULAI MEREKAM DATA HINGGA {TARGET_CYCLES} SIKLUS PENUH")
    print("==========================================")
    
    collected_data = []
    buffer = ""
    
    node1_count = 0
    cycles_done = 0
    waiting_for_first_node1 = True

    try:
        while cycles_done < TARGET_CYCLES:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                
                # Menampilkan LOG lengkap dari Gateway di Terminal secara real-time
                time_log = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                if line:
                    print(f"[{time_log}] {line}")
                
                # Tiap paket JSON dipisahkan oleh garis dari Gateway
                if "--------------------------------" in line:
                    if buffer.strip():
                        try:
                            # Cari data JSON dari buffer
                            start_idx = buffer.find('{')
                            end_idx = buffer.rfind('}')
                            if start_idx != -1 and end_idx != -1:
                                json_str = buffer[start_idx:end_idx+1]
                                data = json.loads(json_str)
                                
                                if "event" in data and data["event"] == "data_received":
                                    node_name = data["payload"].get("node", "").replace(" ", "")
                                    
                                    # Filter: Tunggu sampai Node 1 muncul pertama kali
                                    if waiting_for_first_node1:
                                        if node_name == "Node1":
                                            waiting_for_first_node1 = False
                                            print("\n[START] Node 1 pertama terdeteksi, mulai merekam siklus yang valid!\n")
                                        else:
                                            print(f"[SKIP] Mengabaikan {node_name} karena rekam dimulai di tengah siklus.")
                                            buffer = ""
                                            continue

                                    data["captured_at_komputer"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                                    collected_data.append(data)
                                    
                                    # Hitung jumlah Node1 yang diterima untuk menentukan siklus
                                    if node_name == "Node1":
                                        node1_count += 1
                                        cycles_done = max(0, node1_count - 1)
                                        if cycles_done > 0 and cycles_done <= TARGET_CYCLES:
                                            print(f"\n---> [INFO] Siklus ke-{cycles_done} selesai! Menuju target: {TARGET_CYCLES} siklus.\n")
                                            
                        except json.JSONDecodeError:
                            pass
                    
                    buffer = ""
                else:
                    buffer += line + "\n"
            else:
                time.sleep(0.001)
                
    except KeyboardInterrupt:
        print("\n[!] Perekaman dihentikan secara manual oleh pengguna (Ctrl+C).")

    print("\n==========================================")
    print("WAKTU PEREKAMAN SELESAI")
    print(f"Total data yang berhasil ditangkap: {len(collected_data)} paket")
    print("==========================================")
    
    if len(collected_data) > 0:
        
        # 1. Hitung Metrik Delay
        for d in collected_data:
            d['_dt'] = datetime.datetime.strptime(d["captured_at_komputer"], "%Y-%m-%d %H:%M:%S.%f")
            
        n1_to_n2 = []
        n2_to_n3 = []
        n3_to_n1 = []
        full_cycles = []
        
        last_n1_time = None
        
        for i in range(len(collected_data) - 1):
            curr_node = collected_data[i]["payload"].get("node", "").replace(" ", "")
            next_node = collected_data[i+1]["payload"].get("node", "").replace(" ", "")
            dt_diff = (collected_data[i+1]['_dt'] - collected_data[i]['_dt']).total_seconds()
            
            if curr_node == "Node1" and next_node == "Node2":
                n1_to_n2.append(dt_diff)
            elif curr_node == "Node2" and next_node == "Node3":
                n2_to_n3.append(dt_diff)
            elif curr_node == "Node3" and next_node == "Node1":
                n3_to_n1.append(dt_diff)
                
            if curr_node == "Node1":
                if last_n1_time is not None:
                    cycle_time = (collected_data[i]['_dt'] - last_n1_time).total_seconds()
                    full_cycles.append(cycle_time)
                last_n1_time = collected_data[i]['_dt']
        
        if len(collected_data) > 0 and collected_data[-1]["payload"].get("node", "").replace(" ", "") == "Node1" and last_n1_time is not None:
            cycle_time = (collected_data[-1]['_dt'] - last_n1_time).total_seconds()
            if cycle_time > 0:
                full_cycles.append(cycle_time)
        
        def avg(lst):
            return sum(lst)/len(lst) if len(lst)>0 else 0
            
        avg_1_2 = avg(n1_to_n2)
        avg_2_3 = avg(n2_to_n3)
        avg_3_1 = avg(n3_to_n1)
        avg_full = avg(full_cycles)

        # Hapus object datetime sementara agar JSON bersih
        for d in collected_data:
            if '_dt' in d:
                del d['_dt']

        timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 2. Simpan JSON mentah (Raw Array)
        json_filename = f"dataset_3nodes_{timestamp_str}.json"
        with open(json_filename, "w", encoding="utf-8") as f:
            json.dump(collected_data, f, indent=2)
            
        # 3. Simpan Laporan Metrik (.md file untuk table & rumus matematis)
        md_filename = f"metrics_delay_{timestamp_str}.md"
        with open(md_filename, "w", encoding="utf-8") as f:
            f.write("# Laporan Metrik Delay Gateway LoRa LCDD\n\n")
            f.write(f"**Waktu Perekaman:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**Target Siklus:** {TARGET_CYCLES} siklus\n")
            f.write(f"**Total Paket Diterima:** {len(collected_data)} paket\n\n")
            
            f.write("## 1. Rangkuman Metrik\n\n")
            f.write("Berikut adalah metrik yang dihitung secara akurat menggunakan **Timestamp Komputer** (`captured_at_komputer`). Alasan penggunaannya adalah karena jam komputer menyimpan presisi hingga tingkat **milidetik (ms)**, yang mana sangat penting untuk menghitung kecepatan transmisi *serial/LoRa*, dibandingkan menggunakan `timestamp_wib` dari node yang hanya sebatas detik.\n\n")
            
            f.write("| Deskripsi | Rata-Rata Delay (Detik) | Jumlah Sampel |\n")
            f.write("|-----------|-------------------------|---------------|\n")
            f.write(f"| Node 1 -> Node 2 | {avg_1_2:.3f} | {len(n1_to_n2)} |\n")
            f.write(f"| Node 2 -> Node 3 | {avg_2_3:.3f} | {len(n2_to_n3)} |\n")
            f.write(f"| Node 3 -> Node 1 | {avg_3_1:.3f} | {len(n3_to_n1)} |\n")
            f.write(f"| **1 Siklus Penuh** | **{avg_full:.3f}** | **{len(full_cycles)}** |\n\n")
            
            f.write("## 2. Penjelasan Rumus Matematika\n\n")
            f.write("Kalkulasi didasarkan pada selisih waktu paket yang berurutan. Misalkan $T_{n}^{(k)}$ adalah stempel waktu penerimaan data (`captured_at_komputer`) dari Node $n$ pada siklus putaran ke-$k$.\n\n")
            f.write("- **Delay Node 1 ke Node 2:** \n")
            f.write("  $$\\Delta t_{1\\to2} = \\frac{1}{N} \\sum_{k=1}^{N} \\left( T_{2}^{(k)} - T_{1}^{(k)} \\right)$$\n\n")
            f.write("- **Delay Node 2 ke Node 3:** \n")
            f.write("  $$\\Delta t_{2\\to3} = \\frac{1}{N} \\sum_{k=1}^{N} \\left( T_{3}^{(k)} - T_{2}^{(k)} \\right)$$\n\n")
            f.write("- **Delay Node 3 ke Node 1 (Kembali ke Awal Putaran):** \n")
            f.write("  $$\\Delta t_{3\\to1} = \\frac{1}{N} \\sum_{k=1}^{N} \\left( T_{1}^{(k+1)} - T_{3}^{(k)} \\right)$$\n\n")
            f.write("- **Waktu 1 Siklus Penuh (Waktu antara dua Node 1 yang berurutan):** \n")
            f.write("  $$T_{siklus} = \\frac{1}{N} \\sum_{k=1}^{N} \\left( T_{1}^{(k+1)} - T_{1}^{(k)} \\right)$$\n\n")
            f.write("> **Catatan:** Waktu di atas murni diambil ketika paket masuk ke port Serial Gateway secara lokal. Variasi delay biasanya berasal dari timeout ketika data loss, bentrokan *airtime* frekuensi, atau jeda inisiasi *polling* dari gateway ke node.\n")
            
        print(f"\n[SUKSES] Penyimpanan Selesai!")
        print(f"--> Data Mentah JSON  : {os.path.abspath(json_filename)}")
        print(f"--> File Laporan Metrik: {os.path.abspath(md_filename)}\n")

    else:
        print("\n[INFO] Tidak ada data yang diterima selama proses perekaman. File tidak dibuat.")

if __name__ == "__main__":
    main()
