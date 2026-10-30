import argparse
import json
import socket
import time

parser = argparse.ArgumentParser(
    description="UDP Bandwidth Meter Sender"
)

parser.add_argument("--dest", default="10.0.0.2")
parser.add_argument("--port", type=int, default=5001)
parser.add_argument("--duration", type=float, default=10)
parser.add_argument("--pps", type=int, default=500)
parser.add_argument("--size", type=int, default=1024)
parser.add_argument("--out", help="write a JSON summary to this file")

args = parser.parse_args()

if args.pps <= 0 or args.duration <= 0:
    parser.error("Packet rate and duration must be positive")

if not 10 <= args.size <= 65000:
    parser.error("Packet size must be between 10 and 65000 bytes")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

sent_packets = 0
sent_bytes = 0

interval = 1.0 / args.pps
start_wall = time.time()
start = time.monotonic()
deadline = start + args.duration
next_send = start

print("UDP Bandwidth Meter - Sender")
print(f"Destination: {args.dest}:{args.port}")
print(f"Target rate: {args.pps} packets/second")
print(f"Packet size: {args.size} bytes")

try:
    while time.monotonic() < deadline:
        sequence = sent_packets
        header = f"{sequence:010d}".encode()
        payload = header + b"x" * (args.size - len(header))

        try:
            count = sock.sendto(
                payload, (args.dest, args.port)
            )
            sent_packets += 1
            sent_bytes += count

        except OSError as error:
            print("Socket error:", error)
            break

        next_send += interval
        delay = next_send - time.monotonic()

        if delay > 0:
            time.sleep(delay)
        else:
            next_send = time.monotonic()

finally:
    sock.close()

elapsed = time.monotonic() - start
end_wall = time.time()

throughput = (
    sent_bytes * 8 / elapsed / 1_000_000
    if elapsed > 0 else 0
)

print("\n--- Sender Statistics ---")
print(f"Elapsed time: {elapsed:.2f} seconds")
print(f"Packets sent: {sent_packets}")
print(f"Bytes sent: {sent_bytes}")
print(f"Average packet rate: {sent_packets / elapsed:.2f} packets/sec")
print(f"Application throughput: {throughput:.3f} Mbps")

if args.out:
    summary = {
        "role": "sender",
        "dest": args.dest,
        "port": args.port,
        "target_pps": args.pps,
        "payload_bytes": args.size,
        "duration_s": args.duration,
        "sent_packets": sent_packets,
        "sent_bytes": sent_bytes,
        "elapsed_s": round(elapsed, 6),
        "avg_pps": round(sent_packets / elapsed, 3) if elapsed > 0 else 0.0,
        "throughput_mbps": round(throughput, 6),
        "start_time": start_wall,
        "end_time": end_wall,
    }
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
