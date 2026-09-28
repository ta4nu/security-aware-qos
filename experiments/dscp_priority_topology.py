from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, Controller
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
import time
import json
import re
import subprocess
from subprocess import PIPE

TEST_SECONDS = 10   # length of each iperf3 flow


def class_bytes(node, iface):
    """Return {'1:10': bytes, '1:20': bytes, ...} sent per HTB class, read from tc."""
    out = node.cmd(f'tc -s class show dev {iface}')
    counts, current = {}, None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r'class htb (\d+:\d+)', line)
        if m:
            current = m.group(1)
        m = re.match(r'Sent (\d+) bytes', line)
        if m and current:
            counts[current] = int(m.group(1))
    return counts


def summarize_iperf(label, out, err):
    """Print sender throughput from iperf3's per-second intervals.

    The per-second intervals are used (not the 'end' section) because the
    end-of-test summary is unreliable on this shaped Mininet link.
    """
    try:
        data = json.loads(out)
    except Exception:
        print(f'{label}: no JSON output from iperf3. stderr: {err.strip()[:200]}')
        return
    if data.get('error'):
        print(f'{label}: iperf3 reported: {data["error"]}')
    intervals = data.get('intervals', [])
    if not intervals:
        print(f'{label}: no data intervals recorded')
        return
    total_bytes = sum(i['sum']['bytes'] for i in intervals)
    secs = sum(i['sum']['seconds'] for i in intervals)
    retr = sum(i['sum'].get('retransmits', 0) for i in intervals)
    print(f'{label}: sent {total_bytes / 1e6:.2f} MB in {secs:.1f}s = '
          f'{total_bytes * 8 / secs / 1e6:.2f} Mbps, retransmits = {retr}')


def apply_htb_prio(node, iface, total_rate_mbit=10):
    """
    Configures HTB + PRIO queuing on the given interface of `node`.

    IMPORTANT: tc qdiscs control EGRESS (outgoing) traffic on an interface.
    To prioritize traffic arriving at h3, this must be applied on the
    interface that carries traffic OUT toward h3 - that is the switch's
    port facing h3, NOT h3's own interface (h3 barely sends anything out,
    just ACKs, so queuing there has no effect on the real data flows).

    Creates:
      - A root HTB qdisc capping total bandwidth at total_rate_mbit.
      - Two child classes: high-priority (EF, DSCP 46) gets guaranteed
        bandwidth and goes first; low-priority (BE, DSCP 0) gets the
        remainder.
      - A tc filter that matches on DSCP value (via the IP ToS/DSCP field)
        and sends packets into the correct class.
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
        # First command (qdisc del) is expected to error on a fresh interface -
        # that specific failure is fine. All others print their output so
        # real errors are visible instead of being silently swallowed.
        if "qdisc del" in c:
            node.cmd(c + " 2>/dev/null")
        else:
            out = node.cmd(c)
            if out.strip():
                print(f"[tc] {c}\n    -> {out.strip()}")


def run_dscp_experiment():
    net = Mininet(switch=OVSKernelSwitch, link=TCLink, autoSetMacs=True)

    info('*** Adding controller\n')
    net.addController('c0', controller=Controller)

    info('*** Adding hosts\n')
    h1 = net.addHost('h1', ip='10.0.0.1/24')  # high-priority sender (EF)
    h2 = net.addHost('h2', ip='10.0.0.2/24')  # low-priority sender (BE)
    h3 = net.addHost('h3', ip='10.0.0.3/24')  # receiver / edge gateway

    info('*** Adding switch\n')
    s1 = net.addSwitch('s1')

    info('*** Creating links (shared 10 Mbps bottleneck at the receiver side)\n')
    net.addLink(h1, s1, bw=10, delay='10ms')
    net.addLink(h2, s1, bw=10, delay='10ms')
    # This is the shared bottleneck both flows must pass through
    net.addLink(h3, s1, bw=10, delay='10ms')

    info('*** Starting network\n')
    net.start()
    net.pingAll()

    # Find the switch-side interface of the h3<->s1 link. This is the port
    # traffic from h1 and h2 actually flows OUT of on its way to h3, so this
    # (not h3's own interface) is where egress queuing has real effect.
    link_h3 = h3.connectionsTo(s1)[0]
    switch_iface = link_h3[1].name
    info(f'*** Applying HTB+PRIO QoS on switch interface {switch_iface} (facing h3) ***\n')
    apply_htb_prio(s1, switch_iface, total_rate_mbit=10)

    # Clear any leftover iperf3 processes from earlier runs
    h3.cmd('pkill iperf3')
    time.sleep(1)

    # A single iperf3 server handles only ONE test at a time. h1 and h2 send
    # at the same moment, so each flow gets its own server on its own port.
    info('*** Starting two iperf3 servers on h3 (ports 5201 and 5202)\n')
    h3.cmd('iperf3 -s -p 5201 -D')   # for h1 (high-priority, EF)
    h3.cmd('iperf3 -s -p 5202 -D')   # for h2 (low-priority, BE)
    time.sleep(1)

    bytes_before = class_bytes(s1, switch_iface)

    # NOTE ON TAGGING: iperf3 3.9's --dscp option did NOT produce DSCP-46
    # packets in our VM (iptables counters at h3 showed 0 matches). Setting
    # the TOS byte directly with -S did: DSCP 46 (EF) << 2 = 184 = 0xB8, and
    # 990 of 1005 packets to the server were seen with DSCP 46.
    info('\n*** HIGH-PRIORITY flow: h1 -> h3, EF (TOS 184 = DSCP 46), 5 Mbps offered ***\n')
    p1 = h1.popen(['iperf3', '-c', '10.0.0.3', '-p', '5201', '-t', str(TEST_SECONDS),
                   '-b', '5M', '-S', '184', '-J'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)

    info('*** LOW-PRIORITY flow:  h2 -> h3, BE (DSCP 0), 8 Mbps offered, same time ***\n')
    p2 = h2.popen(['iperf3', '-c', '10.0.0.3', '-p', '5202', '-t', str(TEST_SECONDS),
                   '-b', '8M', '-J'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)

    # Wait for the clients to actually finish (instead of a fixed sleep)
    info('*** Waiting for both flows to finish ***\n')
    outputs = []
    for label, proc in (('HIGH-PRIORITY (EF, h1)', p1), ('LOW-PRIORITY  (BE, h2)', p2)):
        try:
            out, err = proc.communicate(timeout=TEST_SECONDS + 40)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        outputs.append((label, out, err))
        with open('/tmp/' + ('h1_ef' if 'EF' in label else 'h2_be') + '_result.json', 'w') as f:
            f.write(out)

    bytes_after = class_bytes(s1, switch_iface)

    info('\n*** tc class counters on the switch interface ***\n')
    print(s1.cmd(f'tc -s class show dev {switch_iface}'))

    info('\n*** RESULTS SUMMARY ***\n')
    for label, out, err in outputs:
        summarize_iperf(label, out, err)

    ef = bytes_after.get('1:10', 0) - bytes_before.get('1:10', 0)
    be = bytes_after.get('1:20', 0) - bytes_before.get('1:20', 0)
    print('\nBytes through the switch queue during the test (measured by tc):')
    print(f'  EF class 1:10: {ef} bytes  (about {ef * 8 / TEST_SECONDS / 1e6:.2f} Mbps)')
    print(f'  BE class 1:20: {be} bytes  (about {be * 8 / TEST_SECONDS / 1e6:.2f} Mbps)')
    if ef == 0:
        print('  -> EF class saw NO traffic: the DSCP filter did not match the h1 flow.')
    else:
        print('  -> EF class is receiving the h1 flow: DSCP classification works.')

    info('\n*** Raw JSON saved in /tmp/h1_ef_result.json and /tmp/h2_be_result.json ***\n')
    info('*** Dropping into CLI - type "exit" to stop the network ***\n')
    CLI(net)

    h3.cmd('pkill iperf3')
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run_dscp_experiment()
