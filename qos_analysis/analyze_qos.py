# -*- coding: utf-8 -*-
"""
============================================================================
ANALISIS QoS END-TO-END — LOW COST DRONE DETECTION (LCDD)
============================================================================
Menggabungkan dua sumber data pengukuran:

  1. qos_log.csv          <- ditulis backend/app.py (WAJIB)
     Segmen 1 (Node->Gateway), Segmen 2 (proses backend/keputusan),
     Segmen 3 (Backend->Firebase, RTT PATCH), packet loss (timeout & seq).

  2. qos_frontend_*.csv   <- diekspor tab "QoS" di website (OPSIONAL)
     Segmen 3 one-way (Backend->Firebase, jam server), Segmen 4
     (Firebase->Frontend), dan End-to-End (poll gateway -> browser).

Keluaran (folder hasil_qos_<label>_<timestamp>/):
  - laporan_qos.md      : tabel siap salin ke laporan skripsi (+ rumus)
  - ringkasan_qos.csv   : angka ringkasan untuk diolah ulang di Excel
  - grafik_*.png        : grafik siap pakai (butuh matplotlib; opsional)

Cara pakai:
  python qos_analysis/analyze_qos.py --backend qos_log.csv \
         [--frontend qos_frontend_20260721_1030.csv] [--label wifi_ramai]

Metrik mengikuti definisi umum QoS jaringan (ETSI TIPHON TR 101 329):
  Delay/latensi, Jitter (variasi delay), Packet Loss, Throughput.
============================================================================
"""

import argparse
import csv
import datetime
import os
import statistics
import sys

# Palet grafik (lolos uji keterbacaan buta warna / CVD):
WARNA_NODE = {"Node1": "#2563EB", "Node2": "#EA580C", "Node3": "#059669"}
WARNA_UTAMA = "#2563EB"

# Batas wajar selisih seq; di atas ini dianggap node restart, bukan loss
MAX_SEQ_GAP = 50


# ============================================================================
# UTILITAS STATISTIK
# ============================================================================
def to_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except ValueError:
        return None


def stat_ringkas(values):
    """mean/min/max/stdev untuk list angka (None di-skip)."""
    v = [x for x in values if x is not None]
    if not v:
        return None
    return {
        "n": len(v),
        "mean": statistics.mean(v),
        "min": min(v),
        "max": max(v),
        "stdev": statistics.stdev(v) if len(v) > 1 else 0.0,
    }


def hitung_jitter(values):
    """Jitter = rata-rata |selisih delay dua paket berurutan| (variasi delay).
    Definisi sederhana yang lazim dipakai pada pengukuran QoS berbasis TIPHON."""
    v = [x for x in values if x is not None]
    if len(v) < 2:
        return None
    return sum(abs(v[i] - v[i - 1]) for i in range(1, len(v))) / (len(v) - 1)


# ============================================================================
# KATEGORISASI TIPHON (ETSI TR 101 329)
# ============================================================================
def kategori_delay(ms):
    if ms is None:
        return ("-", "-")
    if ms < 150:
        return ("Sangat Bagus", 4)
    if ms < 300:
        return ("Bagus", 3)
    if ms < 450:
        return ("Sedang", 2)
    return ("Jelek", 1)


def kategori_jitter(ms):
    if ms is None:
        return ("-", "-")
    if ms == 0:
        return ("Sangat Bagus", 4)
    if ms < 75:
        return ("Bagus", 3)
    if ms < 125:
        return ("Sedang", 2)
    return ("Jelek", 1)


def kategori_loss(pct):
    if pct is None:
        return ("-", "-")
    if pct == 0:
        return ("Sangat Bagus", 4)
    if pct < 3:
        return ("Bagus", 3)
    if pct < 15:
        return ("Sedang", 2)
    return ("Jelek", 1)


# ============================================================================
# PEMBACAAN CSV
# ============================================================================
def baca_backend_csv(path):
    data_rows, timeout_rows = [], []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("event") == "timeout":
                timeout_rows.append(row)
            else:
                data_rows.append(row)
    return data_rows, timeout_rows


def baca_frontend_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def kelompokkan_per_node(rows, kolom_node="node"):
    per_node = {}
    for r in rows:
        per_node.setdefault(r.get(kolom_node, "?"), []).append(r)
    return per_node


def ambil(rows, kolom):
    return [to_float(r.get(kolom)) for r in rows]


# ============================================================================
# ANALISIS PACKET LOSS DARI NOMOR URUT (seq)
# ============================================================================
def loss_dari_seq(rows_node):
    """Celah nomor urut = balasan yang dikirim node tetapi tidak sampai backend.
    Membedakan arah hilangnya paket:
      - timeout TANPA celah seq  -> poll gateway tidak sampai ke node
      - timeout DENGAN celah seq -> balasan node hilang di udara"""
    seqs = [to_float(r.get("seq")) for r in rows_node]
    seqs = [int(s) for s in seqs if s is not None]
    gap_total = 0
    for i in range(1, len(seqs)):
        d = seqs[i] - seqs[i - 1]
        if 1 < d < MAX_SEQ_GAP:
            gap_total += d - 1
    return gap_total


# ============================================================================
# ANALISIS UTAMA
# ============================================================================
def analisis(backend_csv, frontend_csv, label):
    data_rows, timeout_rows = baca_backend_csv(backend_csv)
    if not data_rows:
        print("Tidak ada baris data di", backend_csv)
        sys.exit(1)

    fe_rows = baca_frontend_csv(frontend_csv) if frontend_csv else []

    be_per_node = kelompokkan_per_node(data_rows)
    to_per_node = kelompokkan_per_node(timeout_rows)
    fe_per_node = kelompokkan_per_node(fe_rows)

    nodes = sorted(set(be_per_node) | set(to_per_node))

    hasil = {"label": label, "nodes": {}, "durasi_s": 0.0}

    t_all = [to_float(r.get("t_rx_epoch_ms")) for r in data_rows]
    t_all = [t for t in t_all if t is not None]
    if len(t_all) >= 2:
        hasil["durasi_s"] = (max(t_all) - min(t_all)) / 1000.0

    for node in nodes:
        rows = be_per_node.get(node, [])
        n_rx = len(rows)
        n_to = len(to_per_node.get(node, []))
        seq_gap = loss_dari_seq(rows)

        rtt = ambil(rows, "poll_rtt_ms")
        link = ambil(rows, "link_delay_ms")
        proc = ambil(rows, "node_proc_ms")
        dec = [x for x in ambil(rows, "decision_ms") if x is not None]
        fb_raw = ambil(rows, "fb_raw_ms")
        fb_pred = [x for x in ambil(rows, "fb_pred_ms") if x is not None]
        be_total = ambil(rows, "backend_total_ms")

        # Throughput sesi segmen LoRa: total bit payload / durasi pengamatan node
        plen = [x for x in ambil(rows, "payload_len") if x is not None]
        t_node = [to_float(r.get("t_rx_epoch_ms")) for r in rows]
        t_node = [t for t in t_node if t is not None]
        durasi_node = (max(t_node) - min(t_node)) / 1000.0 if len(t_node) >= 2 else 0
        throughput_bps = (sum(plen) * 8 / durasi_node) if durasi_node > 0 else None

        d = {
            "rx": n_rx,
            "timeout": n_to,
            "loss_pct": 100.0 * n_to / (n_rx + n_to) if (n_rx + n_to) else 0.0,
            "seq_gap": seq_gap,
            "loss_seq_pct": 100.0 * seq_gap / (n_rx + seq_gap) if (n_rx + seq_gap) else 0.0,
            "rssi": stat_ringkas(ambil(rows, "rssi")),
            "snr": stat_ringkas(ambil(rows, "snr")),
            "poll_rtt": stat_ringkas(rtt),
            "jitter_rtt": hitung_jitter(rtt),
            "link_delay": stat_ringkas(link),
            "node_proc": stat_ringkas(proc),
            "decision_ms": stat_ringkas(dec),
            "fb_raw": stat_ringkas(fb_raw),
            "fb_pred": stat_ringkas(fb_pred),
            "backend_total": stat_ringkas(be_total),
            "throughput_bps": throughput_bps,
        }

        # ---- Data frontend (bila ada) ----
        fe = fe_per_node.get(node, [])
        if fe:
            fbfe = ambil(fe, "fb_to_fe_ms")
            e2e = ambil(fe, "e2e_ms")
            d["fe_n"] = len(fe)
            d["backend_to_fb"] = stat_ringkas(ambil(fe, "backend_to_fb_ms"))
            d["fb_to_fe"] = stat_ringkas(fbfe)
            d["jitter_fb_to_fe"] = hitung_jitter(fbfe)
            d["e2e"] = stat_ringkas(e2e)
            d["jitter_e2e"] = hitung_jitter(e2e)
            d["backend_proc_fe"] = stat_ringkas(ambil(fe, "backend_proc_ms"))

        hasil["nodes"][node] = d

    return hasil, data_rows, timeout_rows, fe_rows


# ============================================================================
# PENULISAN LAPORAN
# ============================================================================
def fmt(v, digits=1):
    if v is None:
        return "-"
    return f"{v:.{digits}f}"


def baris_stat(nama, st, jitter=None):
    if st is None:
        return f"| {nama} | - | - | - | - | - | - |\n"
    j = fmt(jitter) if jitter is not None else "-"
    return (f"| {nama} | {st['n']} | {fmt(st['mean'])} | {fmt(st['min'])} "
            f"| {fmt(st['max'])} | {fmt(st['stdev'])} | {j} |\n")


def tulis_laporan(hasil, outdir, backend_csv, frontend_csv):
    label = hasil["label"]
    path = os.path.join(outdir, "laporan_qos.md")
    n = hasil["nodes"]

    with open(path, "w", encoding="utf-8") as f:
        f.write("# Laporan Analisis QoS End-to-End — Sistem LCDD\n\n")
        f.write(f"**Skenario:** {label}\n")
        f.write(f"**Waktu analisis:** {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"**Sumber backend:** `{os.path.basename(backend_csv)}`\n")
        if frontend_csv:
            f.write(f"**Sumber frontend:** `{os.path.basename(frontend_csv)}`\n")
        f.write(f"**Durasi pengamatan:** {hasil['durasi_s']:.1f} detik\n\n")

        f.write("## 1. Arsitektur & Titik Ukur\n\n")
        f.write("```\n")
        f.write("Node (nRF24 scan + ESP32) --LoRa--> Gateway (ESP32) --Serial--> Backend (laptop)\n")
        f.write("      Segmen 1: poll_rtt_ms                              Segmen 2: decision_ms\n")
        f.write("Backend --HTTP PATCH--> Firebase RTDB --WebSocket--> Frontend (browser)\n")
        f.write("      Segmen 3: fb_raw_ms / backend_to_fb_ms   Segmen 4: fb_to_fe_ms\n")
        f.write("```\n\n")

        # ------------------------------------------------------------------
        f.write("## 2. Segmen 1 — Node -> Gateway (LoRa 433 MHz)\n\n")
        f.write("Delay diukur gateway dengan jam lokalnya sendiri (bebas masalah "
                "sinkronisasi jam): RTT = paket poll dikirim -> balasan node "
                "diterima utuh. `link_delay` = RTT dikurangi waktu proses node "
                "(scan nRF24), yaitu murni waktu di udara + antrean radio.\n\n")
        for node, d in n.items():
            f.write(f"### {node}\n\n")
            f.write("| Metrik | N | Rata-rata | Min | Maks | StDev | Jitter |\n")
            f.write("|---|---|---|---|---|---|---|\n")
            f.write(baris_stat("RTT poll->balasan (ms)", d["poll_rtt"], d["jitter_rtt"]))
            f.write(baris_stat("Delay link LoRa murni (ms)", d["link_delay"]))
            f.write(baris_stat("Waktu proses node / scan (ms)", d["node_proc"]))
            f.write(baris_stat("RSSI (dBm)", d["rssi"]))
            f.write(baris_stat("SNR (dB)", d["snr"]))
            f.write("\n")
            f.write(f"- Paket diterima: **{d['rx']}**, poll timeout: **{d['timeout']}** "
                    f"-> packet loss: **{fmt(d['loss_pct'])}%**\n")
            f.write(f"- Celah nomor urut (balasan node hilang di udara): "
                    f"**{d['seq_gap']}** paket ({fmt(d['loss_seq_pct'])}%). "
                    f"Timeout tanpa celah seq berarti poll gateway yang tidak "
                    f"sampai ke node.\n")
            if d["throughput_bps"] is not None:
                f.write(f"- Throughput sesi: **{fmt(d['throughput_bps'])} bps** "
                        f"(payload LoRa / durasi pengamatan; polling round-robin "
                        f"membuat nilai ini dibatasi desain siklus, bukan kapasitas link)\n")
            f.write("\n")

        # ------------------------------------------------------------------
        f.write("## 3. Segmen 2 — Pemrosesan Keputusan di Backend\n\n")
        f.write("`decision_ms` = lama komputasi keputusan murni (window z-score). "
                "`backend_total_ms` = frame tiba di serial -> selesai seluruh "
                "pipeline (termasuk upload Firebase). Catatan desain: keputusan "
                "baru keluar setelah window berisi WINDOW_SIZE frame dan tiap "
                "STRIDE frame — jeda agregasi ini adalah konsekuensi desain "
                "deteksi, bukan delay jaringan.\n\n")
        f.write("| Node | N keputusan | Komputasi (ms) | Maks (ms) | Total backend/frame (ms) |\n")
        f.write("|---|---|---|---|---|\n")
        for node, d in n.items():
            dm, bt = d["decision_ms"], d["backend_total"]
            f.write(f"| {node} | {dm['n'] if dm else 0} | "
                    f"{fmt(dm['mean'], 3) if dm else '-'} | "
                    f"{fmt(dm['max'], 3) if dm else '-'} | "
                    f"{fmt(bt['mean']) if bt else '-'} |\n")
        f.write("\n")

        # ------------------------------------------------------------------
        f.write("## 4. Segmen 3 — Backend -> Firebase\n\n")
        f.write("`fb_raw_ms` / `fb_pred_ms` = RTT HTTP PATCH penuh diukur backend. "
                "`backend_to_fb` (bila ada data frontend) = latensi satu arah "
                "terhadap jam server Firebase.\n\n")
        for node, d in n.items():
            f.write(f"### {node}\n\n")
            f.write("| Metrik | N | Rata-rata | Min | Maks | StDev | Jitter |\n")
            f.write("|---|---|---|---|---|---|---|\n")
            f.write(baris_stat("PATCH data mentah, RTT (ms)", d["fb_raw"]))
            f.write(baris_stat("PATCH keputusan, RTT (ms)", d["fb_pred"]))
            if "backend_to_fb" in d:
                f.write(baris_stat("Satu arah Backend->FB (ms)", d["backend_to_fb"]))
            f.write("\n")

        # ------------------------------------------------------------------
        if any("fb_to_fe" in d for d in n.values()):
            f.write("## 5. Segmen 4 — Firebase -> Frontend (Website)\n\n")
            f.write("Diukur di browser terhadap jam server Firebase "
                    "(`.info/serverTimeOffset`): selisih waktu record tertulis di "
                    "server dengan waktu tiba di browser.\n\n")
            for node, d in n.items():
                if "fb_to_fe" not in d:
                    continue
                f.write(f"### {node}\n\n")
                f.write("| Metrik | N | Rata-rata | Min | Maks | StDev | Jitter |\n")
                f.write("|---|---|---|---|---|---|---|\n")
                f.write(baris_stat("Firebase->Frontend (ms)", d["fb_to_fe"], d["jitter_fb_to_fe"]))
                f.write(baris_stat("End-to-End poll->browser (ms)", d["e2e"], d["jitter_e2e"]))
                f.write("\n")

        # ------------------------------------------------------------------
        f.write("## 6. Rekapitulasi QoS Keseluruhan & Standar TIPHON\n\n")
        f.write("Kategori mengikuti ETSI TIPHON TR 101 329 — Delay: <150 ms Sangat "
                "Bagus (4), 150–300 Bagus (3), 300–450 Sedang (2), >450 Jelek (1). "
                "Jitter: 0 Sangat Bagus, <75 Bagus, 75–125 Sedang, 125–225 Jelek. "
                "Packet loss: 0% Sangat Bagus, <3% Bagus, <15% Sedang, <25% Jelek.\n\n")
        f.write("| Node | Segmen | Delay (ms) | Kategori | Jitter (ms) | Kategori | Loss (%) | Kategori |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for node, d in n.items():
            rtt_mean = d["poll_rtt"]["mean"] if d["poll_rtt"] else None
            kd, _ = kategori_delay(rtt_mean)
            kj, _ = kategori_jitter(d["jitter_rtt"])
            kl, _ = kategori_loss(d["loss_pct"])
            f.write(f"| {node} | Node->Gateway | {fmt(rtt_mean)} | {kd} | "
                    f"{fmt(d['jitter_rtt'])} | {kj} | {fmt(d['loss_pct'])} | {kl} |\n")
            if "fb_to_fe" in d:
                fbfe = d["fb_to_fe"]["mean"] if d["fb_to_fe"] else None
                kd2, _ = kategori_delay(fbfe)
                kj2, _ = kategori_jitter(d["jitter_fb_to_fe"])
                f.write(f"| {node} | Firebase->Frontend | {fmt(fbfe)} | {kd2} | "
                        f"{fmt(d['jitter_fb_to_fe'])} | {kj2} | - | - |\n")
            if "e2e" in d and d["e2e"]:
                kd3, _ = kategori_delay(d["e2e"]["mean"])
                kj3, _ = kategori_jitter(d["jitter_e2e"])
                f.write(f"| {node} | **End-to-End** | **{fmt(d['e2e']['mean'])}** | {kd3} | "
                        f"{fmt(d['jitter_e2e'])} | {kj3} | {fmt(d['loss_pct'])} | {kl} |\n")
        f.write("\n> Catatan interpretasi: TIPHON dirancang untuk layanan "
                "real-time kelas VoIP. Pada segmen Node->Gateway, delay "
                "didominasi *airtime* LoRa + waktu scan nRF24 (~600–800 ms) yang "
                "merupakan konsekuensi desain sensing, bukan degradasi jaringan — "
                "laporkan kategori TIPHON apa adanya lalu jelaskan konteks ini.\n\n")

        # ------------------------------------------------------------------
        f.write("## 7. Rumus yang Digunakan\n\n")
        f.write("- **Delay rata-rata**: $\\bar{D} = \\frac{1}{N}\\sum_{i=1}^{N} D_i$\n")
        f.write("- **Jitter (variasi delay)**: $J = \\frac{1}{N-1}\\sum_{i=2}^{N} |D_i - D_{i-1}|$\n")
        f.write("- **Packet loss**: $PL = \\frac{paket\\ hilang}{paket\\ dikirim} \\times 100\\%$ "
                "(timeout poll, dan celah nomor urut `seq`)\n")
        f.write("- **Throughput**: $T = \\frac{\\sum ukuran\\ paket \\times 8}{durasi\\ pengamatan}$ (bps)\n")
        f.write("- **Latensi lintas perangkat** (Backend->Firebase->Frontend): semua "
                "stempel waktu dikonversi ke jam server Firebase; browser memakai "
                "`.info/serverTimeOffset`, backend memakai estimasi offset metode "
                "NTP (tulis-baca sentinel timestamp server).\n")

    return path


def tulis_ringkasan_csv(hasil, outdir):
    path = os.path.join(outdir, "ringkasan_qos.csv")
    cols = ["node", "rx", "timeout", "loss_pct", "seq_gap", "loss_seq_pct",
            "rssi_mean", "snr_mean", "poll_rtt_mean_ms", "poll_rtt_stdev_ms",
            "jitter_rtt_ms", "link_delay_mean_ms", "node_proc_mean_ms",
            "decision_mean_ms", "backend_total_mean_ms", "fb_raw_mean_ms",
            "fb_pred_mean_ms", "backend_to_fb_mean_ms", "fb_to_fe_mean_ms",
            "jitter_fb_to_fe_ms", "e2e_mean_ms", "jitter_e2e_ms",
            "throughput_bps"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for node, d in hasil["nodes"].items():
            def m(key):
                st = d.get(key)
                return round(st["mean"], 2) if st else ""
            w.writerow([
                node, d["rx"], d["timeout"], round(d["loss_pct"], 2),
                d["seq_gap"], round(d["loss_seq_pct"], 2),
                m("rssi"), m("snr"), m("poll_rtt"),
                round(d["poll_rtt"]["stdev"], 2) if d["poll_rtt"] else "",
                round(d["jitter_rtt"], 2) if d["jitter_rtt"] is not None else "",
                m("link_delay"), m("node_proc"), m("decision_ms"),
                m("backend_total"), m("fb_raw"), m("fb_pred"),
                m("backend_to_fb"), m("fb_to_fe"),
                round(d.get("jitter_fb_to_fe") or 0, 2) if d.get("jitter_fb_to_fe") is not None else "",
                m("e2e"),
                round(d.get("jitter_e2e") or 0, 2) if d.get("jitter_e2e") is not None else "",
                round(d["throughput_bps"], 1) if d.get("throughput_bps") else "",
            ])
    return path


# ============================================================================
# GRAFIK (opsional, butuh matplotlib)
# ============================================================================
def buat_grafik(hasil, data_rows, fe_rows, outdir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[i] matplotlib tidak terpasang — grafik dilewati. "
              "Pasang dengan: pip install matplotlib")
        return []

    files = []
    n = hasil["nodes"]

    def simpan(fig, nama):
        p = os.path.join(outdir, nama)
        fig.savefig(p, dpi=150, bbox_inches="tight")
        plt.close(fig)
        files.append(p)

    def gaya(ax):
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#e5e7eb", linewidth=0.6)
        ax.set_axisbelow(True)

    # --- 1. Bar: rata-rata delay per segmen (gabungan semua node) ---
    segmen = []
    def rerata_semua(key):
        vals = [d[key]["mean"] for d in n.values() if d.get(key)]
        return statistics.mean(vals) if vals else None
    for nama, key in [("Node→GW\n(RTT poll)", "poll_rtt"),
                      ("Link LoRa\nmurni", "link_delay"),
                      ("Backend\ntotal/frame", "backend_total"),
                      ("Backend→FB\n(PATCH)", "fb_raw"),
                      ("FB→Frontend", "fb_to_fe"),
                      ("End-to-End", "e2e")]:
        v = rerata_semua(key)
        if v is not None:
            segmen.append((nama, v))
    if segmen:
        fig, ax = plt.subplots(figsize=(8, 4))
        nama = [s[0] for s in segmen]
        val = [s[1] for s in segmen]
        bars = ax.bar(nama, val, color=WARNA_UTAMA, width=0.55)
        for b, v in zip(bars, val):
            ax.annotate(f"{v:.0f}", (b.get_x() + b.get_width() / 2, v),
                        ha="center", va="bottom", fontsize=9, color="#374151")
        ax.set_ylabel("Delay rata-rata (ms)")
        ax.set_title("Delay Rata-rata per Segmen Pipeline LCDD")
        gaya(ax)
        simpan(fig, "grafik_delay_per_segmen.png")

    # --- 2. Line: RTT poll per sampel, per node ---
    fig, ax = plt.subplots(figsize=(9, 4))
    ada = False
    per_node_rows = {}
    for r in data_rows:
        per_node_rows.setdefault(r.get("node", "?"), []).append(r)
    for node in sorted(per_node_rows):
        rtt = [to_float(r.get("poll_rtt_ms")) for r in per_node_rows[node]]
        rtt = [x for x in rtt if x is not None]
        if rtt:
            ada = True
            ax.plot(range(1, len(rtt) + 1), rtt, linewidth=1.4,
                    color=WARNA_NODE.get(node, "#64748B"), label=node)
    if ada:
        ax.set_xlabel("Sampel ke-")
        ax.set_ylabel("RTT poll→balasan (ms)")
        ax.set_title("Delay Node→Gateway per Sampel")
        ax.legend(frameon=False)
        gaya(ax)
        simpan(fig, "grafik_delay_timeseries.png")
    else:
        plt.close(fig)

    # --- 3. Bar: packet loss per node ---
    fig, ax = plt.subplots(figsize=(6, 4))
    nodes = list(n.keys())
    loss = [n[x]["loss_pct"] for x in nodes]
    bars = ax.bar(nodes, loss, color=WARNA_UTAMA, width=0.5)
    for b, v in zip(bars, loss):
        ax.annotate(f"{v:.1f}%", (b.get_x() + b.get_width() / 2, v),
                    ha="center", va="bottom", fontsize=9, color="#374151")
    ax.set_ylabel("Packet loss (%)")
    ax.set_title("Packet Loss Segmen Node→Gateway")
    gaya(ax)
    simpan(fig, "grafik_loss_per_node.png")

    # --- 4. CDF end-to-end (bila ada data frontend) ---
    fe_per_node = {}
    for r in fe_rows:
        fe_per_node.setdefault(r.get("node", "?"), []).append(r)
    fig, ax = plt.subplots(figsize=(7, 4))
    ada = False
    for node in sorted(fe_per_node):
        e2e = [to_float(r.get("e2e_ms")) for r in fe_per_node[node]]
        e2e = sorted(x for x in e2e if x is not None)
        if e2e:
            ada = True
            y = [i / len(e2e) for i in range(1, len(e2e) + 1)]
            ax.plot(e2e, y, linewidth=1.6,
                    color=WARNA_NODE.get(node, "#64748B"), label=node)
    if ada:
        ax.set_xlabel("Delay end-to-end (ms)")
        ax.set_ylabel("Proporsi kumulatif")
        ax.set_title("CDF Delay End-to-End (poll gateway → browser)")
        ax.legend(frameon=False)
        gaya(ax)
        simpan(fig, "grafik_e2e_cdf.png")
    else:
        plt.close(fig)

    return files


# ============================================================================
# MAIN
# ============================================================================
def main():
    p = argparse.ArgumentParser(description="Analisis QoS end-to-end LCDD")
    p.add_argument("--backend", required=True, help="qos_log.csv dari backend/app.py")
    p.add_argument("--frontend", default=None,
                   help="qos_frontend_*.csv hasil export tab QoS website (opsional)")
    p.add_argument("--label", default="pengujian", help="nama skenario pengujian")
    args = p.parse_args()

    hasil, data_rows, timeout_rows, fe_rows = analisis(
        args.backend, args.frontend, args.label)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = f"hasil_qos_{args.label}_{stamp}"
    os.makedirs(outdir, exist_ok=True)

    md = tulis_laporan(hasil, outdir, args.backend, args.frontend)
    ringkas = tulis_ringkasan_csv(hasil, outdir)
    grafik = buat_grafik(hasil, data_rows, fe_rows, outdir)

    print("\n[SUKSES] Analisis selesai!")
    print(f"--> Laporan  : {os.path.abspath(md)}")
    print(f"--> Ringkasan: {os.path.abspath(ringkas)}")
    for g in grafik:
        print(f"--> Grafik   : {os.path.abspath(g)}")


if __name__ == "__main__":
    main()
