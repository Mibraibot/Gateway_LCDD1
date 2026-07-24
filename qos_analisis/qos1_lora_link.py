# qos1_lora_link.py
# QoS link Node -> Gateway (LoRa) dari log serial gateway ESP32.
# Jalankan SENDIRIAN (backend/app.py harus dimatikan dulu, port serial cuma satu).
# Stop dengan Ctrl+C -> ringkasan + CSV qos1_lora_link_<waktu>.csv

import re
import csv
import time
import statistics
import serial
import serial.tools.list_ports

BAUD = 115200
# Perkiraan ukuran payload aplikasi balasan node:
# {"node":"NodeX","timestamp_wib":"HH:MM:SS","data_hex":"<125 char>"} ~ 182 byte
PAYLOAD_BYTES = 182

def auto_detect_port():
    ports = serial.tools.list_ports.comports()
    for p in ports:
        d = p.device.lower()
        if "usbserial" in d or "usbmodem" in d or "ttyusb" in d or "ttyacm" in d:
            return p.device
    return ports[0].device if ports else None

class NodeStat:
    def __init__(self):
        self.poll = 0          # jumlah poll terkirim
        self.reply = 0         # jumlah balasan diterima
        self.timeout = 0
        self.delays = []       # ms
        self.rssi = []
        self.snr = []

    @property
    def jitter(self):
        # Jitter = rata-rata |selisih delay paket berurutan| (variasi delay, TIPHON)
        if len(self.delays) < 2:
            return 0.0
        difs = [abs(self.delays[i] - self.delays[i-1]) for i in range(1, len(self.delays))]
        return sum(difs) / len(difs)

def main():
    port = auto_detect_port()
    if not port:
        print("Tidak ada port serial. Colok gateway ESP32 dulu.")
        return
    print(f"Membuka {port} @ {BAUD} ...")
    ser = serial.Serial(port, BAUD, timeout=1)

    stats = {}                 # "Node1" -> NodeStat
    poll_sent = {}             # "Node1" -> perf_counter saat poll dikirim
    cur_rssi, cur_snr = None, None
    t_start = time.perf_counter()

    fname = f"qos1_lora_link_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    fcsv = open(fname, "w", newline="", encoding="utf-8")
    w = csv.writer(fcsv)
    w.writerow(["waktu", "node", "status", "delay_ms", "rssi_dbm", "snr_db"])

    print("Merekam... (Ctrl+C untuk berhenti & lihat ringkasan)\n")
    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            now = time.perf_counter()

            m = re.search(r"Mengirim Komando Panggil: POLL_(Node\d+)", line)
            if m:
                nid = m.group(1)
                stats.setdefault(nid, NodeStat()).poll += 1
                poll_sent[nid] = now
                continue

            m = re.search(r"Timeout! (Node\d+)", line)
            if m:
                nid = m.group(1)
                st = stats.setdefault(nid, NodeStat())
                st.timeout += 1
                poll_sent.pop(nid, None)
                w.writerow([time.strftime("%H:%M:%S"), nid, "timeout", "", "", ""])
                print(f"[{time.strftime('%H:%M:%S')}] {nid} TIMEOUT "
                      f"(loss sementara {st.timeout}/{st.poll})")
                continue

            m = re.search(r'"rssi"\s*:\s*(-?\d+)', line)
            if m and '"data_hex"' not in line:
                cur_rssi = int(m.group(1)); continue
            m = re.search(r'"snr"\s*:\s*(-?\d+\.?\d*)', line)
            if m and '"data_hex"' not in line:
                cur_snr = float(m.group(1)); continue

            # Baris payload: {"node":"NodeX",...,"data_hex":"..."}
            m = re.search(r'"node"\s*:\s*"(Node\d+)"', line)
            if m and '"data_hex"' in line:
                nid = m.group(1)
                st = stats.setdefault(nid, NodeStat())
                st.reply += 1
                if cur_rssi is not None: st.rssi.append(cur_rssi)
                if cur_snr  is not None: st.snr.append(cur_snr)
                d_ms = ""
                if nid in poll_sent:
                    d_ms = (now - poll_sent.pop(nid)) * 1000.0
                    st.delays.append(d_ms)
                    d_ms = f"{d_ms:.1f}"
                w.writerow([time.strftime("%H:%M:%S"), nid, "reply", d_ms, cur_rssi, cur_snr])
                print(f"[{time.strftime('%H:%M:%S')}] {nid} reply | delay={d_ms} ms | "
                      f"RSSI={cur_rssi} dBm | SNR={cur_snr} dB")
    except KeyboardInterrupt:
        pass
    finally:
        fcsv.close()
        ser.close()

    dur = time.perf_counter() - t_start
    print("\n" + "=" * 68)
    print("RINGKASAN QoS LINK LoRa NODE -> GATEWAY")
    print(f"Durasi pengukuran : {dur:.0f} s")
    print("=" * 68)
    tot_reply = 0
    for nid in sorted(stats):
        st = stats[nid]
        tot_reply += st.reply
        loss = 100.0 * st.timeout / st.poll if st.poll else 0.0
        pdr = 100.0 * st.reply / st.poll if st.poll else 0.0
        print(f"\n[{nid}] poll={st.poll} reply={st.reply} timeout={st.timeout}")
        print(f"  PDR          : {pdr:.2f} %   | Packet Loss: {loss:.2f} %")
        if st.delays:
            print(f"  Delay (ms)   : rata={statistics.mean(st.delays):.1f} "
                  f"min={min(st.delays):.1f} max={max(st.delays):.1f} "
                  f"std={statistics.pstdev(st.delays):.1f}")
            print(f"  Jitter (ms)  : {st.jitter:.1f}")
        if st.rssi:
            print(f"  RSSI (dBm)   : rata={statistics.mean(st.rssi):.1f} "
                  f"min={min(st.rssi)} max={max(st.rssi)}")
        if st.snr:
            print(f"  SNR (dB)     : rata={statistics.mean(st.snr):.2f} "
                  f"min={min(st.snr):.2f} max={max(st.snr):.2f}")
        thr = st.reply * PAYLOAD_BYTES * 8 / dur if dur > 0 else 0
        print(f"  Throughput   : {thr:.1f} bps (payload aplikasi {PAYLOAD_BYTES} B/frame)")
    thr_tot = tot_reply * PAYLOAD_BYTES * 8 / dur if dur > 0 else 0
    print(f"\nThroughput total sistem : {thr_tot:.1f} bps")
    print(f"CSV tersimpan           : {fname}")
    print("\nCatatan: delay terukur = scan nRF24 node (~330 ms) + proses node "
          "+ airtime balasan LoRa 182 B SF7 (~292 ms) + serial 115200 (~20 ms). "
          "Airtime poll tidak termasuk karena timestamp poll dicetak setelah TX selesai.")

if __name__ == "__main__":
    main()