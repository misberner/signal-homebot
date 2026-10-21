"""Print trust level and safety number from `signal-cli --output=json listIdentities`.

Safety numbers are not secrets; compare them with the number shown on each
person's phone (Signal → conversation with the bot → View safety number).
"""
from __future__ import annotations
import json, sys
try:
    identities = json.load(sys.stdin)
except ValueError:
    identities = []
if not identities:
    print('  no identity known yet: this person must message the bot first (setup_mode listen)')
for ident in identities:
    print(f"  trust level:   {ident.get('trustLevel')}")
    print(f"  safety number: {ident.get('safetyNumber')}")
