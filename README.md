# Security-Aware QoS: Trust-Verified DSCP Prioritization for IoT Edge Networks

> Course project, CS23502 Networks and Data Communication, Madras Institute of Technology, Anna University, Chennai.

## Overview
IoT edge gateways carry control/alarm messages, video and telemetry over a limited uplink. DiffServ (DSCP) can prioritise urgent traffic, but it trusts every device to mark its own priority honestly. A compromised or misconfigured device can mark bulk traffic as high priority (EF) and take bandwidth from others.

This project adds a **lightweight, rate-based trust-verification module** at the edge gateway. For every flow claiming EF, it compares the flow's cumulative average rate (measured at the source's egress) with the rate expected for that class, computes a trust score, and **demotes** a flow whose score stays low by installing a higher-precedence filter that forces it into the best-effort HTB class.

Full write-up: see the paper in `docs/` (*Security-Aware QoS: Trust-Verified DSCP Prioritization for IoT Edge Networks*).

## Team
| Name | Reg. No. | Email |
|---|---|---|
| Srija Venkataraman | 2024503022 | srijavenkat64@gmail.com |
| Karthikha PR | 2024503570 | karthikhapr@gmail.com |
| Chitabatini Tanushree | 2024503540 | tantanushree24@gmail.com |

## Key Contributions
1. A rate-based trust-verification module for DSCP QoS at an IoT edge gateway (cumulative-average trust score, grace period, consecutive-violation rule).
2. Enforcement by per-source demotion: a filter with higher precedence than the DSCP filter, plus a run-time check that the filter is actually active.
3. A reproducible Linux/Mininet testbed comparing three conditions on one traffic scenario: **No QoS**, **Static DSCP**, **Trust-Verified DSCP**.
4. An evaluation that separates *offered* from *delivered* throughput, reports run-to-run variation, and states the rule used to discard failed runs.

## How It Works
**Trust score** (computed per EF-claiming flow at every poll):

```
R_i(t) = 8 * N_i(t) / t                  # cumulative average rate, from the source's egress byte counter
T_i(t) = 1 - |R_i(t) - R_exp| / R_exp     # R_exp = 2 Mbps for EF control-like traffic
```

A flow is *suspicious* at a poll if `T_i < θ`. It is demoted when suspicious at `K` consecutive polls after a grace period `τg`. Earliest possible demotion time = `τg + (K - 1)·Δ` = 8 s.

| Parameter | Value |
|---|---|
| Link rate / delay | 10 Mbps / 10 ms |
| Test duration | 24 s |
| `R_exp` (EF) | 2 Mbps |
| `θ` (trust threshold) | 0.5 |
| Poll period `Δ` | 2 s |
| Grace period `τg` | 6 s |
| Consecutive violations `K` | 2 |
| HTB classes | EF 1:10 (7/10 Mbit, prio 0), BE 1:20 (3/10 Mbit, prio 1), 50-packet FIFO each |

A constant-rate flow is trusted if its rate is between `θ·R_exp` and `(2-θ)·R_exp`, i.e. **1 to 3 Mbps** with these parameters.

**Traffic (iperf3, TCP):** h1 = legitimate EF flow (~1.5 Mbps), h2 = honest best-effort (6 Mbps), h4 = spoofer claiming EF but sending 6 Mbps, h3 = receiver. The bottleneck is the switch port facing h3. DSCP 46 is set with `iptables` on the sending hosts.

## Results (summary)
Final comparison run, Mbps averaged over 24 s (offered = sent by the host, delivered = arrived at h3):

| Scenario | h1 offered | h1 delivered | h4 offered | h4 delivered | h2 delivered | h4 demoted? |
|---|---|---|---|---|---|---|
| No QoS | 1.17 | 1.06 | 4.86 | 4.22 | 4.45 | No |
| Static DSCP | 1.55 | 1.55 | 5.39 | 5.38 | 3.02 | No |
| Trust-Verified | 1.55 | 1.55 | 4.35 | 4.32 | 3.91 | Yes (t = 8 s) |

- The spoofer h4 was demoted at t = 8 s, the earliest time the rule allows, and the legitimate flow h1 was never flagged (in every run where the network behaved normally).
- Demotion moved about 1 Mbps of delivered throughput from the spoofer to the honest best-effort flow h2; h1 was unchanged.
- **Limitations:** the enforcement result comes from **one valid run** (n = 1). Of 16 scenario attempts, 7 were discarded because flows stalled (cause not found), and one more trust-verified attempt was excluded because of a demotion-filter bug that has since been fixed. Only throughput was measured (no latency or jitter), on an emulated testbed. See the paper for details.

## Project Structure
```
security-aware-qos/
|-- README.md
|-- experiments/        # all code: Mininet scripts, one per experiment stage
|-- results/            # raw output of every experiment (iperf3 logs, trust log, comparison table)
|-- csvs/               # all data as CSV (run history, trust timeline, iperf tables) + readable Excel workbook
|-- docs/               # everything else: paper (PDF) and architecture diagrams
|-- requirements.txt
|-- .gitignore
```

**experiments/** (all code; trust verification, DSCP marking and HTB queuing live inside these scripts)

| File | Stage |
|---|---|
| `baseline_topology.py` | Week 4: h1 to h2, no QoS |
| `dscp_priority_topology.py` | Weeks 5-6: DSCP marking + HTB priority queuing |
| `noqos_comparison_topology.py` | No-QoS reference for the two-flow test |
| `trust_verification_topology.py` | Week 7: trust module with a legitimate EF flow and an attacker |
| `week8_comparison_topology.py` | Week 8: No QoS vs Static DSCP vs Trust-Verified, with validity rule |

**results/** (raw output of the code above)

| File | Produced by |
|---|---|
| `baseline_results.txt` | `baseline_topology.py` |
| `h1_ef_result.txt`, `h2_be_result.txt` | `dscp_priority_topology.py` |
| `h1_noqos_result.txt`, `h2_noqos_result.txt` | `noqos_comparison_topology.py` |
| `trust_verification_results.txt` (trust log every 2 s + tc class counters) | `trust_verification_topology.py` |
| `week8_comparison_results.txt` (paper Table II) | `week8_comparison_topology.py` |

**csvs/** (the same data in table form)

| File | Contents |
|---|---|
| `week8_all_runs.csv` | All 16 scenario attempts with valid/excluded status (original file) |
| `run_history.csv` | The same 16 attempts, with readable column names |
| `trust_timeline.csv` | Trust module log: R and T for h1 and h4 every 2 s |
| `iperf_intervals.csv`, `iperf_summary.csv` | Per-second and end-of-test iperf3 readings |
| `htb_queue_stats.csv` | tc/HTB class counters |
| `QoS_Project_Dataset.xlsx` | All of the above in one workbook, with a README sheet and formulas for the averages |

## Getting Started

### Prerequisites
Ubuntu 22.04 (tested in VirtualBox), run with `sudo` because Mininet needs root.
```bash
sudo apt update
sudo apt install mininet openvswitch-switch iperf3 python3 -y
```

### Run the experiments
Run from the repository root so the Week 8 results are written into `results/`.
```bash
# Week 4: baseline, no QoS (h1 -> h2)
sudo python3 experiments/baseline_topology.py

# Weeks 5-6: DSCP marking + HTB priority queuing (h1 EF, h2 best effort)
sudo python3 experiments/dscp_priority_topology.py
sudo python3 experiments/noqos_comparison_topology.py      # same traffic, no QoS

# Week 7: trust verification with an attacker (h4)
sudo python3 experiments/trust_verification_topology.py

# Week 8: three-way comparison (writes results/week8_comparison_results.txt)
sudo python3 experiments/week8_comparison_topology.py
# optional: change scenario order, e.g.
sudo python3 experiments/week8_comparison_topology.py trust,static,none
```
Notes:
- `week8_comparison_topology.py` discards and repeats an attempt (at most 3 times) if the three flows together deliver < 5 Mbps or any flow delivers < 0.3 Mbps.
- `dscp_priority_topology.py` and `noqos_comparison_topology.py` write their iperf3 logs to `/tmp`; copy them into `results/` if you want to keep them.
- The baseline script uses 20 ms link delay; the later experiments use 10 ms.
- Results vary between runs, especially under plain FIFO (No QoS).

## Reading the Results
Open `csvs/QoS_Project_Dataset.xlsx` first. It indexes every raw file, shows valid vs excluded runs, and recomputes the averages with formulas.

How to read `week8_all_runs.csv`:
- `offered` = Mbps the host sent from its own interface; `delivered` = Mbps that arrived at h3, averaged over 24 s.
- `status`: `valid` passed the validity rule (total delivered >= 5 Mbps and every flow >= 0.3 Mbps); `excluded` was discarded.
- 7 attempts were excluded for stalls (6 trust-verified, 1 static). One more trust-verified attempt (R2) had a healthy network but an ineffective demotion filter (tc priority-0 bug, since fixed) and is also excluded.
- Valid runs: No QoS n = 4, Static DSCP n = 3, Trust-Verified n = 1. Blank cells mean the network collapsed and nothing was measured.
- The `0.00 MB` receiver lines in the no-QoS iperf3 logs mean iperf3 printed no receiver report, not zero delivery.

## Project Workflow (12-Week Plan)
| Weeks | Phase | Status |
|---|---|---|
| 1-2 | Literature review | Done |
| 3 | Tool familiarization (Mininet, tc, iperf3) | Done |
| 4 | Baseline experiments (no QoS) | Done |
| 5-6 | DSCP marking + HTB priority queuing | Done |
| 7 | Trust verification module | Done |
| 8 | Comparative testing (No QoS vs Static DSCP vs Trust-Verified) | Done (n = 1 valid trust run) |
| 9-10 | Data analysis | In progress |
| 11 | Report and PPT preparation | In progress |
| 12 | Final review and submission | Planned |

## Future Work
Log per-interval delivered rates; measure latency and jitter; test a spoofer that saturates the EF class; learn `R_exp` from history and let demoted flows recover; use a one-sided score so quiet legitimate flows are not penalised; find the cause of the stalled runs; repeat on real hardware.

## Tools & Technologies
Linux `tc`/`iproute2` (HTB), `iptables`, Open vSwitch, Mininet, iperf3, Python 3, Matplotlib, draw.io, LaTeX.

## References
See the reference list in the paper (`docs/`). Standards used: RFC 2474, 2475, 2697, 4594, 7657.
