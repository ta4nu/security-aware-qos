from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, Controller
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
import time
import json
import subprocess
from subprocess import PIPE

TEST_SECONDS = 10


def summarize_iperf(label, out, err):
    """Print sender throughput from iperf3's per-second intervals."""
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
    lost = sum(i['sum'].get('lost_packets', 0) for i in intervals)
    print(f'{label}: sent {total_bytes / 1e6:.2f} MB in {secs:.1f}s = '
          f'{total_bytes * 8 / secs / 1e6:.2f} Mbps, retransmits = {retr}')


def run_no_qos_comparison():
    net = Mininet(switch=OVSKernelSwitch, link=TCLink, autoSetMacs=True)

    info('*** Adding controller\n')
    net.addController('c0', controller=Controller)

    info('*** Adding hosts\n')
    h1 = net.addHost('h1', ip='10.0.0.1/24')  
    h2 = net.addHost('h2', ip='10.0.0.2/24') 
    h3 = net.addHost('h3', ip='10.0.0.3/24') 

    info('*** Adding switch\n')
    s1 = net.addSwitch('s1')

    info('*** Creating links (same 10 Mbps bottleneck as the QoS run, for a fair comparison)\n')
    net.addLink(h1, s1, bw=10, delay='10ms')
    net.addLink(h2, s1, bw=10, delay='10ms')
    net.addLink(h3, s1, bw=10, delay='10ms')

    info('*** Starting network\n')
    net.start()
    net.pingAll()

    info('*** NOTE: No HTB/PRIO/DSCP filtering applied - plain FIFO queuing only ***\n')

    h3.cmd('pkill iperf3')
    time.sleep(1)

    info('*** Starting two iperf3 servers on h3 (ports 5201 and 5202)\n')
    h3.cmd('iperf3 -s -p 5201 -D')
    h3.cmd('iperf3 -s -p 5202 -D')
    time.sleep(1)

    info('\n*** Flow 1 (tagged EF/DSCP46, but un-prioritized here): h1 -> h3, 5 Mbps offered ***\n')
    p1 = h1.popen(['iperf3', '-c', '10.0.0.3', '-p', '5201', '-t', str(TEST_SECONDS),
                   '-b', '5M', '-S', '184', '-J'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)

    info('*** Flow 2 (BE, untagged): h2 -> h3, 8 Mbps offered, same time ***\n')
    p2 = h2.popen(['iperf3', '-c', '10.0.0.3', '-p', '5202', '-t', str(TEST_SECONDS),
                   '-b', '8M', '-J'],
                  stdout=PIPE, stderr=PIPE, universal_newlines=True)

    info('*** Waiting for both flows to finish ***\n')
    outputs = []
    for label, proc in (('FLOW 1 (EF-tagged, h1)', p1), ('FLOW 2 (BE, h2)', p2)):
        try:
            out, err = proc.communicate(timeout=TEST_SECONDS + 40)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        outputs.append((label, out, err))
        with open('/tmp/' + ('h1_noqos' if 'FLOW 1' in label else 'h2_noqos') + '_result.json', 'w') as f:
            f.write(out)

    info('\n*** RESULTS SUMMARY (NO QoS - for comparison against the QoS run) ***\n')
    for label, out, err in outputs:
        summarize_iperf(label, out, err)

    info('\n*** Compare these numbers against dscp_priority_topology.py\'s output. ***\n')
    info('*** Raw JSON saved in /tmp/h1_noqos_result.json and /tmp/h2_noqos_result.json ***\n')
    info('*** Dropping into CLI - type "exit" to stop the network ***\n')
    CLI(net)

    h3.cmd('pkill iperf3')
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run_no_qos_comparison()
