# Security-Aware QoS: Trust-Verified DSCP Prioritization for IoT Edge Networks

## Overview
IoT edge networks carry heterogeneous traffic — control/alarm messages, video streams, and sensor telemetry — competing for limited uplink bandwidth. Conventional DSCP-based DiffServ QoS lets devices self-declare their traffic priority, but this assumes devices tag their traffic honestly. A malicious or misconfigured device can falsely mark low-priority bulk traffic (e.g., video) as high-priority, stealing bandwidth from genuinely urgent flows.

This project adds a **lightweight trust-verification layer** at the IoT edge gateway that checks whether a flow's claimed DSCP priority matches its actual observed behavior (packet rate, size, burstiness) — re-classifying and logging any flow that appears to be spoofing its priority — before HTB+PRIO priority queuing forwards traffic onward.

## Team
| Name | Reg. No. | Email |
|---|---|---|
| Srija Venkataraman | 2024503022 | srijavenkat64@gmail.com |
| Karthikha PR | 2024503570 | karthikhapr@gmail.com |
| Chitabatini Tanushree | 2024503540 | tantanushree24@gmail.com |

**Course:** CS23502 — Networks and Data Communication
**Institution:** Madras Institute of Technology, Anna University, Chennai

## Key Contributions
1. A lightweight trust-verification layer for DSCP-based QoS at an IoT edge gateway.
2. A rate-based trust score comparing observed flow behavior with the expected behavior of the claimed priority.
3. Dynamic re-classification of suspicious flows before priority queuing, with monitoring/logging of possible DSCP spoofing.
4. A reproducible Linux/Mininet testbed for comparing ordinary QoS and security-aware QoS under congestion.

## Architecture
IoT devices mark packets according to their claimed service class (control/alarm → EF, video → AF4x, telemetry → BE). At the edge gateway, the trust-verification block checks the claimed DSCP value against observed flow behavior. Trusted traffic proceeds with its original priority; suspicious traffic is re-classified and logged. Finally, HTB+PRIO performs priority queuing before traffic enters the congested uplink.

See `diagrams/architecture.drawio` for the editable source and `diagrams/architecture.jpeg` for the exported image.

## Trust Score Formula
```
T_i = 1 - |R_i^obs - R_exp(d_i)| / R_exp(d_i)
```
A flow is flagged as suspicious when `T_i < θ` (trust threshold), triggering re-classification and logging.

## Project Structure
```
project-root/
|-- README.md
|-- src/
|   |-- dscp_marking/         # DSCP tagging scripts (iptables/tc filters)
|   |-- trust_verification/   # Trust score computation and re-classification logic
|   |-- qos_queuing/          # HTB + PRIO queuing configuration
|-- experiments/              # Mininet topology scripts for each experiment stage
|-- scripts/                  # Automation scripts for running/repeating experiments
|-- results/                  # Raw and processed experiment output (iperf3/Wireshark logs)
|-- diagrams/                 # Architecture diagrams (draw.io source + exported images)
|-- docs/                     # Report drafts, references, slides
`-- requirements.txt          # Python dependencies
```

## Tools & Technologies
- Linux (`tc`, `iproute2`, `iptables`/`nftables` for DSCP marking)
- Mininet (edge topology emulation)
- `tc qdisc`: HTB, PRIO, netem
- iperf3, Wireshark (traffic generation and capture/analysis)
- Mosquitto (MQTT broker) for realistic IoT telemetry traffic
- Python/Bash for the trust-verification classifier and automation
- Matplotlib / Excel for plotting results

## Getting Started

### Prerequisites
```bash
sudo apt update
sudo apt install mininet iperf3 wireshark mosquitto mosquitto-clients -y
```

### Running the baseline experiment (no QoS)
```bash
cd experiments
sudo python3 baseline_topology.py
```

### Running the DSCP + priority queuing experiment
```bash
cd experiments
sudo python3 dscp_priority_topology.py
```

## Project Workflow (12-Week Plan)
| Weeks | Phase |
|---|---|
| 1–2 | Literature review |
| 3 | Tool familiarization (Mininet, tc, iperf3) |
| 4 | Baseline experiments (no QoS) |
| 5–6 | DSCP marking + HTB/PRIO priority queuing |
| 7 | Trust verification module (security layer) |
| 8 | Comparative testing (no QoS vs. static QoS vs. trust-verified QoS) |
| 9–10 | Data analysis |
| 11 | Report and PPT preparation |
| 12 | Final review and submission |

## Current Status
- ✅ Baseline (no-QoS) experiment completed — see `results/baseline/`
- 🔄 DSCP marking + priority queuing in progress
- ⬜ Trust verification module — not yet started
- ⬜ Comparative analysis — not yet started

## References
See `docs/references.md` for the full list of cited works (IEEE Communications Surveys & Tutorials, IEEE Network, IEEE/ACM Transactions on Networking, IEEE Access, IEEE WCNC 2025, IEEE GLOBECOM 2024/2025, and RFC 2474/4594).
