"""Print sender UUID and group ID only; never log the full Signal text."""
from __future__ import annotations
import json,sys
for line in sys.stdin:
    try:
        item=json.loads(line)
        env=item.get('envelope') or {}
        grp=((env.get('dataMessage') or {}).get('groupInfo') or {})
        if env.get('sourceUuid'):
            print('sourceUuid:',env['sourceUuid'],flush=True)
        if grp.get('groupId'):
            print('groupId:',grp['groupId'],flush=True)
    except (ValueError,AttributeError):
        continue
