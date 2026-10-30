#!/usr/bin/env python3
import argparse
import csv
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "sdn"))

from mininet.log import setLogLevel  # noqa: E402
from topology import build_network  # noqa: E402

TESTS = {
    "A": (100, 1024),
    "B": (500, 1024),
    "C": (1000, 1024),
    "D": (2000, 1024),
    "E": (500, 512),
}
H2_MAC = "00:00:00:00:00:02"
UDP_PORT = 5001
SENDER = os.path.join(REPO, "sender", "sender.py")
RECEIVER = os.path.join(REPO, "receiver", "receiver.py")

FIELDS = ["run", "mode", "test", "rep", "target_pps", "payload_bytes", "duration_s",
          "sent", "sent_bytes", "sender_elapsed_s",
          "received", "app_rx_bytes", "rx_duration_s",
          "sdn_pkts_start", "sdn_pkts_end", "sdn_bytes_start", "sdn_bytes_end",
          "sdn_interval_s", "sdn_window_mbps", "sdn_windows", "t_start", "t_end"]


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_flow_rows(path):
    try:
        with open(path, newline="") as f:
            return [r for r in csv.DictReader(f) if r["eth_dst"] == H2_MAC]
    except OSError:
        return []


def sdn_counters(rows, t_start, t_end, interval):
    before = [r for r in rows if float(r["timestamp"]) <= t_start]
    after = [r for r in rows if float(r["timestamp"]) >= t_end]
    if not before or not after:
        return None
    b, a = before[-1], after[0]
    windows = [float(r["rate_mbps"]) for r in rows
               if float(r["interval_s"]) > 0
               and float(r["timestamp"]) - interval >= t_start
               and float(r["timestamp"]) <= t_end]
    return {
        "sdn_pkts_start": int(b["packet_count"]),
        "sdn_pkts_end": int(a["packet_count"]),
        "sdn_bytes_start": int(b["byte_count"]),
        "sdn_bytes_end": int(a["byte_count"]),
        "sdn_window_mbps": round(sum(windows) / len(windows), 4) if windows else "",
        "sdn_windows": len(windows),
    }


def run_test(h1, h2, tag, pps, size, duration, raw_dir):
    tx_path = os.path.join(raw_dir, f"{tag}_tx.json")
    rx_path = os.path.join(raw_dir, f"{tag}_rx.json")
    for p in (tx_path, rx_path):
        if os.path.exists(p):
            os.remove(p)

    rx = h2.popen(["python3", RECEIVER, "--port", str(UDP_PORT), "--out", rx_path],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    h1.cmd(f"python3 {SENDER} --dest 10.0.0.2 --port {UDP_PORT} --duration {duration} "
           f"--pps {pps} --size {size} --out {tx_path}")
    try:
        rx.wait(timeout=duration + 30)
    except subprocess.TimeoutExpired:
        rx.kill()
    return load_json(tx_path), load_json(rx_path)


def next_run_id(path):
    if not os.path.exists(path):
        return 1
    with open(path, newline="") as f:
        return sum(1 for _ in csv.DictReader(f)) + 1


def main():
    parser = argparse.ArgumentParser(description="Automated UDP/SDN experiments")
    parser.add_argument("--mode", required=True, choices=["baseline", "dynamic"])
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--tests", nargs="+", default=list(TESTS), choices=list(TESTS))
    parser.add_argument("--interval", type=float, default=5,
                        help="controller MONITOR_INTERVAL in seconds")
    parser.add_argument("--flow-csv", default=os.path.join(REPO, "results", "flow_stats.csv"))
    parser.add_argument("--out", default=os.path.join(REPO, "results", "measurements.csv"))
    args = parser.parse_args()

    raw_dir = os.path.join(REPO, "results", "raw")
    os.makedirs(raw_dir, exist_ok=True)
    settle = args.interval + 2

    setLogLevel("warning")
    net = build_network(10, "5ms", "127.0.0.1", 6653)
    net.start()
    try:
        time.sleep(3)
        if net.pingAll() > 0:
            print("WARNING: pingall reported loss; check the controller")
        h1, h2 = net.get("h1", "h2")
        time.sleep(settle)

        run_id = next_run_id(args.out)
        is_new = not os.path.exists(args.out)
        with open(args.out, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if is_new:
                writer.writeheader()

            for rep in range(1, args.reps + 1):
                for test in args.tests:
                    pps, size = TESTS[test]
                    tag = f"{args.mode}_{test}_r{rep}"
                    print(f"[{tag}] {pps} pps x {size} B for {args.duration:g} s", flush=True)

                    tx, rx = run_test(h1, h2, tag, pps, size, args.duration, raw_dir)
                    time.sleep(settle)

                    if tx is None:
                        print("  sender output missing, skipping")
                        continue

                    t_start = tx["start_time"]
                    t_end = tx["end_time"]
                    if rx and rx.get("last_rx"):
                        t_end = max(t_end, rx["last_rx"])

                    sdn = sdn_counters(read_flow_rows(args.flow_csv), t_start, t_end, args.interval)
                    if sdn is None:
                        print("  no bracketing flow-stats samples; is the controller running?")
                        sdn = {k: "" for k in ("sdn_pkts_start", "sdn_pkts_end", "sdn_bytes_start",
                                               "sdn_bytes_end", "sdn_window_mbps", "sdn_windows")}

                    rx_dur = ""
                    if rx and rx.get("first_rx") and rx.get("last_rx"):
                        rx_dur = round(rx["last_rx"] - rx["first_rx"], 6)

                    row = {
                        "run": run_id, "mode": args.mode, "test": test, "rep": rep,
                        "target_pps": pps, "payload_bytes": size, "duration_s": args.duration,
                        "sent": tx["sent_packets"], "sent_bytes": tx["sent_bytes"],
                        "sender_elapsed_s": tx["elapsed_s"],
                        "received": rx["unique_received"] if rx else 0,
                        "app_rx_bytes": rx["bytes_received"] if rx else 0,
                        "rx_duration_s": rx_dur,
                        "sdn_interval_s": round(t_end - t_start, 6),
                        "t_start": f"{t_start:.6f}", "t_end": f"{t_end:.6f}",
                        **sdn,
                    }
                    writer.writerow(row)
                    f.flush()
                    run_id += 1

                    loss = 100.0 * (row["sent"] - row["received"]) / row["sent"] if row["sent"] else 0.0
                    sdn_pkts = (sdn["sdn_pkts_end"] - sdn["sdn_pkts_start"]
                                if sdn["sdn_pkts_start"] != "" else "n/a")
                    print(f"  sent={row['sent']} recv={row['received']} loss={loss:.2f}% "
                          f"switch_pkts={sdn_pkts}", flush=True)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
