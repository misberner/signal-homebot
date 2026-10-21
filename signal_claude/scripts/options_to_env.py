"""Export app options as safely shell-quoted environment assignments.

The options hold only secrets and connections; household settings live in the
setup page. The options are only available to Home Assistant admins. No values
are logged.
"""
from __future__ import annotations
import json, shlex, sys
from pathlib import Path

opts = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else '/data/options.json').read_text())
fields = {
  'ha_base_url': 'HA_BASE_URL',
  'ha_mcp_url': 'HA_MCP_URL',
  'ha_mcp_kind': 'HA_MCP_KIND',
  'ha_mcp_domains': 'HA_MCP_DOMAINS',
  'ha_token': 'HA_TOKEN',
  'claude_oauth_token': 'CLAUDE_OAUTH_TOKEN',
  'notify_token': 'NOTIFY_TOKEN',
}
for key, env in fields.items():
    val = opts.get(key, '')
    if not isinstance(val, str):
        raise ValueError(f'{key} must be a string')
    print(f'export {env}={shlex.quote(val.strip())}')
