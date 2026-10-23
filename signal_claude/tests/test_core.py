"""Unit tests for bot/core.py. Standard library only (no aiohttp/mcp needed)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from bot.core import (Store, Incoming, Reaction, RemoteDelete, Settings, Denied, ClaudeError, addressed,
                      freshness, parse_signal_event, load_permissions, permission, validate_action, describe,
                      build_schema, parse_claude_output, parse_verdict, render_overview, ha_mcp_overview_args,
                      identity_trusted, group_roster_problem, APPROVAL_TTL)
from bot.household import Household, HouseholdError, HouseholdStore

BOT = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
USER1 = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
USER2 = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
GROUP = 'abc=/base64'
USERS = {USER1, USER2}
def signal_event(sender=USER1, group=None, text='test', ts=None, **dm_extra):
    """signal-cli 0.14.9 HTTP SSE payload: {"account", "envelope"}."""
    ts = ts or int(time.time() * 1000)
    msg = {'message': text, 'timestamp': ts, **dm_extra}
    if group is not None:
        msg['groupInfo'] = {'groupId': group, 'revision': 1, 'type': 'DELIVER'}
    return {'account': '+49151', 'envelope': {'sourceUuid': sender, 'timestamp': ts,
                                              'serverReceivedTimestamp': ts + 20,
                                              'serverDeliveredTimestamp': ts + 40, 'dataMessage': msg}}


def incoming(ts_offset_s=0.0, server_offset_s=None, expires=0, now=None):
    now = now or time.time()
    ts = int((now + ts_offset_s) * 1000)
    server = int((now + (server_offset_s if server_offset_s is not None else ts_offset_s)) * 1000)
    return Incoming(USER1, 'dm:' + USER1, False, ts, 'x', expires, server)


class ParseTests(unittest.TestCase):
    def parse(self, event):
        return parse_signal_event(event, BOT, USERS, {GROUP})

    def test_one_group_and_two_dms(self):
        a = self.parse(signal_event(USER1, GROUP))
        self.assertTrue(a.group)
        self.assertEqual(a.conv, 'group:' + GROUP)
        self.assertIsNone(self.parse(signal_event(USER1, 'OTHER')))
        self.assertIsNone(self.parse(signal_event('unknown')))
        self.assertIsNone(self.parse(signal_event(BOT)))
        b = self.parse(signal_event(USER2.upper()))
        self.assertFalse(b.group)
        self.assertEqual(b.conv, 'dm:' + USER2)

    def test_jsonrpc_notification_form_also_accepted(self):
        event = signal_event(USER1)
        self.assertIsNotNone(self.parse({'jsonrpc': '2.0', 'method': 'receive', 'params': event}))

    def test_malformed_and_irrelevant_events_rejected(self):
        for event in (None, [], {}, {'envelope': 'x'}, {'exception': {'message': 'x'}},
                      {'envelope': {'sourceUuid': USER1, 'receiptMessage': {}}},
                      {'envelope': {'sourceUuid': USER1, 'syncMessage': {'sentMessage': {'message': 'hi'}}}},
                      {'envelope': {'sourceUuid': USER1, 'editMessage': {'dataMessage': {'message': '/ha x'}}}},
                      signal_event(text=''), signal_event(text=None), signal_event(viewOnce=True),
                      signal_event(ts=True), signal_event(ts=-1), signal_event(expiresInSeconds='9'),
                      signal_event(group=GROUP, text='x') | {'envelope': {
                          **signal_event(group=GROUP)['envelope'],
                          'dataMessage': {'message': 'x', 'timestamp': 5,
                                          'groupInfo': {'groupId': GROUP, 'type': 'UPDATE'}}}}):
            self.assertIsNone(self.parse(event), event)

    def test_reactions_and_quotes(self):
        r = self.parse(signal_event(USER1, None, None, reaction={
            'emoji': '👍', 'targetAuthorUuid': BOT.upper(), 'targetSentTimestamp': 42, 'isRemove': False}))
        self.assertIsInstance(r, Reaction)
        self.assertEqual((r.emoji, r.target_ts, r.target_author, r.remove), ('👍', 42, BOT, False))
        self.assertIsNone(self.parse(signal_event(USER1, None, None, reaction={'emoji': '👍'})))
        q = self.parse(signal_event(USER1, None, 'yes', quote={'id': 77, 'authorUuid': BOT, 'text': 'Shall I'}))
        self.assertEqual((q.text, q.quote_ts, q.quote_author), ('yes', 77, BOT))
        self.assertEqual(self.parse(signal_event(USER1, None, 'yes', quote={'id': 'x'})).quote_ts, 0)

    def test_mentions_and_remote_delete(self):
        msg = parse_signal_event(signal_event(USER1, GROUP, '￼ lamp?', mentions=[
            {'uuid': BOT, 'start': 0, 'length': 1}]), BOT, USERS, {GROUP}, 'HomeBot')
        self.assertIsNone(parse_signal_event(signal_event(USER1, GROUP), BOT, USERS, set()))  # no groups
        self.assertTrue(msg.mentions_bot)
        self.assertEqual(msg.text, '@HomeBot lamp?')
        deletion = self.parse(signal_event(USER2, GROUP, None, remoteDelete={'targetTimestamp': 42}))
        self.assertEqual(deletion, RemoteDelete(USER2, 'group:' + GROUP, 42))

    def test_addressing(self):
        self.assertFalse(addressed('The heat was cold last night', True, 'HomeBot'))
        self.assertFalse(addressed('mail@HomeBotanics.de', True, 'HomeBot'))
        self.assertTrue(addressed('/ha why?', True, 'HomeBot'))
        self.assertTrue(addressed('!HA why?', True, 'HomeBot'))
        self.assertTrue(addressed('hey @HomeBot why?', True, 'HomeBot'))
        self.assertTrue(addressed('lamp?', True, 'HomeBot', mentions_bot=True))
        self.assertTrue(addressed('hi', False, 'HomeBot'))


class TrustTests(unittest.TestCase):
    def test_identity_trust_levels(self):
        def ids(*levels, uuid=USER1):
            return [{'uuid': uuid.upper(), 'trustLevel': level} for level in levels]
        self.assertTrue(identity_trusted(ids('TRUSTED_VERIFIED'), USER1, True))
        self.assertFalse(identity_trusted(ids('TRUSTED_UNVERIFIED'), USER1, True))
        self.assertTrue(identity_trusted(ids('TRUSTED_UNVERIFIED'), USER1, False))
        self.assertFalse(identity_trusted(ids('UNTRUSTED'), USER1, False))
        self.assertFalse(identity_trusted(ids('TRUSTED_VERIFIED', 'UNTRUSTED'), USER1, True))
        self.assertFalse(identity_trusted(ids('TRUSTED_VERIFIED', uuid=USER2), USER1, False))
        for bad in ([], None, {'trustLevel': 'TRUSTED_VERIFIED'}, ['x']):
            self.assertFalse(identity_trusted(bad, USER1, False))

    def test_group_roster(self):
        def group(**kw):
            g = {'id': GROUP, 'isMember': True, 'isBlocked': False, 'pendingMembers': [],
                 'members': [{'uuid': u} for u in (BOT, USER1, USER2)]}
            return [{**g, **kw}]
        self.assertIsNone(group_roster_problem(group(), GROUP, BOT, USERS))
        self.assertIsNone(group_roster_problem(group(requestingMembers=[{'uuid': 'x'}]), GROUP, BOT, USERS))
        # A subset of the configured users is fine, and so is inviting one of them.
        self.assertIsNone(group_roster_problem(group(members=[{'uuid': BOT}, {'uuid': USER1}],
                                                     pendingMembers=[{'uuid': USER2}]), GROUP, BOT, USERS))
        for bad in (group(members=[{'uuid': u} for u in (BOT, USER1, USER2, 'evil')]),
                    group(members=[{'uuid': u} for u in (USER1, USER2)]),                 # bot missing
                    group(members=[{'uuid': BOT}, {'uuid': USER1}, {'number': '+49'}]),   # unknown uuid
                    group(pendingMembers=[{'uuid': 'evil'}]), group(isMember=False),
                    group(isBlocked=True), group(isTerminated=True), group(members=None),
                    group(id='other'), None, []):
            self.assertIsNotNone(group_roster_problem(bad, GROUP, BOT, USERS), bad)


class FreshnessTests(unittest.TestCase):
    def test_policy(self):
        s, now = Settings(command_max_age=300, clock_skew=120, history_days=7), time.time()
        ms = int(now * 1000)
        self.assertEqual(freshness(incoming(-10, now=now), ms, s), 'fresh')
        self.assertEqual(freshness(incoming(60, now=now), ms, s), 'fresh')  # within skew
        self.assertEqual(freshness(incoming(600, now=now), ms, s), 'future')
        self.assertEqual(freshness(incoming(0, 600, now=now), ms, s), 'future')
        self.assertEqual(freshness(incoming(-301, now=now), ms, s), 'stale')
        self.assertEqual(freshness(incoming(-900, 0, now=now), ms, s), 'stale')  # queued on phone
        self.assertEqual(freshness(incoming(0, -900, now=now), ms, s), 'stale')  # server backlog
        self.assertEqual(freshness(incoming(-8 * 86400, now=now), ms, s), 'ancient')
        self.assertEqual(freshness(incoming(-31, expires=30, now=now), ms, s), 'expired')
        self.assertEqual(freshness(incoming(-29, expires=30, now=now), ms, s), 'fresh')


class PolicyTests(unittest.TestCase):
    def test_load_permissions(self):
        self.assertEqual(load_permissions(['# strict', '', 'deny *', 'review light.*', '  allow light.flur ',
                                           'deny *.turn_off', 'deny script.x?']),
                         [('deny', '*'), ('review', 'light.*'), ('allow', 'light.flur'), ('deny', '*.turn_off'),
                          ('deny', 'script.x?')])
        for bad in (['lock.*'], ['allow lock'], ['permit light.*'], ['allow light.*; rm'], ['allow Light.*'],
                    [1], 'deny *', ['deny *'] * 201):
            with self.assertRaises(ValueError, msg=bad):
                load_permissions(bad)

    def test_last_matching_rule_wins(self):
        rules = load_permissions(['deny *', 'review light.*', 'allow light.flur', 'deny *.turn_off'])
        self.assertEqual(permission(rules, 'light.turn_on', 'light.flur'), ('allow', 'allow light.flur'))
        self.assertEqual(permission(rules, 'light.turn_on', 'light.kitchen')[0], 'review')
        self.assertEqual(permission(rules, 'light.turn_off', 'light.flur'), ('deny', 'deny *.turn_off'))
        self.assertEqual(permission(rules, 'switch.turn_on', 'switch.kettle'), ('deny', 'deny *'))
        self.assertEqual(permission([], 'switch.turn_on', 'switch.kettle'), ('review', None))  # default

    def test_validate_action_fixed_limits(self):
        good = {'service': 'light.turn_on', 'entity_id': 'light.kitchen',
                'data': {'brightness_pct': 40, 'color_name': 'warm white', 'flash': False, 'rgb_color': [255, 0, 0]}}
        self.assertEqual(validate_action(good, []),
                         ('light.turn_on', {'entity_id': 'light.kitchen', 'brightness_pct': 40,
                                            'color_name': 'warm white', 'flash': False, 'rgb_color': [255, 0, 0]}))
        self.assertEqual(validate_action({**good, 'data': {}}, [])[1], {'entity_id': 'light.kitchen'})
        heater = {'service': 'water_heater.set_operation_mode', 'entity_id': 'water_heater.dhw',
                  'data': {'operation_mode': 'Manual'}, 'at': None}
        self.assertEqual(validate_action(heater, [])[0], 'water_heater.set_operation_mode')
        for bad in ({**good, 'data': {'entity_id': 'light.other'}}, {**good, 'data': {'area_id': 'kitchen'}},
                    {**good, 'data': {'target': {'area_id': 'x'}}}, {**good, 'data': {'x': {'nested': 1}}},
                    {**good, 'data': {'x': 'y' * 201}}, {**good, 'data': {'x': float('nan')}},
                    {**good, 'data': {'Bad-Key': 1}}, {**good, 'data': {f'k{i}': 1 for i in range(13)}},
                    {**good, 'data': 'x'}, {**good, 'entity_id': 'switch.kettle'},           # wrong domain
                    {**good, 'entity_id': 'light.x; drop'}, {**good, 'service': 'homeassistant.turn_on'},
                    {**good, 'service': 'lock.unlock', 'entity_id': 'lock.front_door'},     # never writable
                    {**good, 'service': 'alarm_control_panel.alarm_disarm', 'entity_id': 'alarm_control_panel.x'},
                    {**good, 'service': 'notify.notify', 'entity_id': 'notify.x'},
                    {**good, 'service': 'automation.turn_off', 'entity_id': 'automation.x'},
                    {**good, 'extra': 1}, 'x', None):
            with self.assertRaises(Denied, msg=bad):
                validate_action(bad, [])
        self.assertEqual(describe('light.turn_on', {'entity_id': 'light.k', 'brightness_pct': 40}),
                         'light.turn_on on light.k (brightness_pct=40)')

    def test_denied_by_permissions(self):
        cover = {'service': 'cover.open_cover', 'entity_id': 'cover.garage_door', 'data': {}}
        for lines in (['deny cover.garage*'], ['deny cover.open_*'], ['deny *.open_cover'], ['deny *'],
                      ['allow cover.*', 'deny cover.garage_door']):
            with self.assertRaises(Denied, msg=lines):
                validate_action(cover, load_permissions(lines))
        for lines in ([], ['deny cover.kitchen*', 'deny light.*'], ['deny *', 'allow cover.garage_door'],
                      ['deny *', 'review cover.*']):
            self.assertEqual(validate_action(cover, load_permissions(lines))[0], 'cover.open_cover', lines)

    def test_schema_has_no_draft_reference_and_blocks_actions_when_read_only(self):
        read_only = build_schema(False)
        self.assertNotIn('$schema', read_only)
        self.assertEqual(read_only['properties']['actions']['maxItems'], 0)
        self.assertEqual(build_schema(True)['properties']['actions']['maxItems'], 3)

    def test_verdict_parsing_defaults_to_ask(self):
        def envelope(obj):
            return json.dumps({'type': 'result', 'is_error': False, 'structured_output': obj}).encode()
        self.assertEqual(parse_verdict(envelope({'decision': 'allow', 'reason': 'Licht,\n harmlos.'})),
                         ('allow', 'Licht, harmlos.'))
        self.assertEqual(parse_verdict(envelope({'decision': 'maybe', 'reason': 'x'}))[0], 'ask')
        self.assertEqual(parse_verdict(envelope({'decision': 'allow'}))[0], 'ask')


class HouseholdTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        house = Household()
        house.validate()
        self.assertFalse(house.allow_writes)
        self.assertTrue(house.require_verified_identity)
        self.assertFalse(house.auto_approve)
        self.assertEqual(house.permissions, [])

    def test_validation(self):
        base = Household(users={'malte': USER1})
        base.validate()
        for change in ({'users': {'Malte': USER1}}, {'users': {'a': 'not-an-aci'}}, {'users': {'a': USER1, 'b': USER1}},
                       {'bot_aci': USER1}, {'groups': {'malte': 'Z3JvdXBpZGlk'}}, {'groups': {'a': 'x'}},
                       {'signal_account': '015112345678'}, {'bot_name': 'Home Bot!'},
                       {'permissions': ['deny lock']}, {'permissions': 'deny *'}, {'rules': 'x' * 4001},
                       {'command_max_age': 5}, {'history_days': 99}, {'notes': 'x' * 4001}, {'model': 'gpt'}):
            with self.assertRaises(HouseholdError, msg=change):
                base.change(**change)

    def test_store_roundtrip_and_types(self):
        with tempfile.TemporaryDirectory() as d:
            store = HouseholdStore(str(Path(d) / 'sub' / 'settings.json'))
            self.assertEqual(store.load(), Household())
            house = Household(signal_account='+4915100000000', users={'malte': USER1}, allow_writes=True,
                              permissions=['deny *', 'review light.*', 'deny cover.garage*'],
                              rules='Lights: anyone.', auto_approve=True)
            store.save(house)
            self.assertEqual(store.load(), house)
            # Settings from before 0.10 still load; a non-empty action table becomes an active
            # allowlist of its entities, so the scope never grows on update.
            old = {k: v for k, v in house.to_json().items() if k != 'permissions'}
            store.path.write_text(json.dumps({**old, 'actions': [
                {'service': 'light.turn_on', 'entities': ['light.*'], 'senders': ['malte']},
                {'service': 'water_heater.set_operation_mode', 'entities': ['water_heater.dhw', 'light.*']}]}))
            self.assertEqual(store.load().permissions, ['deny *', 'review light.*', 'review water_heater.dhw'])
            store.path.write_text(json.dumps({**old, 'actions': []}))
            self.assertEqual(store.load().permissions, [])
            # 0.10's separate lists become ordered lines with the same effect.
            store.path.write_text(json.dumps({**old, 'allowlist_on': True, 'allow': ['light.*'],
                                              'deny': ['light.garden']}))
            self.assertEqual(store.load().permissions, ['deny *', 'review light.*', 'deny light.garden'])
            self.assertEqual(oct(store.path.stat().st_mode & 0o777), '0o600')
            for bad in ({'allow_writes': 'yes'}, {'surprise': 1}, {'command_max_age': True}, []):
                store.path.write_text(json.dumps(bad if isinstance(bad, list) else {**house.to_json(), **bad}))
                with self.assertRaises(HouseholdError, msg=bad):
                    store.load()


class ClaudeOutputTests(unittest.TestCase):
    def test_structured_output(self):
        good = {'type': 'result', 'is_error': False, 'structured_output': {'reply': 'Hello', 'actions': []}}
        self.assertEqual(parse_claude_output(json.dumps(good).encode())['reply'], 'Hello')
        for invalid in (
            {'result': '{"reply":"Hello","actions":[]}'},
            {'structured_output': {'reply': 3, 'actions': []}},
            {'structured_output': {'reply': 'Hi', 'actions': ['unsafe']}},
            {'structured_output': {'reply': 'Hi', 'actions': [], 'extra': True}},
        ):
            with self.assertRaises(ClaudeError) as ctx:
                parse_claude_output(json.dumps(invalid).encode())
            self.assertEqual(ctx.exception.kind, 'invalid')

    def test_error_classification(self):
        # Shapes observed from Claude Code 2.1.306.
        cases = [
            ({'is_error': True, 'api_error_status': 401,
              'result': 'Failed to authenticate. API Error: 401 OAuth access token is invalid.'}, 1, 'auth'),
            ({'is_error': True, 'api_error_status': 429, 'result': 'Rate limited'}, 1, 'limit'),
            ({'is_error': True, 'api_error_status': None, 'result': 'Claude usage limit reached'}, 1, 'limit'),
            ({'is_error': True, 'api_error_status': 529, 'result': 'Overloaded'}, 1, 'unavailable'),
            ({'is_error': True, 'subtype': 'error_max_turns'}, 1, 'invalid'),
        ]
        for envelope, rc, kind in cases:
            with self.assertRaises(ClaudeError) as ctx:
                parse_claude_output(json.dumps(envelope).encode(), rc)
            self.assertEqual(ctx.exception.kind, kind, envelope)
        with self.assertRaises(ClaudeError) as ctx:
            parse_claude_output(b'Error: --json-schema is not a valid JSON Schema', 1)
        self.assertEqual(ctx.exception.kind, 'unavailable')


class OverviewTests(unittest.TestCase):
    def test_render_and_fixed_args(self):
        args = ha_mcp_overview_args(['light'])
        self.assertNotIn('system_info', args['fields'])
        self.assertFalse(args['include_notifications'])
        text = render_overview(json.dumps({'success': True, 'read_only_mode': True, 'domain_stats': {'light': {
            'count': 2, 'states_summary': {'on': 1, 'off': 1}, 'truncated': True,
            'entities': [{'entity_id': 'light.a', 'friendly_name': 'A', 'state': 'on', 'attributes': 'x'}]}},
            'pagination': {'has_more': True}}))
        self.assertEqual(text.splitlines(), ['## light (2: on 1, off 1)', 'light.a (A): on',
                                             '(more entities not shown)', '(entity list truncated)'])
        for bad in ('nope', '[]', '{"success": false}', '{"success": true, "read_only_mode": true}',
                    '{"success": true, "read_only_mode": true, "domain_stats": {"light": 5}}',
                    '{"success": true, "domain_stats": {}}',  # admin (not read-only) ha-mcp
                    '{"success": true, "read_only_mode": "yes", "domain_stats": {}}'):
            with self.assertRaises(ValueError):
                render_overview(bad)


def run_script() -> str:
    """run.sh from the repository, or /run.sh inside the image."""
    for path in (Path(__file__).parent.parent / 'run.sh', Path('/run.sh')):
        if path.exists():
            return path.read_text()
    raise FileNotFoundError('run.sh')


class StartupScriptTests(unittest.TestCase):
    def test_signal_daemon_never_logs_messages(self):
        script = run_script()
        daemon_lines = [line for line in script.splitlines() if ' daemon ' in line and 'signal-cli' in line]
        self.assertTrue(daemon_lines)
        for line in daemon_lines:
            self.assertIn('--no-receive-stdout', line)

    def test_timezone_reaches_bot_and_claude(self):
        script = run_script()
        for marker in ('python3 -m bot.claude_broker', 'python3 -m bot.server'):
            block = script[:script.index(marker)].rsplit('run_as ', 1)[1]
            self.assertIn('TZ="$TZ"', block, marker)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self._dir.name) / 'db.sqlite'))
        self.now = int(time.time())

    def tearDown(self):
        self.db.db.close()
        self._dir.cleanup()

    def test_dedup_retention_and_conversation_isolation(self):
        m = Incoming(USER1, 'group:' + GROUP, True, self.now * 1000, 'The heat was cold')
        self.assertTrue(self.db.add(m))
        self.assertFalse(self.db.add(m))
        self.assertIn('user1: The heat was cold', self.db.context('group:' + GROUP, self.now, {USER1: 'user1'}))
        self.assertEqual(self.db.context('dm:' + USER1, self.now), '')
        self.assertEqual(self.db.context('group:' + GROUP, self.now + 8 * 86400), '')
        short = Incoming(USER1, 'dm:' + USER1, False, self.now * 1000, 'vanishing', expires_in_seconds=60)
        self.db.add(short)
        self.assertIn('vanishing', self.db.context('dm:' + USER1, self.now + 59))
        self.assertNotIn('vanishing', self.db.context('dm:' + USER1, self.now + 61))

    def test_find_approval_by_prompt_timestamp(self):
        data = {'entity_id': 'light.test_lamp'}
        self.db.add_approval('one', USER1, 'dm:' + USER1, 'light.turn_on', data, self.now, 'Shall I 1')
        self.assertEqual(self.db.find_approval(USER1, self.now), 'one')
        self.assertIsNone(self.db.find_approval(USER2, self.now))
        [(item, *_)] = self.db.due_outbox(self.now)
        self.db.outbox_sent(item, 5000)  # the Signal timestamp of the question
        self.db.add_approval('two', USER1, 'dm:' + USER1, 'light.turn_off', data, self.now, 'Shall I 2')
        self.assertEqual(self.db.find_approval(USER1, self.now), Store.PROMPT_AMBIGUOUS)
        self.assertEqual(self.db.find_approval(USER1, self.now, prompt_ts=5000), 'one')
        self.assertIsNone(self.db.find_approval(USER1, self.now, prompt_ts=4999))
        self.assertIsNone(self.db.find_approval(USER1, self.now + APPROVAL_TTL))
        self.assertTrue(self.db.seen('dm:x', USER1, 1, self.now))
        self.assertFalse(self.db.seen('dm:x', USER1, 1, self.now))

    def test_approval_bound_single_use_and_expiring(self):
        data = {'entity_id': 'light.test_lamp'}
        self.db.add_approval('code123', USER1, 'group:' + GROUP, 'light.turn_on', data, self.now, 'prompt')
        self.assertIsNone(self.db.consume_approval('code123', USER2, self.now))
        action_id, conv, service, got, run_at = self.db.consume_approval('code123', USER1, self.now)
        self.assertIsNone(run_at)
        self.assertEqual((conv, service, got), ('group:' + GROUP, 'light.turn_on', data))
        self.assertEqual(self.db.action_status(action_id), 'executing')
        self.assertIsNone(self.db.consume_approval('code123', USER1, self.now))
        self.db.add_approval('late456', USER1, 'dm:' + USER1, 'light.turn_on', data, self.now, 'prompt')
        self.assertIsNone(self.db.consume_approval('late456', USER1, self.now + APPROVAL_TTL))
        self.db.add_approval('cncl789', USER1, 'dm:' + USER1, 'light.turn_on', data, self.now, 'prompt')
        self.assertFalse(self.db.cancel_approval('cncl789', USER2))
        self.assertTrue(self.db.cancel_approval('cncl789', USER1))
        self.assertIsNone(self.db.consume_approval('cncl789', USER1, self.now))
        # The approval prompt is queued in the same transaction, to the requester's DM.
        self.assertEqual([d for _, d, *_ in self.db.due_outbox(self.now)], ['dm:' + USER1] * 3)

    def test_recover_marks_unknown_and_never_retries(self):
        self.db.add_approval('code123', USER1, 'dm:' + USER1, 'light.turn_on',
                             {'entity_id': 'light.test_lamp'}, self.now, 'prompt')
        action_id = self.db.consume_approval('code123', USER1, self.now)[0]
        msg = Incoming(USER2, 'dm:' + USER2, False, self.now * 1000, 'hello')
        self.assertTrue(self.db.add_with_job(msg, self.now))
        self.assertFalse(self.db.add_with_job(msg, self.now))
        self.assertEqual(self.db.recover(self.now), (1, 1))
        self.assertEqual(self.db.action_status(action_id), 'unknown')
        self.assertEqual(self.db.recover(self.now), (0, 0))
        texts = [t for _, _, t, *_ in self.db.due_outbox(self.now)]
        self.assertTrue(any('UNKNOWN and it was NOT retried' in t for t in texts))
        self.assertTrue(any('restarted while handling' in t for t in texts))

    def test_outbox_backoff_and_dead_letter(self):
        self.db.enqueue([('dm:' + USER1, 'hi')], self.now)
        [(item, _, _, attempts, created, _, _)] = self.db.due_outbox(self.now)
        self.assertEqual(self.db.outbox_failed(item, attempts, created, 'E', self.now), 'pending')
        self.assertEqual(self.db.due_outbox(self.now), [])
        self.assertEqual(len(self.db.due_outbox(self.now + 5)), 1)
        self.assertEqual(self.db.outbox_failed(item, 1, created, 'E', self.now + 4000), 'dead')
        self.assertEqual(self.db.outbox_status(), {'dead': 1})

    def test_notify_idempotency(self):
        self.assertTrue(self.db.notify('evt-1', ['group:' + GROUP], 'x', self.now))
        self.assertFalse(self.db.notify('evt-1', ['group:' + GROUP], 'x', self.now))
        self.assertTrue(self.db.notify(None, ['group:' + GROUP], 'x', self.now))
        self.assertEqual(len(self.db.due_outbox(self.now)), 2)

    def test_remote_delete_and_purge(self):
        m = Incoming(USER1, 'dm:' + USER1, False, self.now * 1000, 'oops')
        self.db.add(m)
        self.assertTrue(self.db.remote_delete(RemoteDelete(USER1, 'dm:' + USER1, m.timestamp)))
        self.assertFalse(self.db.has_message('dm:' + USER1, USER1, m.timestamp))
        self.db.enqueue([('dm:' + USER1, 'hi')], self.now)
        self.db.outbox_sent(self.db.due_outbox(self.now)[0][0])
        self.db.purge(self.now + 2 * 86400)
        self.assertEqual(self.db.outbox_status(), {})

    def test_prototype_database_is_migrated(self):
        path = str(Path(self._dir.name) / 'old.sqlite')
        import sqlite3
        old = sqlite3.connect(path)
        old.execute('CREATE TABLE approvals(code TEXT PRIMARY KEY, sender TEXT, destination TEXT, '
                    'action_json TEXT, expires_at INTEGER)')
        old.execute("INSERT INTO approvals VALUES('c','s','d','{}',9999999999)")
        old.commit()
        old.close()
        store = Store(path)
        self.assertIsNone(store.consume_approval('c', 's', self.now))
        store.db.close()


if __name__ == '__main__':
    unittest.main()


class NextRunTests(unittest.TestCase):
    def test_repeats_keep_local_time(self):
        import time
        from bot.core import next_run
        friday_7 = time.mktime((2026, 10, 23, 7, 0, 0, 0, 0, -1))
        fmt = lambda t: time.strftime('%a %H:%M', time.localtime(t))
        self.assertEqual(fmt(next_run(friday_7, 'daily', friday_7)), 'Sat 07:00')
        self.assertEqual(fmt(next_run(friday_7, 'weekdays', friday_7)), 'Mon 07:00')
        self.assertEqual(next_run(friday_7, 'weekly', friday_7) - friday_7 in (7 * 86400, 7 * 86400 + 3600), True)
        self.assertIsNone(next_run(friday_7, 'once', friday_7))
        # Missed runs are skipped, not caught up (Monday 08:00, after the DST change on Sunday).
        monday_8 = time.mktime((2026, 10, 26, 8, 0, 0, 0, 0, -1))
        self.assertEqual(fmt(next_run(friday_7, 'daily', monday_8)), 'Tue 07:00')



class TriggerTests(unittest.TestCase):
    @staticmethod
    def st(value, **attributes):
        return {'state': value, 'attributes': attributes}

    def test_parse_trigger(self):
        from bot.core import parse_trigger
        self.assertEqual(parse_trigger({'entity_id': 'sensor.p', 'below': 5, 'for_minutes': 0, 'to': None}),
                         {'entity_id': 'sensor.p', 'below': 5})
        self.assertEqual(parse_trigger({'entity_id': 'person.a', 'to': 'home'})['to'], 'home')
        for bad in ({'entity_id': 'sensor.p'}, {'to': 'on'}, {'entity_id': 'x', 'to': 'on'},
                    {'entity_id': 'sensor.p', 'above': True}, {'entity_id': 'sensor.p', 'to': ['on']},
                    {'entity_id': 'sensor.p', 'to': 'on', 'for_minutes': 5000},
                    {'entity_id': 'sensor.p', 'to': 'on', 'attribute': 'a b'},
                    {'entity_id': 'sensor.p', 'to': 'on', 'service': 'lock.unlock'}, 'sensor.p'):
            with self.assertRaises(Denied, msg=bad):
                parse_trigger(bad)

    def test_fires_on_transition_only(self):
        from bot.core import trigger_fires
        to_home = {'entity_id': 'person.a', 'to': 'home'}
        self.assertTrue(trigger_fires(to_home, self.st('not_home'), self.st('home')))
        self.assertFalse(trigger_fires(to_home, self.st('home'), self.st('home', gps=1)))  # attribute update
        self.assertFalse(trigger_fires(to_home, self.st('unavailable'), self.st('home')))
        self.assertFalse(trigger_fires(to_home, None, self.st('home')))
        below = {'entity_id': 'sensor.p', 'below': 5}
        self.assertTrue(trigger_fires(below, self.st('120'), self.st('2')))
        self.assertFalse(trigger_fires(below, self.st('3'), self.st('2')))
        self.assertFalse(trigger_fires(below, self.st('120'), self.st('abc')))
        leave = {'entity_id': 'binary_sensor.door', 'from': 'on'}
        self.assertTrue(trigger_fires(leave, self.st('on'), self.st('off')))
        self.assertFalse(trigger_fires(leave, self.st('off'), self.st('on')))
        attr = {'entity_id': 'climate.x', 'attribute': 'current_temperature', 'above': 22}
        self.assertTrue(trigger_fires(attr, self.st('heat', current_temperature=21.5),
                                      self.st('heat', current_temperature=22.5)))

    def test_describe(self):
        from bot.core import describe_trigger, TASK
        self.assertEqual(describe_trigger({'entity_id': 'sensor.p', 'below': 5, 'for_minutes': 3}),
                         'sensor.p is below 5 for 3 min')
        self.assertEqual(describe_trigger({'entity_id': 'cover.x', 'from': 'open', 'to': 'closed'}),
                         'cover.x changes from "open" to "closed"')
        self.assertEqual(describe(TASK, {'prompt': 'p', 'repeat': 'always',
                                         'trigger': {'entity_id': 'person.a', 'to': 'home'}}),
                         'task "p" when person.a becomes "home", every time')

    def test_store_watching_and_rearm(self):
        from bot.core import TASK, TRIGGER_COOLDOWN
        with tempfile.TemporaryDirectory() as d:
            store = Store(str(Path(d) / 'db.sqlite3'))
            data = {'prompt': 'p', 'repeat': 'always', 'trigger': {'entity_id': 'person.a', 'to': 'home'}}
            job = store.add_scheduled(USER1, f'dm:{USER1}', TASK, data, None, 1000)
            store.add_scheduled(USER1, f'dm:{USER1}', TASK, {'prompt': 'q', 'repeat': 'once'}, 5000, 1000)
            self.assertEqual([j[0] for j in store.watching()], [job])
            self.assertEqual(store.due_scheduled(10 ** 9)[0][4]['prompt'], 'q')  # triggers are never "due"
            self.assertTrue(store.start_scheduled(job))
            store.finish_action(job, 'succeeded', 'ran', [], 2000)
            nxt = store.repeat_task(job, 2000)
            [(i, *_, d2, run_at)] = store.watching()
            self.assertEqual((i, run_at, d2['not_before']), (nxt, None, 2000 + TRIGGER_COOLDOWN))
            # A one-off trigger is not re-armed.
            once = store.add_scheduled(USER1, f'dm:{USER1}', TASK, {**data, 'repeat': 'once'}, None, 1000)
            store.start_scheduled(once)
            self.assertIsNone(store.repeat_task(once, 2000))
            store.db.close()

    def test_confirmed_trigger_task_waits(self):
        from bot.core import TASK
        with tempfile.TemporaryDirectory() as d:
            store = Store(str(Path(d) / 'db.sqlite3'))
            data = {'prompt': 'p', 'repeat': 'once', 'trigger': {'entity_id': 'person.a', 'to': 'home'},
                    'grants': [{'service': 'light.turn_on', 'entity_id': 'light.x'}]}
            store.add_approval('code', USER1, f'dm:{USER1}', TASK, data, 1000, 'Shall I?')
            action_id, *_ = store.consume_approval('code', USER1, 1001)
            self.assertEqual(store.action_status(action_id), 'scheduled')
            self.assertEqual([j[0] for j in store.watching()], [action_id])
            store.db.close()

    def test_schema_allows_triggers(self):
        task = build_schema(True)['properties']['tasks']['items']
        self.assertIn('when', task['required'])
        self.assertIn('always', task['properties']['repeat']['enum'])


class PhotoParseTests(unittest.TestCase):
    def test_photos_are_parsed_and_marked(self):
        att = lambda i, t='image/jpeg', size=10: {'id': i, 'contentType': t, 'size': size, 'isVoiceNote': False}
        msg = parse_signal_event(signal_event(text='was ist das?', attachments=[att('a.jpg'), att('b.pdf', 'application/pdf')]),
                                 BOT, USERS, {GROUP})
        self.assertEqual((msg.text, msg.images), ('was ist das? [photo]', ('a.jpg',)))
        msg = parse_signal_event(signal_event(text=None, attachments=[att('a.jpg'), att('b.png', 'image/png')]),
                                 BOT, USERS, {GROUP})
        self.assertEqual((msg.text, msg.images), ('[2 photos]', ('a.jpg', 'b.png')))
        for attachments in ([att('../etc/passwd')], [att('x.jpg', size=50 << 20)], [att('v.m4a', 'audio/aac')]):
            self.assertIsNone(parse_signal_event(signal_event(text=None, attachments=attachments), BOT, USERS, {GROUP}))
        self.assertIsNone(parse_signal_event(signal_event(text='x', viewOnce=True, attachments=[att('a.jpg')]),
                                             BOT, USERS, {GROUP}))

    def test_image_type_from_bytes(self):
        from bot.core import image_type
        self.assertEqual(image_type(b'\xff\xd8\xff\xe0rest'), 'image/jpeg')
        self.assertEqual(image_type(b'RIFF1234WEBPVP8 '), 'image/webp')
        self.assertIsNone(image_type(b'<svg xmlns="http://www.w3.org/2000/svg"/>'))


class ChartUnitTests(unittest.TestCase):
    def test_parse_and_render(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest('matplotlib not installed')
        from datetime import datetime, timedelta
        from bot.charts import statistics_series, history_series, render, check_spec, period_for, ChartError
        now = int(time.time() * 1000)
        stats = json.dumps({'data': {'entities': [
            {'entity_id': 'sensor.p', 'statistics': [{'start': now - 600000, 'end': now - 300000, 'mean': 1.0,
                                                       'min': 0.5, 'max': 2.0}]},
            {'entity_id': 'sensor.e', 'statistics': [{'start': now - 3600000, 'end': now, 'change': 0.4, 'sum': 9}]},
            {'entity_id': 'sensor.none', 'statistics': []}]}})
        found = statistics_series(stats, {'sensor.p': {'label': 'P', 'unit': 'kW'}})
        self.assertEqual({k: v.kind for k, v in found.items()}, {'sensor.p': 'band', 'sensor.e': 'bars'})
        hist = json.dumps({'data': {'entities': [
            {'entity_id': 'binary_sensor.w', 'states': [{'state': 'on', 'last_changed': '2026-10-23T10:00:00+02:00'},
                                                        {'state': 'unavailable', 'last_changed': '2026-10-23T11:00:00+02:00'}]},
            {'entity_id': 'sensor.t', 'states': [{'state': '21.5', 'last_changed': '2026-10-23T10:00:00+02:00'},
                                                 {'state': 'x', 'last_changed': '2026-10-23T11:00:00+02:00'}]}]}})
        found.update(history_series(hist, {}))
        self.assertEqual((found['binary_sensor.w'].kind, found['binary_sensor.w'].y), ('binary', [1.0]))
        self.assertEqual((found['sensor.t'].kind, found['sensor.t'].y), ('step', [21.5]))
        end = datetime.now().astimezone()
        png = render('t', list(found.values()), end - timedelta(hours=30), end)
        self.assertTrue(png.startswith(b'\x89PNG'))
        with self.assertRaises(ChartError):
            render('t', [], end, end)
        self.assertEqual([period_for(h) for h in (24, 72, 73, 1080, 1081)], ['5minute', '5minute', 'hour', 'hour', 'day'])
        self.assertEqual(check_spec({'title': 'a', 'entity_ids': ['sensor.p', 'sensor.p'], 'hours': 2}),
                         ('a', ['sensor.p'], 2))
        for bad in ({'entity_ids': [], 'hours': 1}, {'entity_ids': ['sensor.p'], 'hours': True},
                    {'entity_ids': ['sensor.p'] * 7, 'hours': 1}, {'entity_ids': ['sensor.p'], 'hours': 1, 'x': 1}):
            with self.assertRaises(ChartError):
                check_spec(bad)
