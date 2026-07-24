# qos0_nrf_scan.py
# Analisis kinerja modul akuisisi nRF24 dari serial node (firmware uji NRFQOS).
# Pakai : python qos0_nrf_scan.py                       -> uji idle / umum
#         python qos0_nrf_scan.py --expect 26-48        -> validasi WiFi ch6
#         python qos0_nrf_scan.py --expect 1-23,51-73   -> beberapa rentang
# Stop  : Ctrl+C -> ringkasan + CSV
#
# Peta bin kanal nRF24 (freq = 2400 + n MHz) untuk sumber uji yang umum:
#   WiFi ch1  (2412 MHz, lebar 22 MHz) -> bin  1-23  (puncak 12)
#   WiFi ch6  (2437 MHz)               -> bin 26-48  (puncak 37)
#   WiFi ch11 (2462 MHz)               -> bin 51-73  (puncak 62)
#   BLE advertising                    -> bin 2, 26, 80
#   Remote/drone 2,4 GHz (FHSS)        -> tersebar hampir seluruh band

import re
import csv
import sys
import time
import argparse
import statistics
import serial
import serial.tools.list_ports

BAUD = 115200
TEORETIS_MS = 125 * (0.130 + 2.2)  # 130us PLL + 220x10us per kanal = ~291 ms

def auto_detect_port():
    ports = serial.tools.list_ports.comports()
    for p in ports:
        d = p.device.lower()
        if "usbserial" in d or "usbmodem" in d or "ttyusb" in d or "ttyacm" in d:
            return p.device
    return ports[0].device if ports else None

def parse_expect(teks):
    """'26-48' atau '1-23,51-73' -> set nomor bin kanal."""
    bins = set()
    if not teks:
        return bins
    for bagian in teks.split(","):
        a, b = bagian.split("-")
        bins.update(range(int(a), int(b) + 1))
    return bins

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--expect", default="",
                    help="rentang bin kanal sumber uji, mis. 26-48")
    args = ap.parse_args()

    expect = parse_expect(args.expect)
    port = args.port or auto_detect_port()
    if not port:
        print("Tidak ada port serial. Colok node yang sudah di-flash firmware uji.")
        sys.exit(1)

    print(f"Membuka {port} @ {BAUD} ... (Ctrl+C untuk berhenti)")
    ser = serial.Serial(port, BAUD, timeout=1)

    durasi_ms, aktif_per_sweep = [], []
    n_sweep = n_invalid = 0
    okupansi = [0] * 125          # berapa kali tiap kanal terdeteksi aktif
    hit_expect = 0                # sweep dengan >=1 kanal aktif di rentang expect
    fp_bins = 0                   # total kanal aktif DI LUAR rentang expect

    fname = f"qos0_nrf_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    fcsv = open(fname, "w", newline="", encoding="utf-8")
    w = csv.writer(fcsv)
    w.writerow(["seq", "durasi_ms", "panjang", "valid", "kanal_aktif", "data_hex"])

    t_start = time.perf_counter()
    try:
        while True:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line.startswith("NRFQOS,"):
                if "NRF24L01 Not Found" in line:
                    print("!! Modul nRF24 TIDAK TERDETEKSI oleh node — cek wiring.")
                continue
            try:
                _, s_seq, s_dur, s_len, s_valid, s_act, data = line.split(",", 6)
            except ValueError:
                continue

            n_sweep += 1
            d_ms = int(s_dur) / 1000.0
            valid = (s_valid == "1" and len(data) == 125
                     and all(c in "01" for c in data))
            if not valid:
                n_invalid += 1
            durasi_ms.append(d_ms)
            aktif = data.count("1")
            aktif_per_sweep.append(aktif)
            for i, c in enumerate(data[:125]):
                if c == "1":
                    okupansi[i] += 1
                    if expect and i not in expect:
                        fp_bins += 1
            if expect and any(data[i] == "1" for i in expect if i < len(data)):
                hit_expect += 1

            w.writerow([s_seq, f"{d_ms:.2f}", len(data), int(valid), aktif, data])
            if n_sweep % 20 == 0:
                print(f"  sweep {n_sweep} | durasi {d_ms:.1f} ms | "
                      f"kanal aktif {aktif}")
    except KeyboardInterrupt:
        pass
    finally:
        fcsv.close()
        ser.close()

    if not n_sweep:
        print("Tidak ada data NRFQOS diterima. Cek firmware uji sudah ter-flash?")
        return

    dur_total = time.perf_counter() - t_start
    jitter = (sum(abs(durasi_ms[i] - durasi_ms[i-1])
                  for i in range(1, len(durasi_ms))) / (len(durasi_ms) - 1)
              if len(durasi_ms) > 1 else 0.0)

    print("\n" + "=" * 68)
    print("RINGKASAN KINERJA MODUL AKUISISI nRF24")
    print("=" * 68)
    print(f"Jumlah sweep        : {n_sweep} dalam {dur_total:.0f} s "
          f"({n_sweep/dur_total:.2f} sweep/detik)")
    print(f"Waktu akuisisi (ms) : rata={statistics.mean(durasi_ms):.1f} "
          f"min={min(durasi_ms):.1f} max={max(durasi_ms):.1f} "
          f"std={statistics.pstdev(durasi_ms):.2f}  "
          f"(teoretis ~{TEORETIS_MS:.0f} ms)")
    print(f"Jitter akuisisi     : {jitter:.2f} ms")
    print(f"Integritas data     : {100.0*(n_sweep-n_invalid)/n_sweep:.2f} % "
          f"({n_invalid} sweep tidak valid)")
    print(f"Kanal aktif/sweep   : rata={statistics.mean(aktif_per_sweep):.2f} "
          f"min={min(aktif_per_sweep)} max={max(aktif_per_sweep)}")

    if expect:
        total_act = sum(okupansi)
        print(f"\nValidasi sumber uji (bin {sorted(expect)[0]}-{sorted(expect)[-1]}):")
        print(f"  Hit rate deteksi  : {100.0*hit_expect/n_sweep:.2f} % sweep")
        print(f"  Aktif di luar bin : {fp_bins} dari {total_act} "
              f"({100.0*fp_bins/total_act if total_act else 0:.2f} % — indikasi FP/interferensi lain)")
    else:
        fp_rate = 100.0 * sum(okupansi) / (125 * n_sweep)
        print(f"\nOkupansi total (mode idle = false positive rate): {fp_rate:.3f} % "
              f"dari seluruh slot kanal")

    top = sorted(range(125), key=lambda i: okupansi[i], reverse=True)[:10]
    print("\n10 kanal paling aktif (bin -> frekuensi, jumlah deteksi):")
    for i in top:
        if okupansi[i] == 0:
            break
        bar = "#" * int(40 * okupansi[i] / max(okupansi))
        print(f"  ch {i:3d} ({2400+i} MHz) : {okupansi[i]:5d} {bar}")
    print(f"\nCSV tersimpan: {fname}")

if __name__ == "__main__":
    main()