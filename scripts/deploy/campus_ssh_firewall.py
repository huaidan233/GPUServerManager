#!/usr/bin/env python3
"""Restrict Docker-published workload SSH to the configured campus networks."""
import ipaddress
import os
import subprocess


def main():
    host = str(ipaddress.IPv4Address(os.environ["GSM_CAMPUS_SSH_HOST"]))
    networks = [str(ipaddress.IPv4Network(value.strip(), strict=False))
                for value in os.environ["GSM_CAMPUS_SSH_CIDRS"].split(",") if value.strip()]
    start = int(os.environ.get("GSM_CAMPUS_SSH_PORT_START", "23001"))
    end = int(os.environ.get("GSM_CAMPUS_SSH_PORT_END", "23999"))
    if not networks or not 1024 <= start <= end <= 65535:
        raise ValueError("explicit campus networks and a valid workload port range are required")
    chain = "GSM-CAMPUS-SSH"
    check = subprocess.run(["iptables", "-w", "-S", chain], capture_output=True)
    if check.returncode:
        subprocess.run(["iptables", "-w", "-N", chain], check=True)
    # Commit only our chain atomically; preserve every other host and Docker rule.
    rules = ["*filter", f"-F {chain}"]
    rules.extend(f"-A {chain} -s {network} -j RETURN" for network in networks)
    rules.extend([f"-A {chain} -j DROP", "COMMIT", ""])
    subprocess.run(["iptables-restore", "--wait", "--noflush"], input="\n".join(rules), text=True, check=True)
    match = ["DOCKER-USER", "-p", "tcp", "-m", "conntrack", "--ctdir", "ORIGINAL",
             "--ctorigdst", host, "--ctorigdstport", f"{start}:{end}", "-j", chain]
    if subprocess.run(["iptables", "-w", "-C", *match], capture_output=True).returncode:
        subprocess.run(["iptables", "-w", "-I", *match], check=True)
    print(f"Workload SSH {host}:{start}-{end} restricted to {', '.join(networks)}")


if __name__ == "__main__":
    main()
