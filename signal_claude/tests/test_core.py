"""Unit tests for bot/core.py. Standard library only (no aiohttp/mcp needed)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from bot.core import (Store, Incoming, Reaction, RemoteDelete, Settings, Denied, ClaudeError, addressed,
                      freshness, parse_signal_event, load_policy, permitted, validate_action, describe,
                      build_schema, parse_claude_output, render_overview, ha_mcp_overview_args,
                      identity_trusted, group_roster_problem, APPROVAL_TTL)
from bot.household import Household, HouseholdError, HouseholdStore

BOT = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
USER1 = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
USER2 = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
GROUP = 'abc=/base64'
USERS = {USER1, USER2}
POLICY = load_policy({'actions': [
    {'service': 'light.turn_on', 'entities': ['light.*'], 'senders': ['user1', 'user2'], 'contexts': ['dm', 'group'],
     'fields': {'brightness_pct': {'min': 1, 'max': 100}, 'color_name': {'values': ['red', 'warm white']},
                'flash': {'type': 'boolean'}}},
    {'service': 'climate.set_temperature', 'entities': ['climate.test_room'], 'senders': ['user1'],
     'contexts': ['dm'], 'fields': {'temperature': {'min': 18, 'max': 23}}},
]})


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
    def test_load_policy_fails_closed(self):
        ok = {'service': 'light.turn_on', 'entities': ['light.x']}
        bad = [
            {'actions': [{**ok, 'service': 'lock.unlock', 'entities': ['lock.front_door']}]},   # blocked domain
            {'actions': [{**ok, 'service': 'alarm_control_panel.alarm_disarm', 'entities': []}]},
            {'actions': [{**ok, 'service': 'notify.mobile_app', 'entities': []}]},
            {'actions': [{**ok, 'service': 'homeassistant.restart', 'entities': []}]},
            {'actions': [{**ok, 'entities': ['lock.front_door']}]},                            # wrong domain
            {'actions': [{**ok, 'entities': ['*']}]},
            {'actions': [{**ok, 'confirmation': False}]},                                      # unknown key
            {'actions': [{**ok, 'senders': ['Not A Name']}]},
            {'actions': [{**ok, 'contexts': ['everywhere']}]},
            {'actions': [{**ok, 'contexts': []}]},
            {'actions': [{**ok, 'fields': {'entity_id': {'values': ['light.y']}}}]},           # can't widen
            {'actions': [{**ok, 'fields': {'x': {'min': 5, 'max': 1}}}]},
            {'actions': [{**ok, 'fields': {'x': {'values': []}}}]},
            {'actions': [{**ok, 'fields': {'x': {'regex': '.*'}}}]},
            {'actions': [{**ok, 'fields': {'x': {'min': True, 'max': 2}}}]},
            {'actions': {'light_on': {}}},                                                      # old format
            {'writes_enabled': True, 'actions': []},
            [],
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw):
                load_policy(raw)
        self.assertEqual(load_policy({'actions': [ok]})[0]['contexts'], ['dm'])  # default: DM only

    def test_permitted_by_switch_user_and_context(self):
        self.assertEqual(permitted(POLICY, False, 'user1', 'dm'), [])
        self.assertEqual(permitted(POLICY, True, None, 'dm'), [])
        self.assertEqual([r['service'] for r in permitted(POLICY, True, 'user1', 'dm')],
                         ['light.turn_on', 'climate.set_temperature'])
        self.assertEqual([r['service'] for r in permitted(POLICY, True, 'user2', 'dm')], ['light.turn_on'])
        self.assertEqual([r['service'] for r in permitted(POLICY, True, 'user1', 'group')], ['light.turn_on'])
        self.assertEqual(permitted(load_policy({'actions': [{'service': 'light.turn_on', 'entities': [],
                                                             'senders': ['user1']}]}), True, 'user1', 'dm'), [])

    def test_validate_action(self):
        rules = permitted(POLICY, True, 'user1', 'dm')
        good = {'service': 'light.turn_on', 'entity_id': 'light.kitchen',
                'data': {'brightness_pct': 40, 'color_name': 'warm white', 'flash': False}}
        self.assertEqual(validate_action(good, rules),
                         ('light.turn_on', {'entity_id': 'light.kitchen', 'brightness_pct': 40,
                                            'color_name': 'warm white', 'flash': False}))
        self.assertEqual(validate_action({**good, 'data': {}}, rules)[1], {'entity_id': 'light.kitchen'})
        temp = {'service': 'climate.set_temperature', 'entity_id': 'climate.test_room', 'data': {'temperature': 21.5}}
        self.assertEqual(validate_action(temp, rules)[0], 'climate.set_temperature')
        for bad in ({**good, 'data': {'brightness_pct': 0}}, {**good, 'data': {'brightness_pct': True}},
                    {**good, 'data': {'brightness_pct': '40'}}, {**good, 'data': {'color_name': 'blue'}},
                    {**good, 'data': {'flash': 1}}, {**good, 'data': {'transition': 5}},
                    {**good, 'data': {'entity_id': 'light.other'}}, {**good, 'data': 'x'},
                    {**good, 'entity_id': 'switch.kettle'}, {**good, 'entity_id': 'light.x; drop'},
                    {**good, 'service': 'light.toggle'}, {**good, 'service': 'lock.unlock', 'entity_id': 'lock.x'},
                    {**temp, 'data': {'temperature': 29}}, {**temp, 'entity_id': 'climate.other'},
                    {**good, 'extra': 1}, 'x', None):
            with self.assertRaises(Denied, msg=bad):
                validate_action(bad, rules)
        with self.assertRaises(Denied):
            validate_action(temp, permitted(POLICY, True, 'user2', 'dm'))
        self.assertEqual(describe('light.turn_on', {'entity_id': 'light.k', 'brightness_pct': 40}),
                         'light.turn_on on light.k (brightness_pct=40)')

    def test_schema_has_no_draft_reference_and_blocks_actions_when_read_only(self):
        read_only = build_schema([])
        self.assertNotIn('$schema', read_only)
        self.assertEqual(read_only['properties']['actions']['maxItems'], 0)
        rw = build_schema(permitted(POLICY, True, 'user1', 'dm'))
        self.assertEqual(rw['properties']['actions']['items']['properties']['service']['enum'],
                         ['climate.set_temperature', 'light.turn_on'])


class HouseholdTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        house = Household()
        house.validate()
        self.assertFalse(house.allow_writes)
        self.assertTrue(house.require_verified_identity)
        self.assertEqual(house.policy, [])

    def test_validation(self):
        base = Household(users={'malte': USER1})
        base.validate()
        for change in ({'users': {'Malte': USER1}}, {'users': {'a': 'not-an-aci'}}, {'users': {'a': USER1, 'b': USER1}},
                       {'bot_aci': USER1}, {'groups': {'malte': 'Z3JvdXBpZGlk'}}, {'groups': {'a': 'x'}},
                       {'signal_account': '015112345678'}, {'bot_name': 'Home Bot!'},
                       {'actions': [{'service': 'light.turn_on', 'entities': ['light.x'], 'senders': ['anna']}]},
                       {'actions': [{'service': 'lock.unlock', 'entities': ['lock.x']}]},
                       {'command_max_age': 5}, {'history_days': 99}):
            with self.assertRaises(HouseholdError, msg=change):
                base.change(**change)

    def test_store_roundtrip_and_types(self):
        with tempfile.TemporaryDirectory() as d:
            store = HouseholdStore(str(Path(d) / 'sub' / 'settings.json'))
            self.assertEqual(store.load(), Household())
            house = Household(signal_account='+4915100000000', users={'malte': USER1}, allow_writes=True,
                              actions=[{'service': 'light.turn_on', 'entities': ['light.x'], 'senders': ['malte']}])
            store.save(house)
            self.assertEqual(store.load(), house)
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
        action_id, conv, service, got = self.db.consume_approval('code123', USER1, self.now)
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
        [(item, _, _, attempts, created)] = self.db.due_outbox(self.now)
        self.assertEqual(self.db.outbox_failed(item, attempts, created, 'E', self.now), 'pending')
        self.assertEqual(self.db.due_outbox(self.now), [])
        self.assertEqual(len(self.db.due_outbox(self.now + 5)), 1)
        self.assertEqual(self.db.outbox_failed(item, 1, created, 'E', self.now + 4000), 'dead')
        self.assertEqual(self.db.outbox_status(), {'dead': 1})

    def test_notify_idempotency(self):
        self.assertTrue(self.db.notify('evt-1', 'group:' + GROUP, 'x', self.now))
        self.assertFalse(self.db.notify('evt-1', 'group:' + GROUP, 'x', self.now))
        self.assertTrue(self.db.notify(None, 'group:' + GROUP, 'x', self.now))
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
