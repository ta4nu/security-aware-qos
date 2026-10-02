from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, Controller
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
import time
import re
import subprocess
from subprocess import PIPE

TEST_SECONDS = 24          # total length of each flow
POLL_SECONDS = 2           # how often the trust module checks behaviour
R_EXP_EF_MBPS = 2.0        # expected rate for genuine EF (control-like) traffic
THETA = 0.5                # trust threshold - below this, a flow is demoted

EF_SOURCES = {
    '10.0.0.1': 'h1 (legitimate EF)',
    '10.0.0.4': 'h4 (ATTACKER claiming EF)',
}

_SENDER_RE = re.compile(
    r'([\d.]+)\s+(KBytes|MBytes|GBytes)\s+([\d.]+)\s+(Kbits/sec|Mbits/sec|Gbits/sec)'
    r'(?:\s+(\d+))?\s+sender'
)


def summarize_iperf(label, out, err):
    match = None
    for line in out.splitlines():
        m = _SENDER_RE.search(line)
        if m:
            match = m
    if not match:
        hint = err.strip()[:200] or '(no stderr - process likely still finishing up)'
        print(f'{label}: could not find a sender summary line. stderr: {hint}')
        return
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
    retr = int(retr) if retr else 0
    print(f'{label}: sent {transfer:.2f} MB at {rate:.2f} Mbps, retransmits = {retr}')


def apply_htb_prio(node, iface, total_rate_mbit=10):
    """Same HTB+PRIO setup as the Week 5-6 script: EF class 1:10, BE class 1:20."""
    cmds = [
        f"tc qdisc del dev {iface} root",
        f"tc qdisc add dev {iface} root handle 1: htb default 20",
        f"tc class add dev {iface} parent 1: classid 1:1 htb rate {total_rate_mbit}mbit",
        f"tc class add dev {iface} parent 1:1 classid 1:10 htb rate {int(total_rate_mbit*0.7)}mbit ceil {total_rate_mbit}mbit prio 0",
        f"tc class add dev {iface} parent 1:1 classid 1:20 htb rate {int(total_rate_mbit*0.3)}mbit ceil {total_rate_mbit}mbit prio 1",
        f"tc qdisc add dev {iface} parent 1:10 handle 10: pfifo limit 50",
        f"tc qdisc add dev {iface} parent 1:20 handle 20: pfifo limit 50",
        # prio 1 filter: DSCP-based classification (the "claimed" priority)
        f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 "
        f"match ip tos 0xb8 0xfc flowid 1:10",
    ]
    for c in cmds:
        if "qdisc del" in c:
            node.cmd(c + " 2>/dev/null")
        else:
            out = node.cmd(c)
            if out.strip():
                print(f"[tc] {c}\n    -> {out.strip()}")


def demote_source(switch_node, iface, src_ip):
    """
    Insert a prio-0 (higher precedence than the DSCP filter's prio 1) tc
    filter that matches this specific source IP and forces it into the
    low-priority queue (1:20), overriding whatever DSCP tag it claims.
    This is the trust module's enforcement action.
    """
    out = switch_node.cmd(
        f"tc filter add dev {iface} protocol ip parent 1:0 prio 0 u32 "
        f"match ip src {src_ip} flowid 1:20"
    )
    if out.strip():
        print(f"[demote] {out.strip()}")


def read_counts(h3, ip_to_rule_index):
    """Read exact (not abbreviated) packet/byte counts per source IP from
    the iptables mangle PREROUTING accounting rules set up on h3."""
    out = h3.cmd('iptables -t mangle -L PREROUTING -v -n -x')
    counts = {}
    for line in out.splitlines():
        for ip in ip_to_rule_index:
            if ip in line:
                nums = re.findall(r'\d+', line)
                if len(nums) >= 2:
                    counts[ip] = int(nums[1])  # bytes column
    return counts


def run_trust_experiment():
    net = Mininet(switch=OVSKernelSwitch, link=TCLink, autoSetMacs=True)

    info('*** Adding controller\n')
    net.addController('c0', controller=Controller)

    info('*** Adding hosts (h1=legit EF, h2=BE, h4=attacker spoofing EF)\n')
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')

    info('*** Adding switch\n')
    s1 = net.addSwitch('s1')

    info('*** Creating links (shared 10 Mbps bottleneck at h3)\n')
    net.addLink(h1, s1, bw=10, delay='10ms')
    net.addLink(h2, s1, bw=10, delay='10ms')
    net.addLink(h4, s1, bw=10, delay='10ms')
    net.addLink(h3, s1, bw=10, delay='10ms')

    info('*** Starting network\n')
    net.start()
    net.pingAll()

    link_h3 = h3.connectionsTo(s1)[0]
    switch_iface = link_h3[1].name
    info(f'*** Applying HTB+PRIO QoS on switch interface {switch_iface} (facing h3) ***\n')
    apply_htb_prio(s1, switch_iface, total_rate_mbit=10)

    # Mark DSCP at the senders via iptables (not iperf3 flags - see Week 5-6 notes)
    h1.cmd('iptables -t mangle -F OUTPUT')
    h1.cmd('iptables -t mangle -A OUTPUT -p tcp --dport 5201 -j DSCP --set-dscp 46')
    h4.cmd('iptables -t mangle -F OUTPUT')
    h4.cmd('iptables -t mangle -A OUTPUT -p tcp --dport 5203 -j DSCP --set-dscp 46')
    # h2 sends no DSCP mark at all -> arrives as BE (DSCP 0) by default

    # Counting rules at h3 to observe each EF-claiming source's actual byte rate
    h3.cmd('iptables -t mangle -F PREROUTING')
    for ip in EF_SOURCES:
        h3.cmd(f'iptables -t mangle -A PREROUTING -s {ip} -m dscp --dscp 46')

    h3.cmd('pkill iperf3')
    time.sleep(1)
    info('*** Starting three iperf3 servers on h3 (ports 5201, 5202, 5203)\n')
    h3.cmd('iperf3 -s -p 5201 -D')
    h3.cmd('iperf3 -s -p 5202 -D')
    h3.cmd('iperf3 -s -p 5203 -D')
    time.sleep(1)

    info('\n*** h1 (legit EF, ~1.5 Mbps): starting ***\n')
    p1 = h1.popen(['iperf3', '-c', '10.0.0.3', '-p', '5201', '-t', str(TEST_SECONDS), '-b', '1.5M'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)

    info('*** h2 (BE, ~6 Mbps): starting ***\n')
    p2 = h2.popen(['iperf3', '-c', '10.0.0.3', '-p', '5202', '-t', str(TEST_SECONDS), '-b', '6M'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)

    info('*** h4 (ATTACKER - claims EF, actually sends ~6 Mbps): starting ***\n')
    p4 = h4.popen(['iperf3', '-c', '10.0.0.3', '-p', '5203', '-t', str(TEST_SECONDS), '-b', '6M'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)

    # --- Trust verification loop ---
    info(f'\n*** TRUST MODULE: polling every {POLL_SECONDS}s, R_exp(EF)={R_EXP_EF_MBPS} Mbps, theta={THETA} ***\n')
    GRACE_SECONDS = 6   # skip judgement during early TCP slow-start/ramp-up
    CONSECUTIVE_VIOLATIONS_REQUIRED = 2  # extra safety margin on top of averaging

    demoted = set()
    violation_streak = {ip: 0 for ip in EF_SOURCES}
    elapsed = 0
    while elapsed < TEST_SECONDS:
        time.sleep(POLL_SECONDS)
        elapsed += POLL_SECONDS
        counts = read_counts(h3, EF_SOURCES)
        print(f'\n--- t={elapsed}s ---')
        for ip, label in EF_SOURCES.items():
            cum_bytes = counts.get(ip, 0)
            r_cum = (cum_bytes * 8) / (elapsed * 1e6)  # cumulative avg Mbps since start
            trust = 1 - abs(r_cum - R_EXP_EF_MBPS) / R_EXP_EF_MBPS

            if elapsed < GRACE_SECONDS:
                print(f'{label:32s} R_avg={r_cum:6.2f} Mbps  T={trust:6.2f}  [warming up - grace period]')
                continue

            if trust < THETA:
                violation_streak[ip] += 1
            else:
                violation_streak[ip] = 0

            if ip in demoted:
                status = 'SUSPICIOUS (already demoted)' if trust < THETA else 'TRUSTED (still demoted from earlier)'
            elif violation_streak[ip] >= CONSECUTIVE_VIOLATIONS_REQUIRED:
                demote_source(s1, switch_iface, ip)
                demoted.add(ip)
                status = f'SUSPICIOUS x{violation_streak[ip]} -> DEMOTED NOW'
            elif trust < THETA:
                status = f'suspicious ({violation_streak[ip]}/{CONSECUTIVE_VIOLATIONS_REQUIRED}, not yet demoted)'
            else:
                status = 'TRUSTED'
            print(f'{label:32s} R_avg={r_cum:6.2f} Mbps  T={trust:6.2f}  [{status}]')

    info('\n*** Waiting for flows to finish ***\n')
    outputs = []
    for label, proc in (('h1 (legit EF)', p1), ('h2 (BE)', p2), ('h4 (attacker)', p4)):
        try:
            out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        outputs.append((label, out, err))

    info('\n*** tc class counters on the switch interface ***\n')
    print(s1.cmd(f'tc -s class show dev {switch_iface}'))

    info('\n*** RESULTS SUMMARY ***\n')
    for label, out, err in outputs:
        summarize_iperf(label, out, err)

    print(f'\nFlows demoted by the trust module during this run: {list(demoted) or "none"}')

    info('\n*** Dropping into CLI - type "exit" to stop the network ***\n')
    CLI(net)

    h3.cmd('pkill iperf3')
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run_trust_experiment()
