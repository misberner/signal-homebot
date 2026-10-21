"""Export app options as safely shell-quoted environment assignments.

The options are only available to Home Assistant admins. No values are logged.
"""
from __future__ import annotations
import json, shlex, sys
from pathlib import Path

opts = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else '/data/options.json').read_text())
fields = {
  'signal_phone':'SIGNAL_ACCOUNT',
  'signal_bot_aci':'SIGNAL_BOT_ACI',
  'signal_user1_aci':'SIGNAL_USER1_ACI',
  'signal_user2_aci':'SIGNAL_USER2_ACI',
  'signal_group_id':'SIGNAL_GROUP_ID',
  'bot_name':'BOT_NAME',
  'ha_base_url':'HA_BASE_URL',
  'ha_mcp_url':'HA_MCP_URL',
  'ha_mcp_kind':'HA_MCP_KIND',
  'ha_mcp_domains':'HA_MCP_DOMAINS',
  'ha_token':'HA_TOKEN',
  'claude_oauth_token':'CLAUDE_OAUTH_TOKEN',
  'notify_token':'NOTIFY_TOKEN',
  'signal_user1_safety_number':'SIGNAL_USER1_SAFETY_NUMBER',
  'signal_user2_safety_number':'SIGNAL_USER2_SAFETY_NUMBER',
}
for key, env in fields.items():
    val = opts.get(key,'')
    if not isinstance(val,str):
        raise ValueError(f'{key} must be a string')
    print(f'export {env}={shlex.quote(val.strip())}')
for key, env in [('setup_mode','SETUP_MODE'),('verification_code','VERIFY_CODE'),('signal_captcha','SIGNAL_CAPTCHA')]:
    print(f'export {env}={shlex.quote(str(opts.get(key,"")))}')
# Writes stay disabled unless the option is literally true.
print(f'export ALLOW_WRITES={"true" if opts.get("allow_writes") is True else "false"}')
# Verified safety numbers are required unless explicitly switched off.
print(f'export REQUIRE_VERIFIED_IDENTITY={"false" if opts.get("require_verified_identity") is False else "true"}')
for key, env, default, lo, hi in [('command_max_age_seconds','COMMAND_MAX_AGE',300,30,3600),
                                  ('history_days','HISTORY_DAYS',7,1,30)]:
    val = opts.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
        raise ValueError(f'{key} must be an integer between {lo} and {hi}')
    print(f'export {env}={val}')
