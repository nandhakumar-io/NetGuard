#!/usr/bin/env python3
"""Quick connectivity check for old Cisco-style gear that only offers
legacy SSH crypto (diffie-hellman-group1-sha1, aes128-cbc, etc.) that
modern system OpenSSH refuses to speak.

Uses paramiko directly (same version NetGuard's backend uses --
requirements.txt pins paramiko==3.4.0, which still supports this legacy
kex, unlike paramiko>=5.0 or a modern OpenSSH client binary).

Usage:
    python3 legacy_ssh_test.py 172.17.1.21 admin <password>
"""
import sys

import paramiko

if len(sys.argv) != 4:
    print(f"Usage: {sys.argv[0]} <host> <username> <password>")
    sys.exit(1)

host, username, password = sys.argv[1], sys.argv[2], sys.argv[3]

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

# Explicitly allow the legacy algorithms this device offers, in case a
# newer paramiko/cryptography combo has tightened defaults.
client.connect(
    host,
    username=username,
    password=password,
    timeout=10,
    disabled_algorithms={},  # don't disable anything -- allow legacy kex/ciphers
    look_for_keys=False,
    allow_agent=False,
)

stdin, stdout, stderr = client.exec_command("show version" if True else "")
print(stdout.read().decode(errors="replace"))
client.close()