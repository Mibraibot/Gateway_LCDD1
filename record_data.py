import time
import json
import serial
import serial.tools.list_ports
import datetime
import os

# Konfigurasi waktu rekam (dalam detik). 120 detik = 2 menit.
# Konfigurasi waktu rekam: 30 menit
RECORDING_DURATION = 1800

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
        # Buka koneksi serial
        ser = serial.Serial(port_name, 115200, timeout=1)
    except Exception as e:
        print(f"Gagal membuka port serial: {e}")
        print("\nPENTING: Jika 'app.py' (Web Server) masih berjalan, tolong MATIKAN (Ctrl+C) terlebih dahulu.")
        print("Hanya satu program yang bisa membaca port Serial di waktu yang sama.")
        return

    print("\n==========================================")
    print(f"MULAI MEREKAM DATA LORA SELAMA {RECORDING_DURATION} DETIK")
    print("==========================================")
    
    start_time = time.time()
    collected_data = []
    buffer = ""

    try:
        while time.time() - start_time < RECORDING_DURATION:
            # Hitung sisa waktu untuk ditampilkan di terminal
            elapsed = time.time() - start_time
            remaining = RECORDING_DURATION - elapsed

            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                
                # Tiap paket JSON dipisahkan oleh garis dari Gateway
                if "--------------------------------" in line:
                    if buffer.strip():
                        try:
                            # Coba parsing JSON dari buffer
                            data = json.loads(buffer)
                            if "event" in data and data["event"] == "data_received":
                                # Tambahkan timestamp komputer saat paket ini diterima
                                data["captured_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                
                                collected_data.append(data)
                                print(f"[{data['captured_at']}] Paket berhasil direkam. (Total: {len(collected_data)} paket) | Sisa waktu: {int(remaining)}s")
                        except json.JSONDecodeError:
                            pass
                    
                    # Kosongkan buffer untuk paket berikutnya
                    buffer = ""
                else:
                    buffer += line + "\n"
            else:
                time.sleep(0.01)
                
    except KeyboardInterrupt:
        print("\n[!] Perekaman dihentikan secara manual oleh pengguna (Ctrl+C).")

    print("\n==========================================")
    print("WAKTU PEREKAMAN SELESAI")
    print(f"Total data yang berhasil ditangkap: {len(collected_data)} paket")
    print("==========================================")
    
    if len(collected_data) > 0:
        # Format nama file dengan tanggal & jam, misalnya: dataset_lora_20260502_195500.json
        timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"dataset_lora_{timestamp_str}.json"
        
        # Simpan ke file JSON
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(collected_data, f, indent=2)
            
        print(f"\n[SUKSES] Semua data telah dibungkus dan disimpan ke dalam file:\n--> {os.path.abspath(filename)}\n")
    else:
        print("\n[INFO] Tidak ada data yang diterima selama proses perekaman. File JSON tidak dibuat.")

if __name__ == "__main__":
    main()
