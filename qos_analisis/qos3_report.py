# qos3_report.py
# Rekap QoS keseluruhan sistem + klasifikasi standar ETSI TIPHON TR 101 329.
# Pakai: python qos3_report.py qos_gateway_log.csv [qos_frontend_xxx.csv]
import csv
import sys
import statistics

def kat_delay(ms):
    if ms < 150:  return "Sangat Bagus (4)"
    if ms < 300:  return "Bagus (3)"
    if ms < 450:  return "Sedang (2)"
    return "Jelek (1)"

def kat_jitter(ms):
    if ms == 0:   return "Sangat Bagus (4)"
    if ms < 75:   return "Bagus (3)"
    if ms < 125:  return "Sedang (2)"
    return "Jelek (1)"

def kat_loss(pct):
    if pct == 0:  return "Sangat Bagus (4)"
    if pct < 3:   return "Bagus (3)"
    if pct < 15:  return "Sedang (2)"
    return "Jelek (1)"

def stat(durs):
    jit = (sum(abs(durs[i] - durs[i-1]) for i in range(1, len(durs)))
           / (len(durs) - 1)) if len(durs) > 1 else 0.0
    return statistics.mean(durs), min(durs), max(durs), statistics.pstdev(durs), jit

def main():
    if len(sys.argv) < 2:
        print("Pakai: python qos3_report.py qos_gateway_log.csv [qos_frontend.csv]")
        return

    seg = {}  # segmen -> {"ok": [durasi...], "gagal": n}
    with open(sys.argv[1], encoding="utf-8") as f:
        for row in csv.DictReader(f):
            s = seg.setdefault(row["segmen"], {"ok": [], "gagal": 0})
            if row["ok"] == "1":
                s["ok"].append(float(row["durasi_ms"]))
            else:
                s["gagal"] += 1

    fe = []  # latensi frontend (raw)
    if len(sys.argv) > 2:
        with open(sys.argv[2], encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["jenis"] == "raw":
                    fe.append(float(row["latency_ms"]))

    print("\n## Tabel QoS per Segmen Sistem\n")
    print("| Segmen | n | Delay rata (ms) | Min/Maks (ms) | Jitter (ms) | Loss (%) | Kat. Delay | Kat. Jitter | Kat. Loss |")
    print("|---|---|---|---|---|---|---|---|---|")

    urutan = ["lora_link", "kalibrasi_compute", "kalibrasi_total",
              "decision", "firebase_upload", "gw_total_frame"]
    nama = {"lora_link": "Node->Gateway (LoRa)",
            "kalibrasi_compute": "Kalibrasi (komputasi baseline)",
            "kalibrasi_total": "Kalibrasi (total 20 frame)",
            "decision": "Decision (ekstraksi fitur+threshold)",
            "firebase_upload": "Gateway->Firebase (PATCH)",
            "gw_total_frame": "Gateway total per frame keputusan"}

    e2e_delay, e2e_sukses = 0.0, 1.0
    for k in urutan:
        if k not in seg or not seg[k]["ok"]:
            continue
        d = seg[k]["ok"]
        n_tot = len(d) + seg[k]["gagal"]
        loss = 100.0 * seg[k]["gagal"] / n_tot
        mean, mn, mx, _, jit = stat(d)
        print(f"| {nama[k]} | {n_tot} | {mean:.1f} | {mn:.1f}/{mx:.1f} | {jit:.1f} "
              f"| {loss:.2f} | {kat_delay(mean)} | {kat_jitter(jit)} | {kat_loss(loss)} |")
        if k in ("lora_link", "decision", "firebase_upload"):
            e2e_delay += mean
            e2e_sukses *= (1 - loss / 100.0)

    if fe:
        mean, mn, mx, _, jit = stat(fe)
        print(f"| Firebase->Frontend | {len(fe)} | {mean:.1f} | {mn:.1f}/{mx:.1f} "
              f"| {jit:.1f} | - | {kat_delay(mean)} | {kat_jitter(jit)} | - |")

    print("\n## QoS Keseluruhan Sistem (End-to-End)\n")
    print(f"- Delay E2E (node -> web) : {e2e_delay:.1f} ms "
          f"(LoRa + decision + upload"
          + (f"; Firebase->web sudah termasuk di dalam latensi gw_epoch_ms frontend" if fe else "") + ")")
    print(f"- Keandalan pengiriman    : {100*e2e_sukses:.2f} % "
          f"(perkalian sukses tiap segmen)")
    print(f"- Kategori delay TIPHON   : {kat_delay(e2e_delay)}")
    print("\nCatatan: ambang TIPHON dirancang untuk trafik real-time (VoIP); untuk "
          "sistem monitoring berbasis polling LoRa, delay ratusan ms adalah "
          "konsekuensi airtime SF7 + scan nRF24 (~620 ms per frame), bukan degradasi jaringan. "
          "Sampaikan ini sebagai justifikasi di analisis.")

if __name__ == "__main__":
    main()