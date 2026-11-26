# -*- coding: utf-8 -*-
import json
import time
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo import SUPERUSER_ID, api, fields
from odoo.tests.common import TransactionCase, tagged
import odoo.addons.havsh as havsh
from odoo.addons.havsh.models import havsh_sync
from odoo.addons.havsh.models.havsh_sync import (
    HAVSH_ON_LOAD, HAVSH_ON_SCHEDULE, HAVSH_ON_WRITE, HavShSync,
)

LOGGER = 'odoo.addons.havsh.models.havsh_sync'
SETTLED_STATES = ('installed', 'uninstalled', 'uninstallable')


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = body

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        return self.response


@tagged('post_install', '-at_install')
class TestModuleSync(TransactionCase):
    """hav.sh gets one request with the modules that changed, after the commit,
    and nothing at all when nothing changed.

    The earlier implementation posted one request per module row per ``write()``:
    349 requests and almost a minute for one "Update Apps List" that changed
    nothing. It also did so inside the transaction at first, which aborted module
    installs with "could not serialize access due to concurrent update".
    """

    def setUp(self):
        super().setUp()
        self.sync = self.env['havsh.sync']
        self.posted = []            # the envelopes "delivered" to hav.sh
        self.timeouts = []          # ... and the timeout each request was given
        self.answer = (200, None)   # what hav.sh answers: (status, error)
        self.answers = []           # ... or what it answers to the next requests, one each
        self.token = 'tok'
        self.uuid = 'uid'
        self.real_post = HavShSync._havsh__post

        def fake_post(_model, url, payload, token, session=None, timeout=None):
            self.posted.append(payload)
            self.timeouts.append(timeout)
            return self.answers.pop(0) if self.answers else self.answer

        for patcher in (
            patch.object(havsh_sync, 'HAVSH_SYNC_DURING_TESTS', True),
            patch.object(HavShSync, '_havsh__ensure_auth', side_effect=lambda: (self.token, self.uuid)),
            patch.object(HavShSync, '_havsh__post', autospec=True, side_effect=fake_post),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @contextmanager
    def _registry_test_mode(self):
        """The sync opens cursors of its own; in a test they have to see this transaction."""
        if hasattr(self, 'registry_test_mode'):             # 20.0
            with self.registry_test_mode():
                yield
        elif hasattr(self, 'enter_registry_test_mode'):     # 19.0
            with self.enter_registry_test_mode():
                yield
        else:                                               # 18.0, 17.0
            self.env.flush_all()
            self.registry.enter_test_mode(self.cr)
            try:
                yield
            finally:
                self.registry.leave_test_mode()

    def _sync(self, trigger=HAVSH_ON_LOAD):
        with self._registry_test_mode():
            return havsh_sync._havsh_sync_in_new_cursors(self.registry, trigger)

    def _run_postcommit(self):
        with self._registry_test_mode():
            self.env.cr.postcommit.run()

    def _synced(self):
        """Bring hav.sh up to date, then forget what was posted for that."""
        self._sync()
        self.posted.clear()
        self.timeouts.clear()

    def _sent(self):
        """Technical names of the modules posted so far, in order."""
        return [module['technical_name'] for envelope in self.posted for module in envelope['data']['modules']]

    def _all_modules(self):
        return self.env['ir.module.module'].search([])

    def _settled_modules(self, limit):
        """Modules no operation is pending on: a write may report them."""
        return self.env['ir.module.module'].search([('state', 'in', SETTLED_STATES)], limit=limit)

    def _uninstalled_module(self):
        return self.env['ir.module.module'].search([('state', '=', 'uninstalled')], limit=1)

    def _state(self):
        self.env.cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [havsh_sync.HAVSH_STATE_KEY])
        row = self.env.cr.fetchone()
        return json.loads(row[0]) if row else None

    def _set_state(self, **values):
        state = dict(self._state(), **values)
        self.env.cr.execute(
            "UPDATE ir_config_parameter SET value = %s WHERE key = %s",
            [json.dumps(state), havsh_sync.HAVSH_STATE_KEY],
        )

    def _a_minute_later(self):
        """Nobody tries again within a minute of a failure: let that minute pass."""
        self._set_state(fail_at=(fields.Datetime.now() - timedelta(minutes=2)).isoformat())

    def _retry_at(self):
        return fields.Datetime.to_datetime(self._state()['retry_at'].replace('T', ' '))

    def _requested_trigger(self):
        return (self.env.cr.postcommit.data.get('havsh_sync') or {}).get('trigger')

    def _cron(self):
        """The scheduled action, active and without retries waiting."""
        cron = self.env.ref('havsh.ir_cron_hav_sh_full_sync', raise_if_not_found=False)
        if not cron:
            self.skipTest('the scheduled action was removed from this database')
        cron.active = True
        self._retries(cron).unlink()
        return cron

    def _retries(self, cron):
        return self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)])

    # ---- what is sent

    def test_first_sync_sends_every_module_once(self):
        sent = self._sync()

        names = self._all_modules().mapped('name')
        self.assertEqual(sent, len(names))
        self.assertCountEqual(self._sent(), names)
        self.assertEqual({envelope['event'] for envelope in self.posted}, {'full-sync'})
        self.assertEqual({envelope['uuid'] for envelope in self.posted}, {'uid'})
        self.assertTrue(
            all(envelope['data']['partial'] is True for envelope in self.posted),
            'every request must say it does not list all modules, so hav.sh never takes the others as gone',
        )

    def test_installed_modules_are_sent_first(self):
        self._sync()

        states = [module['state'] for envelope in self.posted for module in envelope['data']['modules']]
        not_installed = [state in ('uninstalled', 'uninstallable') for state in states]
        self.assertEqual(not_installed, sorted(not_installed))

    def test_payload_keeps_the_fields_hav_sh_reads(self):
        self._sync()

        base = next(m for envelope in self.posted for m in envelope['data']['modules'] if m['technical_name'] == 'base')
        self.assertEqual(base['state'], 'installed')
        self.assertTrue(base['module_name'])
        self.assertTrue(base['version'])
        self.assertLessEqual(
            {'technical_name', 'module_name', 'state', 'version', 'author', 'license', 'category', 'application'},
            set(base),
        )

    def test_text_hav_sh_cannot_store_is_cut(self):
        """hav.sh refuses the whole request over one text above 255 characters."""
        self._synced()

        self.env.ref('base.module_base').write({'author': 'x' * 300})
        self._sync()

        self.assertEqual(self.posted[0]['data']['modules'][0]['author'], 'x' * 255)

    def test_module_is_sent_again_for_what_hav_sh_stores_and_nothing_else(self):
        stored = ('technical_name', 'module_name', 'state', 'version', 'author', 'license', 'category', 'application')
        payload = dict.fromkeys(stored, 'a')
        digest = self.sync._havsh__digest

        for key in stored:
            self.assertNotEqual(digest(payload), digest(dict(payload, **{key: 'b'})), key)
        self.assertEqual(digest(payload), digest(dict(payload, updated_at='2026-01-01')))

    def test_nothing_changed_sends_nothing(self):
        self._synced()

        with self.assertNoLogs(LOGGER, 'WARNING'):
            for trigger in (HAVSH_ON_WRITE, HAVSH_ON_LOAD, HAVSH_ON_SCHEDULE):
                self.assertEqual(self._sync(trigger), 0)
        self.assertEqual(self.posted, [], 'a sync without any module change must not call hav.sh')

    def test_rewriting_the_same_values_sends_nothing(self):
        """"Update Apps List" rewrites the unchanged category of half the modules."""
        self._synced()

        for module in self._all_modules()[:50]:
            module.write({'category_id': module.category_id.id, 'state': module.state})
        with self.assertNoLogs(LOGGER, 'WARNING'):
            self._run_postcommit()

        self.assertEqual(self.posted, [])

    def test_only_changed_modules_are_sent_in_one_request(self):
        self._synced()
        changed = self._all_modules()[:3]

        for module in changed:
            module.write({'author': 'Someone Else'})
        self._sync()

        self.assertEqual(len(self.posted), 1, 'changes of several modules belong in one request')
        self.assertCountEqual(self._sent(), changed.mapped('name'))
        self.assertEqual({m['author'] for m in self.posted[0]['data']['modules']}, {'Someone Else'})

    def test_large_sync_is_split_into_batches(self):
        total = len(self._all_modules())

        with patch.object(havsh_sync, 'HAVSH_BATCH_SIZE', 100):
            self._sync()

        self.assertEqual(len(self.posted), -(-total // 100))
        self.assertTrue(all(len(envelope['data']['modules']) <= 100 for envelope in self.posted))
        self.assertCountEqual(self._sent(), self._all_modules().mapped('name'))

    def test_scheduled_sync_sends_every_module_again_after_a_day(self):
        self._synced()
        self._set_state(full_at=(fields.Datetime.now() - timedelta(hours=23, minutes=1)).isoformat())

        self._sync(HAVSH_ON_LOAD)
        self.assertEqual(self.posted, [], 'only the scheduled sync may send everything again')

        self._sync(HAVSH_ON_SCHEDULE)
        self.assertCountEqual(self._sent(), self._all_modules().mapped('name'))

    def test_state_of_another_service_is_ignored(self):
        """A copied database must not believe the copy's hav.sh service already knows its modules."""
        self._synced()

        self.uuid = 'uuid-of-the-copy'
        self._sync()

        self.assertCountEqual(self._sent(), self._all_modules().mapped('name'))

    def test_state_of_another_hav_sh_is_ignored(self):
        self._synced()

        with patch.object(HavShSync, '_havsh__base_url', return_value='https://other.example'):
            self._sync()

        self.assertCountEqual(self._sent(), self._all_modules().mapped('name'))

    def test_state_of_another_database_is_ignored(self):
        """A restored backup brings the state of an earlier time into the same service."""
        self._synced()

        with patch.object(HavShSync, '_havsh__database_identity', return_value='1@2'):
            self._sync()

        self.assertCountEqual(self._sent(), self._all_modules().mapped('name'))

    def test_database_is_identified_by_its_oid(self):
        self.env.cr.execute("SELECT oid FROM pg_database WHERE datname = current_database()")
        oid = str(self.env.cr.fetchone()[0])

        self.assertTrue(self.sync._havsh__database_identity().startswith(oid))

    def test_without_token_nothing_happens(self):
        before = self._state()

        with patch.object(HavShSync, '_havsh__ensure_auth', return_value=(None, None)):
            self.assertEqual(self._sync(HAVSH_ON_SCHEDULE), 0)
            self.env.ref('base.module_base').write({'author': 'Someone Else'})

        self.assertEqual(self.posted, [])
        self.assertEqual(self._state(), before)
        self.assertIsNone(self._requested_trigger())

    def test_uninstall_removes_the_stored_state(self):
        self._synced()
        self.assertTrue(self._state())
        hook = self.env['ir.module.module'].get_module_info('havsh')['uninstall_hook']

        getattr(havsh, hook)(self.env)

        self.assertIsNone(self._state())

    # ---- when it is sent

    def test_write_sends_nothing_before_the_commit(self):
        self._synced()
        module = self.env.ref('base.module_base')

        module.write({'author': 'Someone Else'})
        self.assertEqual(
            self.posted, [],
            'hav.sh was called inside the write transaction; that holds its row locks across the network round-trip',
        )

        self._run_postcommit()
        self.assertEqual(self._sent(), ['base'])

    def test_commit_runs_the_sync_on_cursors_of_its_own(self):
        used = []

        def plan(model, trigger):
            used.append((model.env.cr, trigger))
            return None     # nothing to send: no request, nothing written

        with patch.object(HavShSync, '_havsh__plan', plan):
            with self.registry.cursor() as cr:      # not in test mode: a real cursor and a real commit
                sync = api.Environment(cr, SUPERUSER_ID, {})['havsh.sync']
                sync._havsh_request_sync()
                sync._havsh_request_sync()
                self.assertEqual(used, [], 'nothing may happen before the commit')

        self.assertEqual([trigger for _cr, trigger in used], [HAVSH_ON_WRITE])
        self.assertIsNot(used[0][0], cr)
        self.assertTrue(used[0][0].closed)

    def test_many_writes_in_a_transaction_lead_to_one_sync(self):
        """Every further sync would read all modules again: 349 times for one "Update Apps List"."""
        with patch.object(havsh_sync, '_havsh_sync_in_new_cursors') as run:
            for module in self._all_modules()[:20]:
                module.write({'author': 'Someone Else'})
            self.sync._havsh_request_sync()
            self.env.cr.postcommit.run()

        self.assertEqual(run.call_count, 1)

    def test_changes_of_a_transaction_are_sent_together(self):
        self._synced()
        modules = self._settled_modules(20)

        for module in modules:
            module.write({'author': 'Someone Else'})
        self._run_postcommit()

        self.assertEqual(len(self.posted), 1)
        self.assertCountEqual(self._sent(), modules.mapped('name'))

    def test_new_module_is_sent_after_the_commit(self):
        self._synced()

        self.env['ir.module.module'].create({'name': 'havsh_test_new_module', 'shortdesc': 'New', 'state': 'uninstalled'})
        self._run_postcommit()

        self.assertEqual(self._sent(), ['havsh_test_new_module'])

    def test_write_of_other_fields_requests_no_sync(self):
        module = self.env.ref('base.module_base')

        module.write({'demo': module.demo})

        self.assertIsNone(self._requested_trigger())

    def test_writes_while_modules_are_loaded_request_no_sync(self):
        """An install writes every module it handles; the load ends with the one sync."""
        with patch.object(self.registry, 'ready', False):
            self.env.ref('base.module_base').write({'state': 'installed'})
            self.env['ir.module.module'].create({'name': 'havsh_test_new_module', 'shortdesc': 'New'})

        self.assertIsNone(self._requested_trigger())

    def test_registry_load_requests_a_sync(self):
        self.sync._register_hook()
        self.assertIsNone(self._requested_trigger(), 'a ready registry only sets its models up again')

        with patch.object(self.registry, 'ready', False):
            self.sync._register_hook()
        self.assertEqual(self._requested_trigger(), HAVSH_ON_LOAD)

    def test_registry_load_after_a_write_syncs_as_a_load(self):
        """Only a load reports modules an operation left behind unfinished."""
        with patch.object(havsh_sync, '_havsh_sync_in_new_cursors') as run:
            self.env.ref('base.module_base').write({'author': 'Someone Else'})
            with patch.object(self.registry, 'ready', False):
                self.sync._register_hook()
            self.env.cr.postcommit.run()

        run.assert_called_once_with(self.registry, HAVSH_ON_LOAD)

    def test_test_runs_do_not_sync(self):
        """A preview test run has a real token; its uncommitted modules are nobody's business."""
        with patch.object(havsh_sync, 'HAVSH_SYNC_DURING_TESTS', False):
            self.env.ref('base.module_base').write({'author': 'Someone Else'})
            self.assertIsNone(self._requested_trigger())

            with self._registry_test_mode():
                self.sync._sync_all_to_havsh()
                self.sync._havsh_sync_daily()

        self.assertEqual(self.posted, [])

    def test_intermediate_states_are_not_sent_by_a_write(self):
        """During an install hav.sh gets the result, not "to install" first."""
        installing = self._uninstalled_module()
        other = self.env.ref('base.module_base')

        for state in ('to install', 'to upgrade', 'to remove'):
            with self.subTest(state=state):
                installing.write({'state': 'uninstalled'})
                self._synced()
                installing.write({'state': state})
                other.write({'author': state})
                self._run_postcommit()
                self.assertEqual(self._sent(), ['base'], 'only the module that waits for the operation is held back')
                self.posted.clear()

                installing.write({'state': 'installed'})
                self._run_postcommit()
                self.assertEqual(self._sent(), [installing.name])
                self.assertEqual(self.posted[0]['data']['modules'][0]['state'], 'installed')

    def test_module_stuck_in_an_intermediate_state_is_reported(self):
        """When the load is over, or the scheduled action is run by hand, "to install" is the truth to tell."""
        for trigger in (HAVSH_ON_LOAD, HAVSH_ON_SCHEDULE):
            with self.subTest(trigger=trigger):
                self._synced()
                stuck = self._uninstalled_module()
                stuck.write({'state': 'to install'})

                self._sync(trigger)

                self.assertEqual(self._sent(), [stuck.name])
                self.assertEqual(self.posted[0]['data']['modules'][0]['state'], 'to_install')
                stuck.write({'state': 'uninstalled'})

    def test_daily_sync_runs_with_the_autovacuum(self):
        """The only scheduled way in on a neutralized copy, where Odoo switches the action off."""
        self.assertTrue(getattr(HavShSync._havsh_sync_daily, '_autovacuum', False))

    def test_scheduled_action_and_autovacuum_sync_as_scheduled(self):
        """Only a scheduled sync goes on while automatic ones are paused after a failure."""
        for entry_point in ('_sync_all_to_havsh', '_havsh_sync_daily'):
            with self.subTest(entry_point=entry_point):
                self.answer = (None, 'ConnectTimeout')
                self.env.ref('base.module_base').write({'author': entry_point})
                self._sync()
                self._a_minute_later()
                self.posted.clear()
                self.answer = (200, None)

                with self._registry_test_mode():
                    getattr(self.sync, entry_point)()

                self.assertIn('base', self._sent())

    # ---- several syncs at once

    def test_sync_leaves_when_another_one_is_running(self):
        cron = self._cron()
        with self.registry.cursor() as other:
            other.execute("SELECT pg_advisory_xact_lock(%s)", [havsh_sync.HAVSH_LOCK_KEY])
            # a sync that waits for the lock must fail this test, not hang it
            self.env.cr.execute("SET LOCAL statement_timeout = '5s'")

            started = time.monotonic()
            with self.assertNoLogs(LOGGER, 'WARNING'):
                self.assertEqual(self._sync(HAVSH_ON_WRITE), 0)
            self.assertLess(time.monotonic() - started, 4, 'nobody may wait for a running sync')
            self.assertFalse(self._retries(cron), 'the running sync does all a write would have done')

            for trigger in (HAVSH_ON_LOAD, HAVSH_ON_SCHEDULE):
                self._retries(cron).unlink()
                self.assertEqual(self._sync(trigger), 0)
                self.assertEqual(
                    len(self._retries(cron)), 1,
                    'the running sync may not do all a %s sync would: the scheduled action has to' % trigger,
                )
            self.assertEqual(self.posted, [])

    def test_change_during_a_sync_is_sent_by_the_same_sync(self):
        """A sync that finds another one running leaves: the running one has to look again."""
        self._synced()
        first, late = self._settled_modules(2)
        first.write({'author': 'Someone Else'})

        def change_another_module_meanwhile(_model, url, payload, token, session=None, timeout=None):
            self.posted.append(payload)
            if len(self.posted) == 1:
                late.write({'author': 'Someone Else'})
                self.env.flush_all()
            return 200, None

        with patch.object(HavShSync, '_havsh__post', change_another_module_meanwhile):
            self._sync()

        self.assertEqual(self._sent(), [first.name, late.name])

    def test_sync_that_keeps_finding_changes_hands_over(self):
        self._synced()
        cron = self._cron()
        modules = self._settled_modules(5)
        modules[0].write({'author': 'round 0'})

        def change_another_module_every_time(_model, url, payload, token, session=None, timeout=None):
            self.posted.append(payload)
            modules[len(self.posted)].write({'author': 'round %s' % len(self.posted)})
            self.env.flush_all()
            return 200, None

        with patch.object(HavShSync, '_havsh__post', change_another_module_every_time), \
                patch.object(havsh_sync, 'HAVSH_MAX_ROUNDS', 3):
            self._sync()

        self.assertEqual(len(self.posted), 3)
        self.assertEqual(len(self._retries(cron)), 1, 'what is left must not wait for the next change')

    def test_sync_started_by_a_sync_does_nothing(self):
        """Opening a cursor can reload the registry, which asks for a sync."""
        self._synced()
        self.env.ref('base.module_base').write({'author': 'Someone Else'})
        nested = []

        def sync_again_meanwhile(_model, url, payload, token, session=None, timeout=None):
            self.posted.append(payload)
            nested.append(havsh_sync._havsh_sync_in_new_cursors(self.registry, HAVSH_ON_LOAD))
            return 200, None

        with patch.object(HavShSync, '_havsh__post', sync_again_meanwhile):
            self._sync()

        self.assertEqual(nested, [0])
        self.assertEqual(len(self.posted), 1)

    # ---- a sync must never break what started it

    def test_sync_problem_never_reaches_the_caller(self):
        self._synced()
        self.env.ref('base.module_base').write({'author': 'Someone Else'})

        with patch.object(HavShSync, '_havsh__snapshot', side_effect=RuntimeError('boom')), \
                self.assertLogs(LOGGER, 'WARNING'):
            self._run_postcommit()
            with self._registry_test_mode():
                self.sync._sync_all_to_havsh()
                self.sync._havsh_sync_daily()

        self.assertEqual(self.posted, [])
        self.assertEqual(self._sync(), 1, 'the next sync works again')

    def test_without_token_the_config_is_read_once_per_transaction(self):
        with patch.object(HavShSync, '_havsh__ensure_auth', return_value=(None, None)) as auth:
            for module in self._all_modules()[:20]:
                module.write({'author': 'Someone Else'})

        self.assertEqual(auth.call_count, 1)
        self.assertIsNone(self._requested_trigger())

    def test_problem_requesting_a_sync_never_reaches_the_caller(self):
        with patch.object(type(self.env.cr.postcommit), 'add', side_effect=RuntimeError('boom')), \
                self.assertLogs(LOGGER, 'WARNING'):
            self.env.ref('base.module_base').write({'author': 'Someone Else'})

    # ---- when hav.sh does not answer

    def test_failed_delivery_is_sent_again(self):
        self._synced()
        ok_at = self._state()['ok_at']
        self.assertTrue(ok_at)
        self.env.ref('base.module_base').write({'author': 'Someone Else'})

        self.answers = [(503, 'HTTP 503')]
        self.assertEqual(self._sync(), 0)
        self.assertEqual(self._state()['error'], 'HTTP 503')
        self.assertEqual(self._state()['ok_at'], ok_at, 'the last success must stay on record')
        self._a_minute_later()
        self.posted.clear()

        self._sync(HAVSH_ON_SCHEDULE)

        self.assertEqual(self._sent(), ['base'])
        self.assertEqual(self._state()['fail_count'], 0)
        self.assertIsNone(self._state()['error'])
        self.assertIsNone(self._state()['retry_at'])

    def test_failed_request_stops_the_sync(self):
        self.answer = (None, 'ConnectTimeout')

        with patch.object(havsh_sync, 'HAVSH_BATCH_SIZE', 100):
            self.assertEqual(self._sync(), 0)

        self.assertEqual(len(self.posted), 1, 'a failed request must not be followed by the next batch')

    def test_partly_failed_sync_only_resends_what_failed(self):
        total = len(self._all_modules())
        self.answers = [(200, None), (None, 'ReadTimeout')]

        with patch.object(havsh_sync, 'HAVSH_BATCH_SIZE', 100):
            self.assertEqual(self._sync(), 100)
            delivered = self._sent()[:100]
            self._a_minute_later()
            self.posted.clear()

            self._sync(HAVSH_ON_SCHEDULE)

        self.assertEqual(len(self._sent()), total - 100)
        self.assertFalse(set(self._sent()) & set(delivered))

    def test_automatic_sync_pauses_after_a_failure(self):
        """While hav.sh is down, not every commit and registry load may wait for it."""
        self.answer = (None, 'ConnectTimeout')
        self._sync()
        self.posted.clear()
        self.answer = (200, None)

        for trigger in (HAVSH_ON_WRITE, HAVSH_ON_LOAD, HAVSH_ON_SCHEDULE):
            self._sync(trigger)
        self.assertEqual(self.posted, [], 'right after a failure nobody may call hav.sh again')

        self._a_minute_later()
        for trigger in (HAVSH_ON_WRITE, HAVSH_ON_LOAD):
            self._sync(trigger)
        self.assertEqual(self.posted, [], 'automatic syncs stay paused')

        self._sync(HAVSH_ON_SCHEDULE)
        self.assertTrue(self.posted, 'the scheduled sync retries regardless')

    def test_pause_grows_with_every_failure_in_a_row(self):
        self.answer = (None, 'ConnectTimeout')
        pauses = []
        for _attempt in range(4):
            before = fields.Datetime.now()
            self._sync(HAVSH_ON_SCHEDULE)
            pauses.append(self._retry_at() - before)
            self._a_minute_later()

        self.assertEqual(self._state()['fail_count'], 4)
        self.assertEqual(self._state()['error'], 'ConnectTimeout')
        for pause, expected in zip(pauses, [1, 5, 30, 30]):
            self.assertAlmostEqual(pause.total_seconds(), expected * 60, delta=5)

    def test_rejected_request_pauses_for_long(self):
        """A wrong token or an unknown service does not get better within minutes."""
        for status in (401, 403, 404, 422):
            with self.subTest(status=status):
                self.answer = (status, 'HTTP %s' % status)
                before = fields.Datetime.now()

                self._sync(HAVSH_ON_SCHEDULE)

                self.assertAlmostEqual((self._retry_at() - before).total_seconds(), 3600, delta=5)
                self._a_minute_later()

    def test_corrected_token_is_tried_at_once(self):
        """The hour of pause belongs to the token hav.sh rejected."""
        self.answer = (403, 'HTTP 403: Invalid token')
        self._sync()
        self.posted.clear()
        self.answer = (200, None)

        self._sync()
        self.assertEqual(self.posted, [])

        self.token = 'corrected token'
        self._sync()
        self.assertCountEqual(self._sent(), self._all_modules().mapped('name'))

    def test_failure_is_forgotten_when_nothing_is_left_to_send(self):
        self._synced()
        module = self.env.ref('base.module_base')
        author = module.author
        module.write({'author': 'Someone Else'})
        self.answers = [(503, 'HTTP 503')]
        self._sync()
        self.posted.clear()
        module.write({'author': author})

        self._a_minute_later()
        self._sync(HAVSH_ON_WRITE)
        self.assertEqual(self._state()['fail_count'], 1, 'not while automatic syncs are paused')

        self._sync(HAVSH_ON_SCHEDULE)
        self.assertEqual(self.posted, [])
        self.assertEqual(self._state()['fail_count'], 0)
        self.assertIsNone(self._state()['error'])

    def test_failed_sync_asks_the_scheduled_action_to_retry(self):
        cron = self._cron()
        self.answer = (None, 'ConnectTimeout')

        self._sync()

        retry = self._retries(cron)
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry.call_at, self._retry_at())

        retry.call_at = fields.Datetime.now() + timedelta(minutes=3)
        self._a_minute_later()
        self._sync(HAVSH_ON_SCHEDULE)
        self.assertEqual(self._retries(cron), retry, 'one retry waiting is enough')

    def test_retry_is_not_left_to_a_run_that_comes_too_early(self):
        """After a module operation with hav.sh down, the autovacuum and the
        scheduled action come round in the same minute: the first fails again, the
        second may not try so soon and uses up the retry that started it."""
        cron = self._cron()
        self.answer = (None, 'ConnectTimeout')
        self._sync()
        due = self._retries(cron)
        due.call_at = fields.Datetime.now() - timedelta(seconds=5)
        self._a_minute_later()

        with self._registry_test_mode():
            self.sync._havsh_sync_daily()
            self.assertEqual(self._state()['fail_count'], 2)
            self.posted.clear()
            self.sync._sync_all_to_havsh()
        due.unlink()    # what Odoo does when the action is over

        self.assertEqual(self.posted, [], 'nobody tries again within a minute of a failure')
        self.assertEqual(self._state()['fail_count'], 2)
        self.assertEqual(self._retries(cron).call_at, self._retry_at(), 'a failure on record needs a retry to come')

    def test_retry_that_fails_again_asks_for_the_next_one(self):
        """Odoo keeps the trigger that started the action until the action is over."""
        cron = self._cron()
        self.answer = (None, 'ConnectTimeout')
        self._sync()
        started_by = self._retries(cron)
        started_by.call_at = fields.Datetime.now() - timedelta(seconds=5)
        self._a_minute_later()

        with self._registry_test_mode():
            self.sync._sync_all_to_havsh()

        self.assertEqual(self._state()['fail_count'], 2)
        self.assertEqual((self._retries(cron) - started_by).call_at, self._retry_at())
        self.assertIsNone(havsh_sync._havsh_thread.scheduled_since)

    def test_scheduled_action_out_of_time_asks_for_another_run(self):
        """The trigger that started it does not count: it is gone when the action is over."""
        cron = self._cron()
        clock = SimpleNamespace(now=1000.0)
        started_by = self.env['ir.cron.trigger'].create({'cron_id': cron.id, 'call_at': fields.Datetime.now()})

        def slow_post(_model, url, payload, token, session=None, timeout=None):
            self.posted.append(payload)
            clock.now += 12
            return 200, None

        with patch.object(havsh_sync, 'time', SimpleNamespace(monotonic=lambda: clock.now)), \
                patch.object(HavShSync, '_havsh__post', slow_post), \
                patch.object(havsh_sync, 'HAVSH_BATCH_SIZE', 100), \
                self._registry_test_mode():
            self.sync._sync_all_to_havsh()

        self.assertEqual(len(self.posted), 2, 'a third request would start with less than 2 of the 20 seconds left')
        self.assertEqual(self._state()['fail_count'], 0)
        self.assertEqual(len(self._retries(cron) - started_by), 1)

    def test_no_retry_is_asked_of_an_inactive_or_missing_action(self):
        """On a neutralized copy the retries would pile up without ever running."""
        cron = self._cron()
        triggers = self.env['ir.cron.trigger'].search([])
        self.answer = (None, 'ConnectTimeout')

        cron.active = False
        self._sync(HAVSH_ON_SCHEDULE)
        self.assertEqual(self.env['ir.cron.trigger'].search([]), triggers)
        self.assertEqual(self._state()['fail_count'], 1)
        self._a_minute_later()

        self.env['ir.model.data'].search([('module', '=', 'havsh'), ('name', '=', 'ir_cron_hav_sh_full_sync')]).unlink()
        self._sync(HAVSH_ON_SCHEDULE)
        self.assertEqual(self.env['ir.cron.trigger'].search([]), triggers)
        self.assertEqual(self._state()['fail_count'], 2)

    # ---- time

    def test_requests_get_what_is_left_of_the_budget(self):
        with patch.object(havsh_sync, 'HAVSH_SYNC_BUDGET', 5):
            self._sync(HAVSH_ON_WRITE)
            self.env.ref('base.module_base').write({'author': 'Someone Else'})
            self._sync(HAVSH_ON_SCHEDULE)

        for connect, read in self.timeouts[:-1]:
            self.assertLessEqual(connect, havsh_sync.HAVSH_CONNECT_TIMEOUT)
            self.assertLessEqual(read, 5, 'an automatic sync must not wait longer than its budget')
        self.assertEqual(self.timeouts[-1], (havsh_sync.HAVSH_CONNECT_TIMEOUT, havsh_sync.HAVSH_READ_TIMEOUT_SCHEDULED))

    def test_sync_after_a_registry_load_gets_less_time(self):
        """Odoo still holds its registry lock then, and others wait for it."""
        self.assertLess(havsh_sync.HAVSH_SYNC_BUDGET_ON_LOAD, havsh_sync.HAVSH_SYNC_BUDGET)
        with patch.object(havsh_sync, 'HAVSH_SYNC_BUDGET_ON_LOAD', 3):
            self._sync(HAVSH_ON_LOAD)
            self.env.ref('base.module_base').write({'author': 'Someone Else'})
            self._sync(HAVSH_ON_WRITE)

        self.assertGreater(len(self.timeouts), 1)
        for connect, read in self.timeouts[:-1]:
            self.assertLessEqual(connect, 3)
            self.assertLessEqual(read, 3, 'the sync after a registry load must not wait longer than its own budget')
        self.assertEqual(self.timeouts[-1], (havsh_sync.HAVSH_CONNECT_TIMEOUT, havsh_sync.HAVSH_READ_TIMEOUT))

    def test_sync_out_of_time_leaves_the_rest_to_the_scheduled_action(self):
        """Slow is not down: no failure, no pause, and the rest is sent without waiting for a change."""
        cron = self._cron()
        clock = SimpleNamespace(now=1000.0)

        def slow_post(_model, url, payload, token, session=None, timeout=None):
            self.posted.append(payload)
            clock.now += 6
            return 200, None

        with patch.object(havsh_sync, 'time', SimpleNamespace(monotonic=lambda: clock.now)), \
                patch.object(HavShSync, '_havsh__post', slow_post), \
                patch.object(havsh_sync, 'HAVSH_BATCH_SIZE', 100):
            self.assertEqual(
                self._sync(HAVSH_ON_WRITE), 200,
                'a third request would start with less than 2 of the 10 seconds left',
            )

        self.assertEqual(self._state()['fail_count'], 0)
        self.assertIsNone(self._state()['retry_at'])
        self.assertLessEqual(self._retries(cron).call_at, fields.Datetime.now())
        delivered = self._sent()
        self.posted.clear()

        self._sync()
        self.assertEqual(len(self._sent()), len(self._all_modules()) - 200)
        self.assertFalse(set(self._sent()) & set(delivered))

    def test_no_request_is_started_when_time_is_up(self):
        self._synced()
        self.env.ref('base.module_base').write({'author': 'Someone Else'})
        self.env.flush_all()
        plan = self.sync._havsh__plan(HAVSH_ON_LOAD)

        outcome = self.sync._havsh__send(plan, deadline=time.monotonic() + 1)

        self.assertEqual(outcome['delivered'], [])
        self.assertTrue(outcome['out_of_time'])
        self.assertIsNone(outcome['error'])
        self.assertEqual(self.posted, [])

    # ---- what counts as delivered

    def test_only_the_answer_of_hav_sh_counts_as_delivered(self):
        cases = [
            (FakeResponse(200, '{"status":"ok"}'), True),
            (FakeResponse(200, '<html>some page</html>'), False),
            (FakeResponse(200, '{"status":"error"}'), False),
            (FakeResponse(200, '["ok"]'), False),
            (FakeResponse(302, ''), False),
            (FakeResponse(403, '{"message":"Invalid token"}'), False),
            (FakeResponse(500, 'oops'), False),
        ]
        for response, delivered in cases:
            with self.subTest(status=response.status_code, body=response.text):
                session = FakeSession(response)

                status, error = self.real_post(self.sync, 'http://hav.sh.test/hook', {}, 'tok', session=session)

                self.assertEqual(status, response.status_code)
                self.assertEqual(error is None, delivered)
                self.assertIs(session.calls[0]['allow_redirects'], False)
                self.assertEqual(session.calls[0]['headers']['Authorization'], 'Bearer tok')

    def test_request_never_waits_without_a_limit(self):
        session = FakeSession(FakeResponse(200, '{"status":"ok"}'))

        self.real_post(self.sync, 'http://hav.sh.test/hook', {}, 'tok', session=session, timeout=(1, 2))
        self.real_post(self.sync, 'http://hav.sh.test/hook', {}, 'tok', session=session)

        self.assertEqual(session.calls[0]['timeout'], (1, 2))
        self.assertEqual(session.calls[1]['timeout'], (havsh_sync.HAVSH_CONNECT_TIMEOUT, havsh_sync.HAVSH_READ_TIMEOUT))

    def test_connection_problem_is_an_error_not_an_exception(self):
        class BrokenSession:
            def post(self, url, **kwargs):
                raise ConnectionError('no route to host')

        status, error = self.real_post(self.sync, 'http://hav.sh.test/hook', {}, 'tok', session=BrokenSession())

        self.assertIsNone(status)
        self.assertIn('no route to host', error)
