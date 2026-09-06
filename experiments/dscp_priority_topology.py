from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, Controller
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel,info
import time

def apply_htb_prio(host, iface total_rate_mbit=10):
