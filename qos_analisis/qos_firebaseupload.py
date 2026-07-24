# qos2_firebase_upload.py
# Benchmark QoS unggah Gateway -> Firebase RTDB (REST PATCH), tanpa hardware.
# Payload dibuat sama persis ukurannya dengan frame asli (data_hex 125 karakter).
import json
import time
import statistics
import urllib.request

FIREBASE_URL = "https://lowcostdronedetect-default-rtdb.asia-southeast1.firebasedatabase.app"
PATH = "qos_test/bench"     # path uji terpisah, tidak mengganggu detection_system
N = 100                     # jumlah paket uji
INTERVAL = 0.5              # detik antar paket (~ritme 1 frame per giliran poll)
TIMEOUT = 4

def main():
    delays, gagal = [], 0
    payload_hex = "0" * 125
    t_start = time.perf_counter()
    total_bytes = 0

    for i in range(1, N + 1):
        data = {
            "node": "bench", "data_hex": payload_hex,
            "timestamp_wib": time.strftime("%H:%M:%S"),
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rssi": -40, "snr": 9.5, "seq": i,
            "gw_epoch_ms": int(time.time() * 1000),
        }
        body = json.dumps(data).encode()
        total_bytes += len(body)
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(f"{FIREBASE_URL}/{PATH}.json", method="PATCH")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, body, timeout=TIMEOUT)
            d = (time.perf_counter() - t0) * 1000
            delays.append(d)
            print(f"[{i:3d}/{N}] OK  {d:7.1f} ms")
        except Exception as e:
            gagal += 1
            print(f"[{i:3d}/{N}] GAGAL ({e})")
        time.sleep(INTERVAL)

    dur = time.perf_counter() - t_start
    print("\n" + "=" * 60)
    print("RINGKASAN QoS UNGGAH GATEWAY -> FIREBASE")
    print("=" * 60)
    print(f"Paket terkirim : {len(delays)}/{N}  (gagal {gagal}, "
          f"loss {100.0*gagal/N:.2f} %, sukses {100.0*len(delays)/N:.2f} %)")
    if delays:
        jit = (sum(abs(delays[i]-delays[i-1]) for i in range(1, len(delays)))
               / (len(delays)-1)) if len(delays) > 1 else 0.0
        print(f"Delay (ms)     : rata={statistics.mean(delays):.1f} "
              f"min={min(delays):.1f} max={max(delays):.1f} "
              f"std={statistics.pstdev(delays):.1f}")
        print(f"Jitter (ms)    : {jit:.1f}")
    print(f"Throughput     : {total_bytes*8/dur:.1f} bps "
          f"(payload {total_bytes} B dalam {dur:.1f} s, termasuk jeda {INTERVAL}s)")

if __name__ == "__main__":
    main()