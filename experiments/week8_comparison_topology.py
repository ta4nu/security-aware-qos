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

def apply_htb_prio(node, iface, total_rate_mbit=10):
    cmds = [
        f"tc qdisc del dev {iface} root",
        f"tc qdisc add dev {iface} root handle 1: htb default 20",
        f"tc class add dev {iface} parent 1: classid 1:1 htb rate {total_rate_mbit}mbit",
        f"tc class add dev {iface} parent 1:1 classid 1:10 htb rate {int(total_rate_mbit*0.7)}mbit ceil {total_rate_mbit}mbit prio 0",
        f"tc class add dev {iface} parent 1:1 classid 1:20 htb rate {int(total_rate_mbit*0.3)}mbit ceil {total_rate_mbit}mbit prio 1",
        f"tc qdisc add dev {iface} parent 1:10 handle 10: pfifo limit 50",
        f"tc qdisc add dev {iface} parent 1:20 handle 20: pfifo limit 50",
        f"tc filter add dev {iface} protocol ip parent 1:0 prio 2 u32 "
        f"match ip tos 0xb8 0xfc flowid 1:10",
    ]
    for c in cmds:
        if "qdisc del" in c:
            node.cmd(c + " 2>/dev/null")
        else:
            node.cmd(c)


def demote_source(switch_node, iface, src_ip):
    switch_node.cmd(
        f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 "
        f"match ip src {src_ip} flowid 1:20"
    )


def demotion_is_active(switch_node, iface):
    """
    Check that the demotion filter really sits AHEAD of the DSCP filter.
    tc checks filters in ascending 'pref' order and the first match wins, so
    the override must have a lower pref than the DSCP filter (pref 2).
    """
    out = switch_node.cmd(f"tc filter show dev {iface}")
    return any(' pref 1 ' in line and 'flowid 1:20' in line for line in out.splitlines())


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


def read_delivered_bytes(h3, ports):
    """
    Read how many bytes of each flow actually ARRIVED at h3, from counting
    rules on h3's PREROUTING chain (one per destination port).

    This is deliberately used only for the final comparison table. The live
    trust decisions keep using sender-side (egress) counters, because judging
    honesty from traffic already shaped by our own queue would be circular.
    Delivered bytes answer a different question: what did each flow actually
    get through the network once the queuing/enforcement had acted on it?
    """
    out = h3.cmd('iptables -t mangle -L PREROUTING -v -n -x')
    delivered = {}
    for port in ports:
        for line in out.splitlines():
            if f'dpt:{port}' in line:
                nums = re.findall(r'\d+', line)
                if len(nums) >= 2:
                    delivered[port] = int(nums[1])
    return delivered


def run_scenario(qos_mode):
    """qos_mode: 'none', 'static', or 'trust'. Returns a results dict."""
    info(f'\n{"="*70}\n*** SCENARIO: {qos_mode.upper()} ***\n{"="*70}\n')

    # Start every scenario from a clean slate: stray iperf3 processes or
    # leftover network state from the previous scenario have produced
    # collapsed, unusable runs before.
    os.system('pkill iperf3 > /dev/null 2>&1; mn -c > /dev/null 2>&1')
    time.sleep(3)

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

    # Counting-only rules at the receiver: bytes of each flow that actually
    # arrive at h3 (used for the final delivered-throughput comparison)
    h3.cmd('iptables -t mangle -F PREROUTING')
    for port in (5201, 5202, 5203):
        h3.cmd(f'iptables -t mangle -A PREROUTING -p tcp --dport {port}')

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
    demote_time = {}
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
                    demote_time[ip] = elapsed
                    ok = demotion_is_active(s1, switch_iface)
                    print(f'  [t={elapsed}s] {label}: DEMOTED (R_avg={r_cum:.2f} Mbps, T={trust:.2f}) '
                          f'[demotion filter active: {ok}]')
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

    def to_mbps(nbytes):
        return (nbytes * 8) / (TEST_SECONDS * 1e6)

    # OFFERED: what h1/h4 pushed out of their own interfaces (sender side)
    sent = read_sender_egress_bytes(EF_SOURCES)
    # DELIVERED: what actually arrived at h3 for each flow (receiver side)
    got = read_delivered_bytes(h3, (5201, 5202, 5203))

    result = {
        'scenario': qos_mode,
        'h1_offered': to_mbps(sent.get('10.0.0.1', 0)),
        'h4_offered': to_mbps(sent.get('10.0.0.4', 0)),
        'h1_delivered': to_mbps(got.get(5201, 0)),
        'h2_delivered': to_mbps(got.get(5202, 0)),
        'h4_delivered': to_mbps(got.get(5203, 0)),
        'h4_demoted': '10.0.0.4' in demoted,
        'h4_demote_time': demote_time.get('10.0.0.4'),
    }

    info(f"\n*** {qos_mode.upper()} result (delivered Mbps): "
         f"h1={result['h1_delivered']:.2f}, h2={result['h2_delivered']:.2f}, "
         f"h4={result['h4_delivered']:.2f}; h4_demoted={result['h4_demoted']} ***\n")

    h3.cmd('pkill iperf3')
    net.stop()
    os.system('mn -c > /dev/null 2>&1')
    time.sleep(2)
    return result


# A healthy run moves most of the ~10 Mbps link's capacity (earlier valid
# runs delivered about 9.4-9.6 Mbps in total across the three flows). A run
# where the network collapses (all flows near zero) says nothing about the
# QoS mechanism, so it is detected, discarded and retried instead of being
# reported as a result.
MIN_TOTAL_DELIVERED_MBPS = 5.0
MAX_ATTEMPTS = 3


def run_valid_scenario(mode):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        r = run_scenario(mode)
        total = r['h1_delivered'] + r['h2_delivered'] + r['h4_delivered']
        r['attempts'] = attempt
        if total >= MIN_TOTAL_DELIVERED_MBPS:
            r['valid'] = True
            return r
        print(f'*** INVALID RUN ({mode}, attempt {attempt}/{MAX_ATTEMPTS}): total delivered '
              f'{total:.2f} Mbps - the network collapsed, so this says nothing about the '
              f'QoS mechanism. Discarding and retrying. ***')
        time.sleep(5)
    r['valid'] = False
    return r


def build_summary(results):
    label_map = {'none': 'No QoS', 'static': 'Static DSCP', 'trust': 'Trust-Verified'}
    width = 100
    lines = []
    lines.append('=' * width)
    lines.append('WEEK 8 - THREE-WAY COMPARISON SUMMARY (all values in Mbps, averaged over the test)')
    lines.append('=' * width)
    lines.append("OFFERED = sent from the host's own interface | DELIVERED = arrived at h3")
    lines.append('-' * width)
    lines.append(f"{'Scenario':<16}{'h1 offered':<12}{'h1 deliv.':<12}{'h4 offered':<12}"
                 f"{'h4 deliv.':<12}{'h2 deliv.':<12}{'h4 demoted?':<14}{'note':<10}")
    lines.append('-' * width)
    for r in results:
        demoted_str = f"Yes (t={r['h4_demote_time']}s)" if r['h4_demoted'] else 'No'
        note = '' if r['valid'] else 'INVALID'
        if r['valid'] and r['attempts'] > 1:
            note = f"retry x{r['attempts'] - 1}"
        lines.append(f"{label_map[r['scenario']]:<16}{r['h1_offered']:<12.2f}{r['h1_delivered']:<12.2f}"
                     f"{r['h4_offered']:<12.2f}{r['h4_delivered']:<12.2f}{r['h2_delivered']:<12.2f}"
                     f"{demoted_str:<14}{note:<10}")
    lines.append('=' * width)
    lines.append('')
    lines.append('How to read this:')
    lines.append('- OFFERED is what each host tried to send; it is what the trust module judges,')
    lines.append('  so it is deliberately not changed by downstream queuing.')
    lines.append('- DELIVERED shows the effect of the queuing/enforcement on what actually got through.')
    lines.append('- h1 delivered: whether the legitimate EF flow got what it sent.')
    lines.append('- h2 delivered: whether honest best-effort traffic was squeezed by the spoofed flow.')
    lines.append('- h4 demoted?: only the trust-verified run can detect the spoofed priority claim.')
    lines.append('- A row marked INVALID never produced a healthy network run; do not use its numbers.')
    return lines


def save_results(lines):
    """Write the summary to a file from inside the script, so it does not
    depend on shell redirection (Mininet's progress messages go to stderr and
    are not captured by a plain > or | tee)."""
    out_dir = 'results' if os.path.isdir('results') else '.'
    path = os.path.join(out_dir, 'week8_comparison_results.txt')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    # The script runs under sudo; hand the file back to the real user so
    # git add / editing works without sudo.
    uid, gid = os.environ.get('SUDO_UID'), os.environ.get('SUDO_GID')
    if uid and gid:
        os.chown(path, int(uid), int(gid))
    return path


def main():
    setLogLevel('info')
    results = [run_valid_scenario(mode) for mode in ('none', 'static', 'trust')]

    lines = build_summary(results)
    print('\n' + '\n'.join(lines))
    path = save_results(lines)
    print(f'\nSummary saved to: {os.path.abspath(path)}')


if __name__ == '__main__':
    main()
