import csv
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import ether_types, ethernet, packet
from ryu.ofproto import ofproto_v1_3

UDP_PORT = 5001
UDP_COOKIE = 0x5001
UDP_PRIORITY = 10

MODE = os.environ.get("CONTROLLER_MODE", "baseline").lower()
MONITOR_INTERVAL = float(os.environ.get("MONITOR_INTERVAL", 5))
THRESHOLD_MBPS = float(os.environ.get("THRESHOLD_MBPS", 8))
RELEASE_MBPS = float(os.environ.get("RELEASE_MBPS", THRESHOLD_MBPS / 2))
METER_RATE_KBPS = int(os.environ.get("METER_RATE_KBPS", 8000))
METER_BURST_KB = int(os.environ.get("METER_BURST_KB", METER_RATE_KBPS // 10))

RESULTS_DIR = os.environ.get("RESULTS_DIR", "results")
FLOW_CSV = os.path.join(RESULTS_DIR, "flow_stats.csv")
EVENT_CSV = os.path.join(RESULTS_DIR, "controller_events.csv")

FLOW_HEADER = ["timestamp", "dpid", "eth_dst", "packet_count", "byte_count",
               "flow_duration_s", "interval_s", "delta_bytes", "rate_mbps", "metered"]
EVENT_HEADER = ["timestamp", "mode", "dpid", "eth_dst", "event", "rate_mbps",
                "threshold_mbps", "release_mbps", "meter_rate_kbps"]


def open_csv(path, header):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    is_new = not os.path.exists(path)
    f = open(path, "a", newline="")
    writer = csv.writer(f)
    if is_new:
        writer.writerow(header)
        f.flush()
    return f, writer


class BandwidthController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if MODE not in ("baseline", "dynamic"):
            raise ValueError(f"CONTROLLER_MODE must be baseline or dynamic, got {MODE!r}")

        self.mac_to_port = {}
        self.udp_flows = {}
        self.meter_ids = {}
        self.metered = set()
        self.datapaths = {}
        self.prev = {}

        self.flow_file, self.flow_writer = open_csv(FLOW_CSV, FLOW_HEADER)
        self.event_file, self.event_writer = open_csv(EVENT_CSV, EVENT_HEADER)

        self.logger.info("Mode=%s interval=%.1fs threshold=%.2f Mbps release=%.2f Mbps "
                         "meter=%d kbps burst=%d kb", MODE, MONITOR_INTERVAL, THRESHOLD_MBPS,
                         RELEASE_MBPS, METER_RATE_KBPS, METER_BURST_KB)
        self.monitor_thread = hub.spawn(self._monitor)

    @staticmethod
    def _udp_match(parser, dst):
        return parser.OFPMatch(eth_dst=dst, eth_type=ether_types.ETH_TYPE_IP,
                               ip_proto=17, udp_dst=UDP_PORT)

    def add_flow(self, dp, priority, match, actions, cookie=0, meter_id=None, command=None):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = []
        if meter_id is not None:
            inst.append(parser.OFPInstructionMeter(meter_id, ofp.OFPIT_METER))
        inst.append(parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        dp.send_msg(parser.OFPFlowMod(datapath=dp,
                                      command=ofp.OFPFC_ADD if command is None else command,
                                      priority=priority, match=match,
                                      instructions=inst, cookie=cookie))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        dp.send_msg(parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                      table_id=ofp.OFPTT_ALL, out_port=ofp.OFPP_ANY,
                                      out_group=ofp.OFPG_ANY, match=parser.OFPMatch()))
        dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE,
                                       meter_id=ofp.OFPM_ALL))

        self.mac_to_port[dp.id] = {}
        self.udp_flows[dp.id] = {}
        self.meter_ids[dp.id] = {}
        self.metered = {k for k in self.metered if k[0] != dp.id}

        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, parser.OFPMatch(), actions)
        self.logger.info("Switch %016x connected", dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.match["in_port"]

        eth = packet.Packet(msg.data).get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            self.add_flow(dp, 1, parser.OFPMatch(in_port=in_port, eth_dst=eth.dst), actions)
            flows = self.udp_flows.setdefault(dp.id, {})
            if eth.dst not in flows:
                self.add_flow(dp, UDP_PRIORITY, self._udp_match(parser, eth.dst), actions,
                              cookie=UDP_COOKIE)
                flows[eth.dst] = out_port
                self.logger.info("UDP:%d monitor flow installed -> %s via port %d",
                                 UDP_PORT, eth.dst, out_port)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))

    def _set_meter(self, dp, dst, enable, rate):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        actions = [parser.OFPActionOutput(self.udp_flows[dp.id][dst])]

        meter_id = None
        if enable:
            ids = self.meter_ids.setdefault(dp.id, {})
            if dst not in ids:
                ids[dst] = len(ids) + 1
                bands = [parser.OFPMeterBandDrop(rate=METER_RATE_KBPS, burst_size=METER_BURST_KB)]
                dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD,
                                               flags=ofp.OFPMF_KBPS | ofp.OFPMF_BURST | ofp.OFPMF_STATS,
                                               meter_id=ids[dst], bands=bands))
            meter_id = ids[dst]

        self.add_flow(dp, UDP_PRIORITY, self._udp_match(parser, dst), actions,
                      cookie=UDP_COOKIE, meter_id=meter_id, command=ofp.OFPFC_MODIFY_STRICT)

        key = (dp.id, dst)
        event = "meter_applied" if enable else "meter_removed"
        if enable:
            self.metered.add(key)
        else:
            self.metered.discard(key)

        self.event_writer.writerow([f"{time.time():.6f}", MODE, dp.id, dst, event, f"{rate:.4f}",
                                    THRESHOLD_MBPS, RELEASE_MBPS, METER_RATE_KBPS])
        self.event_file.flush()
        self.logger.info("%s: dst=%s rate=%.3f Mbps (meter %d kbps)",
                         event.upper(), dst, rate, METER_RATE_KBPS)

    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)
            hub.sleep(MONITOR_INTERVAL)

    def _request_stats(self, dp):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPFlowStatsRequest(datapath=dp, table_id=ofp.OFPTT_ALL,
                                               out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                               cookie=UDP_COOKIE,
                                               cookie_mask=0xFFFFFFFFFFFFFFFF,
                                               match=parser.OFPMatch()))

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        now = time.time()
        dp = ev.msg.datapath

        for stat in ev.msg.body:
            dst = stat.match.get("eth_dst")
            key = (dp.id, dst)
            duration = stat.duration_sec + stat.duration_nsec / 1e9

            interval, delta, rate = 0.0, 0, 0.0
            prev = self.prev.get(key)
            if prev is not None:
                interval = duration - prev[0]
                delta = stat.byte_count - prev[1]
                if interval > 0 and delta >= 0:
                    rate = delta * 8 / interval / 1e6
                else:
                    interval, delta = 0.0, 0
            self.prev[key] = (duration, stat.byte_count)

            is_metered = key in self.metered
            self.flow_writer.writerow([f"{now:.6f}", dp.id, dst, stat.packet_count,
                                       stat.byte_count, f"{duration:.6f}", f"{interval:.6f}",
                                       delta, f"{rate:.4f}", int(is_metered)])
            self.logger.info("dst=%s pkts=%d bytes=%d rate=%.3f Mbps%s", dst,
                             stat.packet_count, stat.byte_count, rate,
                             " [METERED]" if is_metered else "")

            if MODE == "dynamic" and interval > 0 and dst in self.udp_flows.get(dp.id, {}):
                if not is_metered and rate > THRESHOLD_MBPS:
                    self._set_meter(dp, dst, True, rate)
                elif is_metered and rate < RELEASE_MBPS:
                    self._set_meter(dp, dst, False, rate)

        self.flow_file.flush()
