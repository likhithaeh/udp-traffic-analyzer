#!/usr/bin/env python3
import argparse

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController


def build_network(bw, delay, ctrl_ip, ctrl_port):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip=ctrl_ip, port=ctrl_port)

    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    s1 = net.addSwitch("s1", protocols="OpenFlow13")

    net.addLink(h1, s1, bw=bw, delay=delay)
    net.addLink(h2, s1, bw=bw, delay=delay)
    return net


def main():
    parser = argparse.ArgumentParser(description="Bandwidth meter SDN topology")
    parser.add_argument("--bw", type=float, default=10, help="link bandwidth in Mbps")
    parser.add_argument("--delay", default="5ms", help="per-link delay")
    parser.add_argument("--ctrl-ip", default="127.0.0.1")
    parser.add_argument("--ctrl-port", type=int, default=6653)
    args = parser.parse_args()

    setLogLevel("info")
    net = build_network(args.bw, args.delay, args.ctrl_ip, args.ctrl_port)
    net.start()
    CLI(net)
    net.stop()


if __name__ == "__main__":
    main()
