import argparse
import json
import socket
import time

SEQ_LEN = 10  # sender writes a 10-digit ASCII sequence number


def main():
    parser = argparse.ArgumentParser(description="UDP Bandwidth Meter Receiver")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--idle-timeout", type=float, default=3.0,
                        help="stop after this many seconds with no packets")
    parser.add_argument("--out", help="write a JSON summary to this file")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(args.idle_timeout)

    seen = set()
    rx_datagrams = dups = bytes_rx = 0
    max_seq = -1
    first = last = None

    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            break  # no packets for idle-timeout seconds: sender has finished

        try:
            seq = int(data[:SEQ_LEN])
        except ValueError:
            continue  # not one of our packets

        now = time.time()
        rx_datagrams += 1
        if first is None:
            first = now
        last = now

        if seq in seen:
            dups += 1
            continue
        seen.add(seq)
        bytes_rx += len(data)
        max_seq = max(max_seq, seq)

    sock.close()

    dur = (last - first) if first is not None and last is not None else 0.0
    expected = max_seq + 1  # sequence numbers start at 0
    unique = len(seen)
    result = {
        "role": "receiver",
        "datagrams_received": rx_datagrams,
        "unique_received": unique,
        "duplicates": dups,
        "expected": expected,
        "lost": expected - unique,
        "loss_pct": round(100.0 * (expected - unique) / expected, 3) if expected else 0.0,
        "bytes_received": bytes_rx,
        "throughput_bps": round(bytes_rx * 8 / dur, 2) if dur > 0 else 0.0,
        "first_rx": first,
        "last_rx": last,
    }

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
