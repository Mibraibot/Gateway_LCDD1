import os
import json
import time
import re
import serial
import serial.tools.list_ports
import math

# Global state to store the latest parsed data
latest_data = {
    "rssi": 0,
    "snr": 0,
    "timestamp": "-",
    "node": "-",
    "spectrum": [0] * 125
}

# Time-series prediction thresholds and window
THRESHOLD_BURST = 7.70
THRESHOLD_ENTROPY = 2.24
THRESHOLD_ACTIVE = 20.30
WINDOW_SIZE = 10
data_windows = {} # Menggunakan dictionary agar tidak tercampur antar Node

def get_spatial_entropy(channels):
    ones = sum(channels)
    zeros = len(channels) - ones
    if not channels: return 0.0
    p1 = ones / len(channels)
    p0 = zeros / len(channels)
    ent = 0.0
    if p1 > 0: ent -= p1 * math.log2(p1)
    if p0 > 0: ent -= p0 * math.log2(p0)
    return ent

def get_burst_count(channels):
    count = 0
    in_burst = False
    for val in channels:
        if val >= 1 and not in_burst:
            in_burst = True
            count += 1
        elif val == 0 and in_burst:
            in_burst = False
    return count

def auto_detect_port():
    """Auto-detect the ESP32 serial port."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        # Typical keywords for ESP32/Arduino USB-to-Serial chips
        if "usbserial" in port.device.lower() or "usbmodem" in port.device.lower() or "ttyusb" in port.device.lower() or "ttyacm" in port.device.lower():
            return port.device
    
    # Fallback to the first available port if no explicit match
    if ports:
        return ports[0].device
    return None

def read_from_port(ser):
    """Background thread to read data from the serial port."""
    global data_windows
    
    current_node = "Unknown"
    print("Mendengarkan data spektrum... (Menunggu 10 data window per Node)")
    while True:
        try:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                
                # Tangkap info node (muncul sebelum data_hex)
                node_match = re.search(r'"node"\s*:\s*"([^"]+)"', line)
                if node_match:
                    current_node = node_match.group(1)
                
                # Kita abaikan debug log yang merusak JSON, langsung ekstrak data_hex pakai Regex!
                match = re.search(r'"data_hex"\s*:\s*"([0-9a-fA-F]+)"', line)
                if match:
                    hex_str = match.group(1)
                    node_id = current_node # Gunakan node yang baru saja ditangkap
                    
                    if len(hex_str) == 125:
                        spectrum_values = []
                        for char in hex_str:
                            try:
                                spectrum_values.append(int(char, 16))
                            except ValueError:
                                spectrum_values.append(0)
                        
                        # Inisialisasi list untuk node ini jika belum ada
                        if node_id not in data_windows:
                            data_windows[node_id] = []
                            
                        # Process for time-series prediction per Node
                        data_windows[node_id].append(spectrum_values)
                        if len(data_windows[node_id]) > WINDOW_SIZE:
                            data_windows[node_id].pop(0)
                            
                        print(f"-> Data Hex diterima dari {node_id} ({len(data_windows[node_id])}/10)")
                            
                        if len(data_windows[node_id]) == WINDOW_SIZE:
                            window_data = data_windows[node_id]
                            burst_counts = [get_burst_count(row) for row in window_data]
                            spatial_entropies = [get_spatial_entropy(row) for row in window_data]
                            
                            burst_count_mean = sum(burst_counts) / WINDOW_SIZE
                            spatial_entropy_mean = sum(spatial_entropies) / WINDOW_SIZE
                            total_active_mean = sum([sum(row) for row in window_data]) / WINDOW_SIZE
                            
                            # Logika Prediksi diprioritaskan sepenuhnya pada Burst Count
                            predicted_as_drone = (burst_count_mean >= THRESHOLD_BURST)
                            
                            status = "DRONE TERDETEKSI 🚁 !!!" if predicted_as_drone else "AMAN ✅ (Tidak ada Drone)"
                            
                            print("\n" + "="*60)
                            print(f"[{time.strftime('%H:%M:%S')}] ANALISIS TIME-SERIES LOKASI: {node_id}")
                            print(f"Burst Mean   : {burst_count_mean:.2f} \t(Threshold: {THRESHOLD_BURST})")
                            print(f"Entropy Mean : {spatial_entropy_mean:.2f} \t(Threshold: {THRESHOLD_ENTROPY})")
                            print(f"Active Mean  : {total_active_mean:.2f} \t(Threshold: {THRESHOLD_ACTIVE})")
                            print(f"-> KEPUTUSAN : {status} DI LOKASI {node_id}")
                            print("="*60 + "\n")
            else:
                time.sleep(0.01)
        except Exception as e:
            print(f"Serial reading error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    port_name = auto_detect_port()
    if not port_name:
        print("Warning: No serial port detected! Please plug in your ESP32.")
    else:
        print(f"Connecting to serial port: {port_name} at 115200 baud...")
        try:
            ser = serial.Serial(port_name, 115200, timeout=1)
            print("Serial connection established successfully. Waiting for data...")
            # Run the serial reading loop directly in the main thread
            read_from_port(ser)
        except Exception as e:
            print(f"Failed to open serial port: {e}")
