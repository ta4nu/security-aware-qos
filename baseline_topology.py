from mininet.net import Mininet
from mininet.node import OVSKernelSwitch
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel,info

def run_baseline():
	net = Mininet(switch=OVSKernelSwitch, link=TCLink,  autoSetMacs=True)

	info('*** Adding controller\n')
	net.addController('c0')

	info('*** Adding hosts\n')
	h1=net.addHost('h1',ip='10.0.0.1/24')
	h2=net.addHost('h2',ip='10.0.0.2/24')

	info('*** Adding switch\n')
	s1=net.addSwitch('s1')

	info('***Creating links (10Mbps, 20ms delay - emulates a constrained edge link)\n')
	net.addLink(h1, s1, bw=10, delay='20ms', loss=0)
	net.addLink(h2, s1, bw=10, delay='20ms', loss=0)

	info('*** Starting network\n')
	net.start()

	info('*** Testing connectivity\n')
	net.pingAll()

	info('\n*** Running baseline iperf3 test (no QoS applied) ***\n')
	h2.cmd('iperf3 -s -D') 
	result = h1.cmd('iperf3 -c 10.0.0.2 -t 10 -i 1')
	print(result)

	info('*** Dropping into CLI - type "exit" to stop the network ***\n')
	CLI(net)

	info('*** Stopping network\n')
	net.stop()


if __name__== '__main__':
    setLogLevel('info')
    run_baseline()
