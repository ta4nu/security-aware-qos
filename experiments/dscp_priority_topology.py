from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, Controller
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
import time


def apply_htb_prio(host, iface, total_rate_mbit=10):
    """
    Configures HTB + PRIO queuing on the given interface of `host`.

    Creates:
      - A root HTB qdisc capping total bandwidth at total_rate_mbit.
      - Two child classes: high-priority (EF, DSCP 46) gets guaranteed
        bandwidth and goes first; low-priority (BE, DSCP 0) gets the
        remainder.
      - tc filters that match on DSCP value (via the IP ToS/DSCP field)
        and send packets into the correct class.
    """
    cmds = [
        f"tc qdisc del dev {iface} root",  
        f"tc qdisc add dev {iface} root handle 1: htb default 20",
        f"tc class add dev {iface} parent 1: classid 1:1 htb rate {total_rate_mbit}mbit",
        f"tc class add dev {iface} parent 1:1 classid 1:10 htb rate {int(total_rate_mbit*0.7)}mbit ceil {total_rate_mbit}mbit prio 0",
        f"tc class add dev {iface} parent 1:1 classid 1:20 htb rate {int(total_rate_mbit*0.3)}mbit ceil {total_rate_mbit}mbit prio 1",
        f"tc qdisc add dev {iface} parent 1:10 handle 10: pfifo limit 50",
        f"tc qdisc add dev {iface} parent 1:20 handle 20: pfifo limit 50",
        f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 "
        f"match ip tos 0xb8 0xfc flowid 1:10",
    ]
    for c in cmds:
        host.cmd(c + " 2>/dev/null")


def run_dscp_experiment():
    net = Mininet(switch=OVSKernelSwitch, link=TCLink, autoSetMacs=True)

    info('*** Adding controller\n')
    net.addController('c0', controller=Controller)

    info('*** Adding hosts\n')
    h1 = net.addHost('h1', ip='10.0.0.1/24')  
    h2 = net.addHost('h2', ip='10.0.0.2/24') 
    h3 = net.addHost('h3', ip='10.0.0.3/24') 

    info('*** Adding switch\n')
    s1 = net.addSwitch('s1')

    info('*** Creating links (shared 10 Mbps bottleneck at the receiver side)\n')
    net.addLink(h1, s1, bw=10, delay='10ms')
    net.addLink(h2, s1, bw=10, delay='10ms')
  
    net.addLink(h3, s1, bw=10, delay='10ms')

    info('*** Starting network\n')
    net.start()
    net.pingAll()

    h3_iface = h3.defaultIntf().name
    info(f'*** Applying HTB+PRIO QoS on {h3_iface}\n')
    apply_htb_prio(h3, h3_iface, total_rate_mbit=10)

    info('*** Starting iperf3 server on h3\n')
    h3.cmd('iperf3 -s -p 5201 -D')
    time.sleep(1)

    info('\n*** Sending HIGH-PRIORITY (EF, DSCP 46) traffic from h1 ***\n')
    h1.cmd('iperf3 -c 10.0.0.3 -p 5201 -t 10 -b 5M --dscp 46 -J > /tmp/h1_ef_result.json &')

    info('*** Sending LOW-PRIORITY (BE, DSCP 0) traffic from h2 (competing at the same time) ***\n')
    h2.cmd('iperf3 -c 10.0.0.3 -p 5201 -t 10 -b 8M -J > /tmp/h2_be_result.json &')

    info('*** Waiting for both flows to finish (12s) ***\n')
    time.sleep(12)

    info('\n*** Results saved to /tmp/h1_ef_result.json (high-priority) '
         'and /tmp/h2_be_result.json (low-priority) ***\n')
    info('*** Run: cat /tmp/h1_ef_result.json | python3 -m json.tool  to inspect ***\n')

    info('*** Dropping into CLI - type "exit" to stop the network ***\n')
    CLI(net)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run_dscp_experiment()
