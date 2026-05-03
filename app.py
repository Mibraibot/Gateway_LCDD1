import os
import json
import time
import threading
import serial
import serial.tools.list_ports
from flask import Flask, render_template, Response

app = Flask(__name__)

# Global state to store the latest parsed data
latest_data = {
    "rssi": 0,
    "snr": 0,
    "timestamp": "-",
    "node": "-",
    "spectrum": [0] * 125
}

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
    global latest_data
    buffer = ""
    
    while True:
        try:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                
                # The gateway separates JSON packets with "--------------------------------"
                if "--------------------------------" in line:
                    if buffer.strip():
                        try:
                            # Try to parse the accumulated JSON buffer
                            data = json.loads(buffer)
                            
                            if "event" in data and data["event"] == "data_received" and "payload" in data:
                                payload = data["payload"]
                                
                                # Decode the hex string into an array of integers (0-15)
                                hex_str = payload.get("data_hex", "")
                                spectrum_values = []
                                for char in hex_str:
                                    try:
                                        spectrum_values.append(int(char, 16))
                                    except ValueError:
                                        spectrum_values.append(0)
                                
                                # Make sure it's exactly 125 channels
                                while len(spectrum_values) < 125:
                                    spectrum_values.append(0)
                                spectrum_values = spectrum_values[:125]
                                
                                # Update global state
                                latest_data = {
                                    "rssi": data.get("rssi", 0),
                                    "snr": data.get("snr", 0),
                                    "timestamp": payload.get("timestamp_wib", "-"),
                                    "node": payload.get("node", "Unknown"),
                                    "spectrum": spectrum_values
                                }
                        except json.JSONDecodeError:
                            print(f"Failed to parse JSON: {buffer}")
                        
                    # Clear buffer for the next packet
                    buffer = ""
                else:
                    # Append line to buffer
                    buffer += line + "\n"
            else:
                time.sleep(0.01)
        except Exception as e:
            print(f"Serial reading error: {e}")
            time.sleep(1)

def generate_sse():
    """Generator for Server-Sent Events."""
    while True:
        # Send the latest data to the client as JSON
        json_data = json.dumps(latest_data)
        yield f"data: {json_data}\n\n"
        time.sleep(0.5) # Send updates every 500ms

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/stream")
def stream():
    return Response(generate_sse(), mimetype="text/event-stream")

if __name__ == "__main__":
    port_name = auto_detect_port()
    if not port_name:
        print("Warning: No serial port detected! Please plug in your ESP32.")
        ser = None
    else:
        print(f"Connecting to serial port: {port_name} at 115200 baud...")
        try:
            ser = serial.Serial(port_name, 115200, timeout=1)
            # Start the background thread
            thread = threading.Thread(target=read_from_port, args=(ser,), daemon=True)
            thread.start()
            print("Serial connection established successfully.")
        except Exception as e:
            print(f"Failed to open serial port: {e}")
    
    print("Starting Flask Web Server on http://0.0.0.0:5100")
    app.run(host="0.0.0.0", port=5100, debug=False, threaded=True)
