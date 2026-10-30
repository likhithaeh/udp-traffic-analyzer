# Live demo script (about 6 minutes)

Copy-paste every command; don't retype. The demo writes its logs to `/tmp/demo`, so the experiment data in `results/` is not touched.

## Before the demo

- Plug in the Mac charger and keep it from sleeping.
- Boot the VM and open two terminals (T1, T2) and Firefox with the GitHub repo.
- In T2:
  ```bash
  cd ~/udp-traffic-analyzer
  git pull
  sudo mn -c
  ```
- Have a screen recording of a full rehearsal ready as a backup (on the Mac: Cmd+Shift+5).

## 1. Start the controller (T1)

```bash
cd ~/udp-traffic-analyzer
source ~/ryu-env/bin/activate
CONTROLLER_MODE=dynamic RESULTS_DIR=/tmp/demo ryu-manager --ofp-tcp-listen-port 6653 sdn/controller.py
```

**Expect:** `Mode=dynamic interval=5.0s threshold=8.00 Mbps ... meter=8000 kbps`

**Say:** "This is our Ryu SDN controller, speaking OpenFlow 1.3. It's in dynamic mode: it monitors the UDP flow and rate-limits it if it goes above 8 Mbps."

## 2. Start the network (T2)

```bash
sudo python3 sdn/topology.py
```

At `mininet>`:
```
pingall
sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

**Expect:** `0% dropped`. T1 shows `Switch connected` and two `UDP:5001 monitor flow installed` lines. The flow table shows a `priority=10,udp,...,tp_dst=5001` rule with `cookie=0x5001`.

**Say:** "Mininet creates two hosts and an Open vSwitch, with 10 Mbps, 5 ms links. The controller learned where the hosts are and installed a dedicated rule for our UDP traffic on port 5001. The controller reads that rule's packet and byte counters every 5 seconds, so ping and ARP traffic never pollute the measurements."

## 3. Normal load: application and SDN agree (T2)

```
h2 python3 receiver/receiver.py --port 5001 --out /tmp/demo_rx_b.json > /dev/null 2>&1 &
h1 python3 sender/sender.py --dest 10.0.0.2 --port 5001 --duration 10 --pps 500 --size 1024
sh sleep 4; cat /tmp/demo_rx_b.json
```

**Expect:** the sender sent about 5000 packets at about 4.1 Mbps. The receiver reports the same count, `lost: 0`, about 4.1 Mbps. T1 shows about **4.26 Mbps** at the switch.

**Say:** "The sender and receiver count the same packets, so there's no loss. The switch reports about 4% more throughput because it counts the 42 bytes of Ethernet, IP and UDP headers that the application never sees. Correcting for that, the two measurements agree to within 0.06%."

## 4. Congestion: dynamic SDN response (T2)

```
h2 python3 receiver/receiver.py --port 5001 --out /tmp/demo_rx_d.json > /dev/null 2>&1 &
h1 python3 sender/sender.py --dest 10.0.0.2 --port 5001 --duration 20 --pps 2000 --size 1024
```

**While it runs, point at T1:** the rate rises to about **10 Mbps**, then **`METER_APPLIED`** appears, and later lines carry **`[METERED]`**.

After the sender finishes:
```
sh sleep 4; cat /tmp/demo_rx_d.json
sh ovs-ofctl -O OpenFlow13 meter-stats s1
```

**Expect:** about 40,000 sent; the receiver gets roughly 21–22k (about 45% loss). `meter-stats` shows packets dropped by meter 1. About 5–10 s later, T1 prints `METER_REMOVED`.

**Say:** "2000 packets per second is about 16 Mbps, more than the 10 Mbps link can carry. The controller saw the flow exceed 8 Mbps in the switch statistics and attached an OpenFlow meter, which drops anything above 8 Mbps. Most of the loss happens on h1's link before the switch; comparing sender, switch and receiver counts tells us where. When the traffic stopped, the controller removed the meter automatically."

## 5. Results (Firefox)

Show the README's Results tables and the 4 graphs. Key points:
- **throughput_vs_rate:** throughput tracks the sending rate until the link saturates at about 9.6 Mbps; dynamic mode levels off near 8 Mbps.
- **packet_loss_vs_rate:** no loss below capacity; about 38% loss at 2000 pps; dynamic mode adds meter drops.
- **application_vs_sdn:** the points sit on y = x apart from the header overhead.
- **baseline_vs_dynamic:** identical below the threshold; dynamic mode caps tests C and D.

"Each point is the average of 3 repetitions, 30 runs in total. The error bars are tiny, so the results are repeatable."

## 6. Clean up

```
exit
```
```bash
sudo mn -c
```

## Things that may go wrong (guideline for us)
| Problem | Fix |
|---|---|
| `Unable to contact the remote controller` | Ryu isn't running. Start T1 again, then `sudo mn -c` and restart T2 (always `mn -c` *before* starting Ryu). |
| Ryu printed `Terminated` | `sudo mn -c` killed it. Restart T1. |
| `pingall` shows loss | Exit Mininet, run `sudo mn -c`, restart T1, then T2. |
| Receiver shows 0 packets | Make sure `pingall` ran first and the sender used `--port 5001`. |
| Anything else | Switch to the backup screen recording and the README results. |
