from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, Controller
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
import time
import re
import os
import subprocess
from subprocess import PIPE

TEST_SECONDS = 24
POLL_SECONDS = 2
R_EXP_EF_MBPS = 2.0
THETA = 0.5
GRACE_SECONDS = 6
CONSECUTIVE_VIOLATIONS_REQUIRED = 2

_SENDER_RE = re.compile(
    r'([\d.]+)\s+(KBytes|MBytes|GBytes)\s+([\d.]+)\s+(Kbits/sec|Mbits/sec|Gbits/sec)'
    r'(?:\s+(\d+))?\s+sender'
)


def summarize_iperf_text(out):
    """Return (transfer_MB, rate_Mbps, retransmits) parsed from iperf3 plain text, or None."""
    match = None
    for line in out.splitlines():
        m = _SENDER_RE.search(line)
        if m:
            match = m
    if not match:
        return None
    transfer, tunit, rate, runit, retr = match.groups()
    transfer, rate = float(transfer), float(rate)
    if tunit == 'KBytes':
        transfer /= 1024
    elif tunit == 'GBytes':
        transfer *= 1024
    if runit == 'Kbits/sec':
        rate /= 1000
    elif runit == 'Gbits/sec':
        rate *= 1000
    return transfer, rate, (int(retr) if retr else 0)


def apply_htb_prio(node, iface, total_rate_mbit=10):
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
        if "qdisc del" in c:
            node.cmd(c + " 2>/dev/null")
        else:
            node.cmd(c)


def demote_source(switch_node, iface, src_ip):
    switch_node.cmd(
        f"tc filter add dev {iface} protocol ip parent 1:0 prio 0 u32 "
        f"match ip src {src_ip} flowid 1:20"
    )


def read_sender_egress_bytes(ef_sources):
    counts = {}
    for ip, (label, node, port) in ef_sources.items():
        out = node.cmd('iptables -t mangle -L OUTPUT -v -n -x')
        for line in out.splitlines():
            if f'dpt:{port}' in line:
                nums = re.findall(r'\d+', line)
                if len(nums) >= 2:
                    counts[ip] = int(nums[1])
    return counts


def run_scenario(qos_mode):
    """qos_mode: 'none', 'static', or 'trust'. Returns a results dict."""
    info(f'\n{"="*70}\n*** SCENARIO: {qos_mode.upper()} ***\n{"="*70}\n')

    net = Mininet(switch=OVSKernelSwitch, link=TCLink, autoSetMacs=True)
    net.addController('c0', controller=Controller)
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')
    s1 = net.addSwitch('s1')
    net.addLink(h1, s1, bw=10, delay='10ms')
    net.addLink(h2, s1, bw=10, delay='10ms')
    net.addLink(h4, s1, bw=10, delay='10ms')
    net.addLink(h3, s1, bw=10, delay='10ms')
    net.start()
    net.pingAll()

    link_h3 = h3.connectionsTo(s1)[0]
    switch_iface = link_h3[1].name

    if qos_mode in ('static', 'trust'):
        apply_htb_prio(s1, switch_iface, total_rate_mbit=10)
    else:
        info('*** No QoS applied - plain FIFO on all interfaces ***\n')

    # Mark DSCP at the senders (a real device would tag its own traffic
    # regardless of whether the network happens to honor it)
    h1.cmd('iptables -t mangle -F OUTPUT')
    h1.cmd('iptables -t mangle -A OUTPUT -p tcp --dport 5201 -j DSCP --set-dscp 46')
    h4.cmd('iptables -t mangle -F OUTPUT')
    h4.cmd('iptables -t mangle -A OUTPUT -p tcp --dport 5203 -j DSCP --set-dscp 46')

    EF_SOURCES = {
        '10.0.0.1': ('h1 (legit EF)', h1, 5201),
        '10.0.0.4': ('h4 (attacker)', h4, 5203),
    }

    h3.cmd('pkill iperf3')
    time.sleep(1)
    h3.cmd('iperf3 -s -p 5201 -D')
    h3.cmd('iperf3 -s -p 5202 -D')
    h3.cmd('iperf3 -s -p 5203 -D')
    time.sleep(1)

    p1 = h1.popen(['iperf3', '-c', '10.0.0.3', '-p', '5201', '-t', str(TEST_SECONDS), '-b', '1.5M'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)
    p2 = h2.popen(['iperf3', '-c', '10.0.0.3', '-p', '5202', '-t', str(TEST_SECONDS), '-b', '6M'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)
    p4 = h4.popen(['iperf3', '-c', '10.0.0.3', '-p', '5203', '-t', str(TEST_SECONDS), '-b', '6M'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)

    demoted = set()
    if qos_mode == 'trust':
        info(f'*** TRUST MODULE active: polling every {POLL_SECONDS}s ***\n')
        violation_streak = {ip: 0 for ip in EF_SOURCES}
        elapsed = 0
        while elapsed < TEST_SECONDS:
            time.sleep(POLL_SECONDS)
            elapsed += POLL_SECONDS
            counts = read_sender_egress_bytes(EF_SOURCES)
            for ip, (label, _n, _p) in EF_SOURCES.items():
                r_cum = (counts.get(ip, 0) * 8) / (elapsed * 1e6)
                trust = 1 - abs(r_cum - R_EXP_EF_MBPS) / R_EXP_EF_MBPS
                if elapsed < GRACE_SECONDS:
                    continue
                if trust < THETA:
                    violation_streak[ip] += 1
                else:
                    violation_streak[ip] = 0
                if ip not in demoted and violation_streak[ip] >= CONSECUTIVE_VIOLATIONS_REQUIRED:
                    demote_source(s1, switch_iface, ip)
                    demoted.add(ip)
                    print(f'  [t={elapsed}s] {label}: DEMOTED (R_avg={r_cum:.2f} Mbps, T={trust:.2f})')
    else:
        # No active monitoring in the 'none' and 'static' scenarios -
        # this is exactly the vulnerability being demonstrated.
        time.sleep(TEST_SECONDS)

    outputs = {}
    for key, proc in (('h1', p1), ('h2', p2), ('h4', p4)):
        try:
            out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        outputs[key] = (out, err)

    final_counts = read_sender_egress_bytes(EF_SOURCES)
    h1_mb = final_counts.get('10.0.0.1', 0) / 1e6
    h1_mbps = (final_counts.get('10.0.0.1', 0) * 8) / (TEST_SECONDS * 1e6)
    h4_mb = final_counts.get('10.0.0.4', 0) / 1e6
    h4_mbps = (final_counts.get('10.0.0.4', 0) * 8) / (TEST_SECONDS * 1e6)

    h2_parsed = summarize_iperf_text(outputs['h2'][0])
    h2_mbps = h2_parsed[1] if h2_parsed else None

    result = {
        'scenario': qos_mode,
        'h1_mb': h1_mb, 'h1_mbps': h1_mbps,
        'h4_mb': h4_mb, 'h4_mbps': h4_mbps,
        'h2_mbps': h2_mbps,
        'h4_demoted': '10.0.0.4' in demoted,
    }

    info(f"\n*** {qos_mode.upper()} result: h1={h1_mbps:.2f} Mbps, "
         f"h4={h4_mbps:.2f} Mbps, h2={h2_mbps if h2_mbps else 'n/a'} Mbps, "
         f"h4_demoted={result['h4_demoted']} ***\n")

    h3.cmd('pkill iperf3')
    net.stop()
    os.system('mn -c > /dev/null 2>&1')
    time.sleep(2)
    return result


def main():
    setLogLevel('info')
    results = []
    for mode in ('none', 'static', 'trust'):
        results.append(run_scenario(mode))

    print('\n' + '=' * 78)
    print('WEEK 8 - THREE-WAY COMPARISON SUMMARY')
    print('=' * 78)
    header = f"{'Scenario':<16}{'h1 EF (Mbps)':<16}{'h4 attacker (Mbps)':<20}{'h2 BE (Mbps)':<14}{'h4 demoted?':<12}"
    print(header)
    print('-' * 78)
    label_map = {'none': 'No QoS', 'static': 'Static DSCP', 'trust': 'Trust-Verified'}
    for r in results:
        h2_str = f"{r['h2_mbps']:.2f}" if r['h2_mbps'] is not None else 'n/a'
        print(f"{label_map[r['scenario']]:<16}{r['h1_mbps']:<16.2f}{r['h4_mbps']:<20.2f}{h2_str:<14}{str(r['h4_demoted']):<12}")
    print('=' * 78)
    print('\nInterpretation:')
    print('- No QoS: h1 and h4 compete on raw TCP fairness alone; no priority is honored.')
    print('- Static DSCP: h4 is a spoofed EF flow, so it unfairly shares the protected')
    print('  priority queue with h1 - the vulnerability this project addresses.')
    print('- Trust-Verified: h4 is detected and demoted, restoring h1\'s intended')
    print('  protection without trusting h4\'s claimed priority blindly.')


if __name__ == '__main__':
    main()
