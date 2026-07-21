# -*- coding: utf-8 -*-
"""
============================================================================
RUNNER UJI QoS PER SEGMEN — LOW COST DRONE DETECTION (LCDD)
============================================================================
Menjalankan pengujian QoS untuk SATU segmen pipeline secara terpisah,
tanpa harus menjalankan seluruh sistem. Pasangan dari analyze_qos.py:
CSV keluaran runner ini bisa langsung dianalisis olehnya.

  SEGMEN 1  Node -> Gateway (LoRa)          : butuh gateway di port serial
  SEGMEN 2  Pipeline keputusan backend      : offline, dari dataset JSON
                                              atau qos_log.csv (tanpa alat)
  SEGMEN 3  Backend -> Firebase             : butuh internet saja
  SEGMEN 4  Firebase -> penerima realtime   : butuh backend/app.py berjalan;
            (simulasi frontend via Python)    hasilnya = qos_frontend_*.csv
  LOOPBACK  Firebase naik+turun terisolasi  : butuh internet saja

Contoh pemakaian (dari root repo Gateway_LCDD1):
  python qos_analysis/uji_qos_segmen.py segmen1 --durasi 300 --label jarak20m
  python qos_analysis/uji_qos_segmen.py segmen2 --sumber dataset_3nodes_20260623_110934.json
  python qos_analysis/uji_qos_segmen.py segmen2 --sumber qos_log.csv
  python qos_analysis/uji_qos_segmen.py segmen3 --jumlah 100 --jeda 0.5
  python qos_analysis/uji_qos_segmen.py segmen4 --durasi 300 --label wifi_kos
  python qos_analysis/uji_qos_segmen.py loopback --jumlah 50 --jeda 1.0

Catatan:
- SEGMEN 1 memakai port serial yang sama dengan backend/app.py — jangan
  dijalankan bersamaan dengan backend. Mode lain aman berjalan paralel.
- SEGMEN 3/4/LOOPBACK hanya menulis ke path uji `qos/uji/...` dan membaca
  `qos/live` — data deteksi produksi tidak disentuh.
============================================================================
"""

import argparse
import csv
import datetime
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_qos import (stat_ringkas, hitung_jitter, kategori_delay,
                         kategori_jitter, kategori_loss, to_float)

FIREBASE_URL = "https://lowcostdronedetect-default-rtdb.asia-southeast1.firebasedatabase.app"

# Header identik dengan qos_log.csv backend -> bisa dianalisis analyze_qos.py
HEADER_BACKEND = ("t_rx_iso,t_rx_epoch_ms,event,node,seq,rssi,snr,"
                  "poll_rtt_ms,node_proc_ms,link_delay_ms,payload_len,"
                  "fb_raw_ms,fb_raw_ok,decision,decision_ms,fb_pred_ms,"
                  "fb_pred_ok,fb_qos_ms,backend_total_ms,"
                  "rx_count,timeout_count,loss_pct").split(",")

# Header identik dengan export tab QoS website -> bisa jadi --frontend
HEADER_FRONTEND = ("node,seq,event,rssi,snr,poll_rtt_ms,node_proc_ms,"
                   "link_delay_ms,decision,decision_ms,backend_proc_ms,"
                   "fb_raw_ms,backend_to_fb_ms,fb_to_fe_ms,e2e_ms,"
                   "loss_pct,arrived_at_server_ms").split(",")


# ============================================================================
# UTILITAS UMUM
# ============================================================================
def stempel():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def simpan_csv(nama, header, rows):
    with open(nama, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"--> CSV tersimpan: {os.path.abspath(nama)}")
    return nama


def cetak_statistik(judul, daftar):
    """daftar = list of (nama_metrik, list_nilai, satuan, jenis_tiphon|None)"""
    print("\n" + "=" * 72)
    print(judul)
    print("=" * 72)
    print(f"{'Metrik':<34}{'N':>5}{'Rata2':>9}{'Min':>8}{'Maks':>8}"
          f"{'StDev':>8}{'Jitter':>8}")
    for nama, nilai, satuan, jenis in daftar:
        st = stat_ringkas(nilai)
        if st is None:
            print(f"{nama:<34}{'-':>5}")
            continue
        j = hitung_jitter(nilai)
        kat = ""
        if jenis == "delay":
            kat = f"  [TIPHON delay: {kategori_delay(st['mean'])[0]}"
            if j is not None:
                kat += f", jitter: {kategori_jitter(j)[0]}"
            kat += "]"
        j_txt = f"{j:.1f}" if j is not None else "-"
        print(f"{nama:<34}{st['n']:>5}{st['mean']:>9.1f}{st['min']:>8.1f}"
              f"{st['max']:>8.1f}{st['stdev']:>8.1f}"
              f"{j_txt:>8}  ({satuan}){kat}")
    print("=" * 72)


def firebase_req(path, method="GET", data=None, timeout=6):
    """Satu request REST ke Firebase. Return (obj_json, rtt_ms)."""
    url = f"{FIREBASE_URL}/{path}.json"
    body = None
    req = urllib.request.Request(url, method=method)
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    t0 = time.perf_counter()
    resp = urllib.request.urlopen(req, body, timeout=timeout)
    hasil = json.loads(resp.read().decode("utf-8") or "null")
    return hasil, (time.perf_counter() - t0) * 1000.0


def estimasi_offset(jumlah=8, path="qos/uji/clock_probe_runner"):
    """Offset jam lokal terhadap jam server Firebase (metode NTP sederhana).
    PUT sentinel .sv timestamp -> server mengembalikan nilai terselesaikan.
    offset = median( server_ts - titik_tengah(t0,t1) )."""
    offsets, rtts = [], []
    for _ in range(jumlah):
        t0 = time.time() * 1000.0
        server_ts, _ = firebase_req(path, "PUT", {".sv": "timestamp"})
        t1 = time.time() * 1000.0
        offsets.append(float(server_ts) - (t0 + t1) / 2.0)
        rtts.append(t1 - t0)
        time.sleep(0.2)
    off = statistics.median(offsets)
    print(f"[i] Offset jam lokal vs server Firebase: {off:+.1f} ms "
          f"(median {jumlah} probe, RTT median {statistics.median(rtts):.0f} ms)")
    return off


# ============================================================================
# SEGMEN 1 — NODE -> GATEWAY (SERIAL)
# ============================================================================
def parse_baris_serial(lines):
    """Generator event dari aliran baris serial gateway.
    Yields ("data", info) untuk tiap payload utuh, ("timeout", node) untuk
    tiap event poll_timeout. Logika sama dengan parser backend/app.py."""
    ctx = {"rssi": None, "snr": None, "rtt": None, "plen": None}
    for line in lines:
        m = re.search(r'"rssi"\s*:\s*(-?\d+)', line)
        if m and '"data_hex"' not in line:
            ctx["rssi"] = int(m.group(1))
        m = re.search(r'"snr"\s*:\s*(-?\d+\.?\d*)', line)
        if m and '"data_hex"' not in line:
            ctx["snr"] = float(m.group(1))
        m = re.search(r'"poll_rtt_ms"\s*:\s*(\d+)', line)
        if m and '"data_hex"' not in line:
            ctx["rtt"] = int(m.group(1))
        m = re.search(r'"payload_len"\s*:\s*(\d+)', line)
        if m and '"data_hex"' not in line:
            ctx["plen"] = int(m.group(1))

        if '"poll_timeout"' in line:
            m = re.search(r'"node"\s*:\s*"([^"]+)"', line)
            if m:
                yield "timeout", m.group(1)
            continue

        if '"data_hex"' in line:
            node = re.search(r'"node"\s*:\s*"([^"]+)"', line)
            seq = re.search(r'"seq"\s*:\s*(\d+)', line)
            proc = re.search(r'"proc"\s*:\s*(\d+)', line)
            info = {
                "node": node.group(1) if node else "?",
                "seq": int(seq.group(1)) if seq else None,
                "proc": int(proc.group(1)) if proc else None,
                "rssi": ctx["rssi"], "snr": ctx["snr"],
                "rtt": ctx["rtt"], "plen": ctx["plen"],
            }
            yield "data", info


def auto_detect_port():
    import serial.tools.list_ports
    ports = serial.tools.list_ports.comports()
    for port in ports:
        d = port.device.lower()
        if "usbserial" in d or "usbmodem" in d or "ttyusb" in d or "ttyacm" in d:
            return port.device
    return ports[0].device if ports else None


def uji_segmen1(args):
    import serial
    port = args.port or auto_detect_port()
    if not port:
        print("Error: port serial gateway tidak ditemukan.")
        sys.exit(1)
    print(f"[i] SEGMEN 1: membaca serial gateway di {port} selama "
          f"{args.durasi} detik (JANGAN jalankan backend/app.py bersamaan).")
    ser = serial.Serial(port, 115200, timeout=1)

    batas = time.time() + args.durasi

    def baris_serial():
        while time.time() < batas:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if line:
                yield line

    rows = []
    rx, to = {}, {}
    rtt_semua = []
    for jenis, ev in parse_baris_serial(baris_serial()):
        t = time.time()
        t_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        if jenis == "timeout":
            to[ev] = to.get(ev, 0) + 1
            total = rx.get(ev, 0) + to[ev]
            rows.append([t_iso, int(t * 1000), "timeout", ev] + [""] * 15 +
                        [rx.get(ev, 0), to[ev], round(100 * to[ev] / total, 2)])
            print(f"  [TIMEOUT] {ev} (total {to[ev]})")
        else:
            node = ev["node"]
            rx[node] = rx.get(node, 0) + 1
            total = rx[node] + to.get(node, 0)
            link = (ev["rtt"] - ev["proc"]
                    if ev["rtt"] is not None and ev["proc"] is not None else "")
            rows.append([t_iso, int(t * 1000), "deteksi", node, ev["seq"],
                         ev["rssi"], ev["snr"], ev["rtt"], ev["proc"], link,
                         ev["plen"], "", "", "", "", "", "", "", "",
                         rx[node], to.get(node, 0),
                         round(100 * to.get(node, 0) / total, 2)])
            if ev["rtt"] is not None:
                rtt_semua.append(float(ev["rtt"]))
            print(f"  [{len(rows):>4}] {node} seq={ev['seq']} "
                  f"rtt={ev['rtt']}ms rssi={ev['rssi']} snr={ev['snr']}")
    ser.close()

    n_rx = sum(rx.values())
    n_to = sum(to.values())
    loss = 100.0 * n_to / (n_rx + n_to) if (n_rx + n_to) else 0.0
    cetak_statistik(
        f"HASIL SEGMEN 1 (Node->Gateway) — {n_rx} paket, {n_to} timeout, "
        f"loss {loss:.1f}% [{kategori_loss(loss)[0]}]",
        [("RTT poll->balasan", rtt_semua, "ms", "delay")])

    nama = f"uji_segmen1_{args.label}_{stempel()}.csv"
    simpan_csv(nama, HEADER_BACKEND, rows)
    print(f"[i] Analisis lengkap: python qos_analysis/analyze_qos.py "
          f"--backend {nama} --label {args.label}")


# ============================================================================
# SEGMEN 2 — PIPELINE KEPUTUSAN BACKEND (OFFLINE)
# ============================================================================
# Salinan ringkas pipeline z-score backend/app.py (parameter identik)
WINDOW_SIZE, STRIDE, CALIB_SAMPLES = 5, 2, 20
K_FACTOR, MIN_STDEV_BURST, MIN_STDEV_ACTIVE = 3.0, 0.5, 1.0


def hex_to_bits(hex_str):
    return [1 if c in "123456789abcdefABCDEF" else 0 for c in hex_str]


def get_burst_count(bits):
    count, prev = 0, 0
    for b in bits:
        if b == 1 and prev == 0:
            count += 1
        prev = b
    return count


def uji_segmen2(args):
    sumber = args.sumber
    print(f"[i] SEGMEN 2: mengukur pipeline keputusan dari {sumber} (offline).")

    if sumber.endswith(".csv"):
        # qos_log.csv hasil pengukuran live: rangkum kolom decision/backend
        with open(sumber, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        n_kalib = sum(1 for r in rows if r.get("event") == "kalibrasi")
        print(f"[i] Frame fase kalibrasi di log: {n_kalib}")
        dec = [to_float(r.get("decision_ms")) for r in rows]
        dec = [x for x in dec if x is not None]
        tot = [to_float(r.get("backend_total_ms")) for r in rows]
        tot = [x for x in tot if x is not None]
        cetak_statistik(
            f"HASIL SEGMEN 2 (dari {os.path.basename(sumber)}) — "
            f"{len(dec)} keputusan",
            [("Komputasi keputusan murni", dec, "ms", None),
             ("Total backend per frame", tot, "ms", "delay")])
        return

    # Dataset JSON: [{payload:{node,data_hex},...}, ...] -> replay pipeline
    with open(sumber, encoding="utf-8") as f:
        dataset = json.load(f)

    frames = []
    for d in dataset:
        p = d.get("payload", {})
        hex_str = p.get("data_hex", "")
        if len(hex_str) == 125:
            frames.append((p.get("node", "?").replace(" ", ""), hex_str))
    print(f"[i] {len(frames)} frame valid dari {len(dataset)} entri dataset.")

    windows, strides, calib, frozen = {}, {}, {}, {}
    hasil_dec, rows_csv = [], []
    n_drone = 0
    for i, (node, hex_str) in enumerate(frames):
        bits = hex_to_bits(hex_str)
        calib.setdefault(node, [])
        windows.setdefault(node, [])
        strides.setdefault(node, 0)

        # ====== FASE 1: KALIBRASI DINAMIS (identik backend/app.py) ======
        if node not in frozen:
            fb = get_burst_count(bits)
            fa = sum(bits)
            calib[node].append((fb, fa))
            n = len(calib[node])
            print(f"[KALIBRASI {node}] frame {n}/{CALIB_SAMPLES} "
                  f"| burst={fb} | active={fa}")
            rows_csv.append([i, node, "kalibrasi", "", "", fb, fa])

            if n == CALIB_SAMPLES:
                b = [x[0] for x in calib[node]]
                a = [x[1] for x in calib[node]]
                sb = max(MIN_STDEV_BURST, statistics.stdev(b))
                sa = max(MIN_STDEV_ACTIVE, statistics.stdev(a))
                frozen[node] = (statistics.mean(b) + K_FACTOR * sb,
                                statistics.mean(a) + K_FACTOR * sa)
                print(f"[i] KALIBRASI {node} SELESAI - baseline dikunci "
                      f"(thr_burst={frozen[node][0]:.2f}, "
                      f"thr_active={frozen[node][1]:.2f})")
            continue

        # ====== FASE 2: DETEKSI ======
        windows[node].append(bits)
        if len(windows[node]) > WINDOW_SIZE:
            windows[node].pop(0)
        strides[node] += 1
        if len(windows[node]) == WINDOW_SIZE and strides[node] >= STRIDE:
            strides[node] = 0
            t0 = time.perf_counter()
            bm = sum(get_burst_count(r) for r in windows[node]) / WINDOW_SIZE
            am = sum(sum(r) for r in windows[node]) / WINDOW_SIZE
            drone = bm >= frozen[node][0] or am >= frozen[node][1]
            ms = (time.perf_counter() - t0) * 1000.0
            hasil_dec.append(ms)
            n_drone += 1 if drone else 0
            rows_csv.append([i, node, "deteksi", "DRONE" if drone else "AMAN",
                             round(ms, 4), round(bm, 2), round(am, 2)])

    cetak_statistik(
        f"HASIL SEGMEN 2 (replay {os.path.basename(sumber)}) — "
        f"{len(hasil_dec)} keputusan ({n_drone} DRONE)",
        [("Komputasi keputusan murni", hasil_dec, "ms", None)])
    print("[i] Komputasi < 1 ms membuktikan backend bukan bottleneck pipeline;"
          "\n    delay keputusan didominasi menunggu window "
          f"({WINDOW_SIZE} frame) x siklus polling.")
    nama = f"uji_segmen2_{args.label}_{stempel()}.csv"
    simpan_csv(nama, ["frame_ke", "node", "fase", "keputusan", "decision_ms",
                      "burst_mean", "active_mean"], rows_csv)


# ============================================================================
# SEGMEN 3 — BACKEND -> FIREBASE (PROBE AKTIF)
# ============================================================================
def uji_segmen3(args):
    print(f"[i] SEGMEN 3: {args.jumlah} probe PATCH ke Firebase "
          f"(path uji qos/uji/segmen3), jeda {args.jeda} dtk.")
    # Payload berukuran representatif = PATCH data mentah detection_system
    beban = {
        "node": "uji_segmen3",
        "data_hex": "0" * 125,
        "timestamp_wib": "00:00:00",
        "captured_at": "-",
        "rssi": -40,
        "snr": 9.5,
    }
    rtts, ups, downs, gagal = [], [], [], 0
    rows = []
    probes = []
    for i in range(args.jumlah):
        try:
            _, rtt = firebase_req("qos/uji/segmen3", "PATCH", beban)
            t0 = time.time() * 1000.0
            server_ts, _ = firebase_req("qos/uji/clock_probe_runner", "PUT",
                                        {".sv": "timestamp"})
            t1 = time.time() * 1000.0
            rtts.append(rtt)
            probes.append((t0, float(server_ts), t1))
            print(f"  [{i + 1:>3}/{args.jumlah}] PATCH rtt={rtt:.0f} ms")
        except Exception as e:
            gagal += 1
            print(f"  [{i + 1:>3}/{args.jumlah}] GAGAL: {e}")
        time.sleep(args.jeda)

    # Offset dari seluruh probe -> estimasi one-way per probe
    if probes:
        offset = statistics.median(s - (a + b) / 2 for a, s, b in probes)
        for a, s, b in probes:
            ups.append(s - (a + offset))
            downs.append((b + offset) - s)
        print(f"[i] Offset jam lokal vs server: {offset:+.1f} ms (median)")

    for i, rtt in enumerate(rtts):
        rows.append([i + 1, round(rtt, 1),
                     round(ups[i], 1) if i < len(ups) else "",
                     round(downs[i], 1) if i < len(downs) else ""])

    loss = 100.0 * gagal / args.jumlah if args.jumlah else 0.0
    cetak_statistik(
        f"HASIL SEGMEN 3 (Backend->Firebase) — {len(rtts)} sukses, "
        f"{gagal} gagal ({loss:.1f}% [{kategori_loss(loss)[0]}])",
        [("RTT HTTP PATCH (beban nyata)", rtts, "ms", "delay"),
         ("Estimasi satu arah naik", ups, "ms", "delay"),
         ("Estimasi satu arah turun", downs, "ms", "delay")])
    simpan_csv(f"uji_segmen3_{args.label}_{stempel()}.csv",
               ["probe_ke", "rtt_ms", "up_ms", "down_ms"], rows)


# ============================================================================
# SEGMEN 4 — FIREBASE -> PENERIMA REALTIME (SIMULASI FRONTEND)
# ============================================================================
def stream_sse(path, batas_waktu):
    """Generator event streaming REST Firebase (Server-Sent Events).
    Yields (nama_event, obj) sampai batas_waktu terlewati."""
    req = urllib.request.Request(
        f"{FIREBASE_URL}/{path}.json",
        headers={"Accept": "text/event-stream"})
    resp = urllib.request.urlopen(req, timeout=60)
    nama = None
    for raw in resp:
        if time.time() > batas_waktu:
            break
        line = raw.decode("utf-8", errors="ignore").strip()
        if line.startswith("event:"):
            nama = line[6:].strip()
        elif line.startswith("data:"):
            body = line[5:].strip()
            if nama in ("put", "patch") and body and body != "null":
                try:
                    yield nama, json.loads(body)
                except json.JSONDecodeError:
                    pass


def ekstrak_records(obj):
    """Dari satu event SSE, keluarkan daftar record QoS (punya server_ts).
    path "/" saat snapshot awal berisi semua node sekaligus."""
    data = obj.get("data")
    if not isinstance(data, dict):
        return []
    if "server_ts" in data:
        return [data]
    return [v for v in data.values()
            if isinstance(v, dict) and "server_ts" in v]


def uji_segmen4(args):
    print(f"[i] SEGMEN 4: mendengarkan qos/live selama {args.durasi} dtk "
          f"(pastikan backend/app.py sedang berjalan & node aktif).")
    offset = estimasi_offset()
    try:
        backend_off, _ = firebase_req("qos/meta/backend_offset_ms")
        backend_off = float(backend_off)
        print(f"[i] Offset jam backend (dari qos/meta): {backend_off:+.1f} ms")
    except Exception:
        backend_off = None
        print("[!] qos/meta/backend_offset_ms belum ada — backend belum "
              "berjalan? Metrik lintas jam memakai offset 0 (kurang akurat).")

    mulai_server = time.time() * 1000.0 + offset
    batas = time.time() + args.durasi
    rows, fbfe_semua, e2e_semua = [], [], []
    terakhir = {}

    for _, obj in stream_sse("qos/live", batas):
        t_arr_server = time.time() * 1000.0 + offset
        for rec in ekstrak_records(obj):
            # Lewati snapshot awal / record lama (tertulis sebelum uji mulai)
            if float(rec.get("server_ts", 0)) < mulai_server - 2000:
                continue
            kunci = (rec.get("node"), rec.get("seq"), rec.get("server_ts"))
            if terakhir.get(rec.get("node")) == kunci:
                continue
            terakhir[rec.get("node")] = kunci

            boff = backend_off if backend_off is not None else 0.0
            fbfe = t_arr_server - float(rec["server_ts"])
            b2fb = (float(rec["server_ts"]) - (float(rec["backend_sent_at"]) + boff)
                    if rec.get("backend_sent_at") else None)
            bproc = (float(rec["backend_sent_at"]) - float(rec["backend_rx_at"])
                     if rec.get("backend_sent_at") and rec.get("backend_rx_at")
                     else None)
            e2e = (float(rec["poll_rtt_ms"]) +
                   (t_arr_server - (float(rec["backend_rx_at"]) + boff))
                   if rec.get("poll_rtt_ms") is not None and
                   rec.get("backend_rx_at") else None)

            fbfe_semua.append(fbfe)
            if e2e is not None:
                e2e_semua.append(e2e)
            rows.append([
                rec.get("node", "?"), rec.get("seq", ""),
                rec.get("event", ""), rec.get("rssi", ""), rec.get("snr", ""),
                rec.get("poll_rtt_ms", ""), rec.get("node_proc_ms", ""),
                rec.get("link_delay_ms", ""), rec.get("decision", ""),
                rec.get("decision_ms", ""),
                round(bproc, 1) if bproc is not None else "",
                rec.get("fb_raw_ms", ""),
                round(b2fb, 1) if b2fb is not None else "",
                round(fbfe, 1),
                round(e2e, 1) if e2e is not None else "",
                rec.get("loss_pct", ""), int(t_arr_server),
            ])
            print(f"  [{len(rows):>4}] {rec.get('node')} seq={rec.get('seq')} "
                  f"FB->penerima={fbfe:.0f} ms"
                  f"{f' e2e={e2e:.0f} ms' if e2e is not None else ''}")

    cetak_statistik(
        f"HASIL SEGMEN 4 (Firebase->penerima realtime) — {len(rows)} record",
        [("Firebase -> penerima", fbfe_semua, "ms", "delay"),
         ("End-to-End poll -> penerima", e2e_semua, "ms", "delay")])
    nama = f"qos_frontend_python_{args.label}_{stempel()}.csv"
    simpan_csv(nama, HEADER_FRONTEND, rows)
    print(f"[i] Gabungkan dengan log backend: python qos_analysis/analyze_qos.py"
          f" --backend qos_log.csv --frontend {nama} --label {args.label}\n"
          f"[i] Nilai dari browser sungguhan tetap diambil dari tab QoS "
          f"website (Export CSV); mode ini adalah pembanding/otomasinya.")


# ============================================================================
# LOOPBACK — FIREBASE NAIK+TURUN TERISOLASI (TANPA ALAT)
# ============================================================================
def uji_loopback(args):
    print(f"[i] LOOPBACK: tulis {args.jumlah} record ke qos/uji/loopback dan "
          f"dengarkan sendiri (uji jalur Firebase terisolasi, tanpa alat).")
    offset = estimasi_offset()

    def penulis():
        time.sleep(2.0)  # beri waktu stream tersambung
        for i in range(args.jumlah):
            try:
                firebase_req("qos/uji/loopback", "PATCH", {
                    "seq": i + 1,
                    "sent_at": int(time.time() * 1000),
                    "server_ts": {".sv": "timestamp"},
                })
            except Exception as e:
                print(f"  [tulis {i + 1}] GAGAL: {e}")
            time.sleep(args.jeda)

    t = threading.Thread(target=penulis, daemon=True)
    t.start()

    batas = time.time() + 2.0 + args.jumlah * args.jeda + 10.0
    ups, downs, rows = [], [], []
    terlihat = set()
    for _, obj in stream_sse("qos/uji/loopback", batas):
        t_arr = time.time() * 1000.0 + offset
        data = obj.get("data")
        if not isinstance(data, dict) or "server_ts" not in data:
            continue
        seq = data.get("seq")
        if seq in terlihat or seq is None:
            continue
        terlihat.add(seq)
        naik = float(data["server_ts"]) - (float(data["sent_at"]) + offset)
        turun = t_arr - float(data["server_ts"])
        ups.append(naik)
        downs.append(turun)
        rows.append([seq, round(naik, 1), round(turun, 1),
                     round(naik + turun, 1)])
        print(f"  [{len(rows):>3}/{args.jumlah}] naik={naik:.0f} ms "
              f"turun={turun:.0f} ms")
        if len(terlihat) >= args.jumlah:
            break

    loss = 100.0 * (args.jumlah - len(rows)) / args.jumlah if args.jumlah else 0
    cetak_statistik(
        f"HASIL LOOPBACK Firebase — {len(rows)}/{args.jumlah} record "
        f"(loss {loss:.1f}% [{kategori_loss(loss)[0]}])",
        [("Naik (tulis -> server)", ups, "ms", "delay"),
         ("Turun (server -> penerima)", downs, "ms", "delay"),
         ("Total naik+turun", [u + d for u, d in zip(ups, downs)], "ms", "delay")])
    simpan_csv(f"uji_loopback_{args.label}_{stempel()}.csv",
               ["seq", "naik_ms", "turun_ms", "total_ms"], rows)


# ============================================================================
# MAIN
# ============================================================================
def main():
    p = argparse.ArgumentParser(
        description="Runner uji QoS per segmen sistem LCDD",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Contoh pemakaian")[1].split("Catatan:")[0])
    sub = p.add_subparsers(dest="mode", required=True)

    s1 = sub.add_parser("segmen1", help="Node->Gateway via serial gateway")
    s1.add_argument("--port", default=None, help="port serial (auto bila kosong)")
    s1.add_argument("--durasi", type=int, default=300, help="detik (default 300)")

    s2 = sub.add_parser("segmen2", help="pipeline keputusan (offline)")
    s2.add_argument("--sumber", required=True,
                    help="dataset_*.json (replay) atau qos_log.csv (rangkum)")

    s3 = sub.add_parser("segmen3", help="Backend->Firebase (probe aktif)")
    s3.add_argument("--jumlah", type=int, default=100)
    s3.add_argument("--jeda", type=float, default=0.5, help="detik antar probe")

    s4 = sub.add_parser("segmen4", help="Firebase->penerima realtime "
                                        "(butuh backend berjalan)")
    s4.add_argument("--durasi", type=int, default=300, help="detik (default 300)")

    lb = sub.add_parser("loopback", help="uji Firebase terisolasi (tanpa alat)")
    lb.add_argument("--jumlah", type=int, default=50)
    lb.add_argument("--jeda", type=float, default=1.0, help="detik antar tulis")

    for s in (s1, s2, s3, s4, lb):
        s.add_argument("--label", default="uji", help="nama skenario")

    args = p.parse_args()
    {"segmen1": uji_segmen1, "segmen2": uji_segmen2, "segmen3": uji_segmen3,
     "segmen4": uji_segmen4, "loopback": uji_loopback}[args.mode](args)


if __name__ == "__main__":
    main()
