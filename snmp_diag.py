#!/usr/bin/env python3
"""Standalone SNMP GET test using the exact same pysnmp v1arch.asyncio
call path as backend/app/services/snmp_service.py -- run this from the
NetGuard venv to see the real error, bypassing the app's
`except Exception: return None` (which is what turns any failure into
the generic "did not respond" message you see in the UI/alerts).

Usage:
    python3 snmp_diag.py 172.17.1.21 public v2c
"""
import asyncio
import sys

if len(sys.argv) != 4:
    print(f"Usage: {sys.argv[0]} <host> <community> <v1|v2c>")
    sys.exit(1)

host, community, version = sys.argv[1], sys.argv[2], sys.argv[3]
SYSUPTIME_OID = "1.3.6.1.2.1.1.3.0"


async def main():
    from pysnmp.hlapi.v1arch.asyncio import (
        CommunityData,
        ObjectIdentity,
        ObjectType,
        SnmpDispatcher,
        UdpTransportTarget,
        get_cmd,
    )

    mp_model = 0 if version == "v1" else 1
    print(f"Connecting to {host}:161, community={community!r}, mpModel={mp_model} ...")

    with SnmpDispatcher() as dispatcher:
        transport = await UdpTransportTarget.create((host, 161), timeout=3.0, retries=1)
        error_indication, error_status, error_index, var_binds = await get_cmd(
            dispatcher,
            CommunityData(community, mpModel=mp_model),
            transport,
            ObjectType(ObjectIdentity(SYSUPTIME_OID)),
        )

    print(f"error_indication = {error_indication!r}")
    print(f"error_status     = {error_status!r}")
    print(f"error_index      = {error_index!r}")
    print(f"var_binds        = {var_binds!r}")

    if error_indication:
        print("\n>>> FAILED at the engine level (network/timeout/auth) -- see error_indication above.")
    elif error_status:
        print("\n>>> FAILED at the agent level (device rejected the request) -- see error_status above.")
    elif not var_binds:
        print("\n>>> No var_binds returned but no error either -- unexpected empty response.")
    else:
        print(f"\n>>> SUCCESS: sysUpTime = {var_binds[0][1]}")


try:
    asyncio.run(main())
except Exception:
    import traceback
    print("\n>>> Raised an exception (this is what snmp_service.py's bare")
    print(">>> except swallows into 'did not respond'):\n")
    traceback.print_exc()