# backend/qos_logger.py
# Pencatat QoS untuk backend gateway. Semua durasi dalam milidetik.
import csv
import os
import time
import threading
import statistics

class QoSLogger:
    def __init__(self, csv_file="qos_gateway_log.csv"):
        self.csv_file = csv_file
        self.lock = threading.Lock()
        self.data = {}  # segmen -> list (durasi_ms, ok)
        if not os.path.exists(csv_file):
            with open(csv_file, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    ["timestamp", "segmen", "target", "durasi_ms", "ok", "extra"])

    def log(self, segmen, target, durasi_ms, ok=1, extra=""):
        with self.lock:
            with open(self.csv_file, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    [time.strftime("%Y-%m-%d %H:%M:%S"), segmen, target,
                     f"{durasi_ms:.2f}", ok, extra])
            self.data.setdefault(segmen, []).append((durasi_ms, ok))

    def ringkas(self):
        print("\n" + "=" * 68)
        print("RINGKASAN QoS BACKEND GATEWAY")
        print("=" * 68)
        for seg, rows in sorted(self.data.items()):
            durs_ok = [d for d, ok in rows if ok == 1]
            n_gagal = sum(1 for _, ok in rows if ok == 0)
            sukses = 100.0 * len(durs_ok) / len(rows) if rows else 0.0
            print(f"\n[{seg}] n={len(rows)} sukses={sukses:.2f}% gagal={n_gagal}")
            if durs_ok:
                jit = (sum(abs(durs_ok[i] - durs_ok[i-1])
                           for i in range(1, len(durs_ok))) / (len(durs_ok) - 1)
                       if len(durs_ok) > 1 else 0.0)
                print(f"  durasi (ms): rata={statistics.mean(durs_ok):.2f} "
                      f"min={min(durs_ok):.2f} max={max(durs_ok):.2f} "
                      f"std={statistics.pstdev(durs_ok):.2f} | jitter={jit:.2f}")
        print(f"\nDetail per-event: {self.csv_file}")