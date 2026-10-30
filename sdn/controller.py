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
MONITOR_INTERVAL = 5
CSV_PATH = os.environ.get("FLOW_CSV", "results/flow_stats.csv")

CSV_HEADER = ["timestamp", "dpid", "eth_dst", "packet_count", "byte_count",
              "flow_duration_s", "interval_s", "delta_bytes", "rate_mbps"]


class BandwidthController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.udp_flows = {}
        self.datapaths = {}
        self.prev = {}
        self._open_csv()
        self.monitor_thread = hub.spawn(self._monitor)

    def _open_csv(self):
        os.makedirs(os.path.dirname(CSV_PATH) or ".", exist_ok=True)
        is_new = not os.path.exists(CSV_PATH)
        self.csv_file = open(CSV_PATH, "a", newline="")
        self.writer = csv.writer(self.csv_file)
        if is_new:
            self.writer.writerow(CSV_HEADER)
            self.csv_file.flush()

    def add_flow(self, dp, priority, match, actions, cookie=0):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, cookie=cookie))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        dp.send_msg(parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                      table_id=ofp.OFPTT_ALL, out_port=ofp.OFPP_ANY,
                                      out_group=ofp.OFPG_ANY, match=parser.OFPMatch()))
        self.mac_to_port[dp.id] = {}
        self.udp_flows[dp.id] = set()

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
            installed = self.udp_flows.setdefault(dp.id, set())
            if eth.dst not in installed:
                udp_match = parser.OFPMatch(eth_dst=eth.dst, eth_type=ether_types.ETH_TYPE_IP,
                                            ip_proto=17, udp_dst=UDP_PORT)
                self.add_flow(dp, 10, udp_match, actions, cookie=UDP_COOKIE)
                installed.add(eth.dst)
                self.logger.info("UDP:%d monitor flow installed -> %s via port %d",
                                 UDP_PORT, eth.dst, out_port)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))

    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)
            hub.sleep(MONITOR_INTERVAL)

    def _request_stats(self, dp):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath=dp, table_id=ofp.OFPTT_ALL,
                                         out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                         cookie=UDP_COOKIE, cookie_mask=0xFFFFFFFFFFFFFFFF,
                                         match=parser.OFPMatch())
        dp.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        now = time.time()
        dpid = ev.msg.datapath.id

        for stat in ev.msg.body:
            dst = stat.match.get("eth_dst")
            key = (dpid, dst)
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

            self.writer.writerow([f"{now:.6f}", dpid, dst, stat.packet_count, stat.byte_count,
                                  f"{duration:.6f}", f"{interval:.6f}", delta, f"{rate:.4f}"])
            self.logger.info("dst=%s pkts=%d bytes=%d rate=%.3f Mbps",
                             dst, stat.packet_count, stat.byte_count, rate)

        self.csv_file.flush()
