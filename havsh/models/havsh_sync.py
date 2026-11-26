# -*- coding: utf-8 -*-
import configparser
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import requests

from odoo import SUPERUSER_ID, api, fields, models
from odoo import release
from odoo.modules import module as odoo_module
from odoo.tools.config import config

_logger = logging.getLogger(__name__)

# Outbound HTTP timeouts to hav.sh (seconds): time to connect, time to wait for an answer.
HAVSH_CONNECT_TIMEOUT = 3.05
HAVSH_READ_TIMEOUT = 8
HAVSH_READ_TIMEOUT_SCHEDULED = 15
# Time (seconds) one sync may spend on requests. An automatic sync runs right after
# a commit, in the thread that committed (a user's request, a server start): it has
# to stay short. Whatever is not sent in time is left to the scheduled sync. The
# last request may run over by the time it took to connect, and resolving the host
# name is not covered by any limit.
HAVSH_SYNC_BUDGET = 10
# The sync after a registry load gets less: Odoo still holds its registry lock
# then. Up to 19.0 that stalls the other threads of the process; from 20.0 on the
# other workers wait 15 seconds for it and fail then.
HAVSH_SYNC_BUDGET_ON_LOAD = 5
# The scheduled sync runs in a cron worker. While it runs, Odoo refuses to install
# or upgrade modules ("Odoo is currently processing a scheduled action").
HAVSH_SYNC_BUDGET_SCHEDULED = 20
# A request is not started with less than this (seconds) left of the budget.
HAVSH_MIN_REQUEST_TIME = 2
# Modules per request. A sync that has more to send splits it into several requests,
# so each one stays quick for hav.sh and a failure only costs that part.
HAVSH_BATCH_SIZE = 500
# A sync that sent something looks again afterwards, for what changed meanwhile.
# The budget is what ends it; this only stops a sync that never gets to send.
HAVSH_MAX_ROUNDS = 10
# The scheduled sync sends every module again this often even when it did not change,
# so hav.sh recovers from whatever changed on its side only. Just under a day: the
# action runs every 6 hours and has to find the previous round old enough.
HAVSH_FULL_RESYNC_INTERVAL = timedelta(hours=23)
# After a failed sync, automatic syncs pause: this long after the 1st, 2nd and any
# further failure in a row (for the first of them nobody tries again at all, not
# even the scheduled sync) ...
HAVSH_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
# ... and this long when hav.sh rejects the request itself (wrong token, unknown
# service): retrying soon cannot help.
HAVSH_RETRY_DELAY_REJECTED = timedelta(hours=1)
HAVSH_REJECTED_STATUSES = (401, 403, 404, 422)
# What a sync could not do itself is handed to the scheduled action after this long.
HAVSH_HAND_OVER_DELAY = timedelta(minutes=1)

# ir_config_parameter row holding what was last delivered to hav.sh.
HAVSH_STATE_KEY = 'havsh.sync_state'
HAVSH_STATE_VERSION = 1
# Advisory lock that serializes the syncs of one database.
HAVSH_LOCK_KEY = int.from_bytes(b'havsh.sy', 'big')
HAVSH_CRON_XMLID = 'havsh.ir_cron_hav_sh_full_sync'
# The payload keys hav.sh stores. A module is sent again only when one of them changed.
HAVSH_DIGEST_FIELDS = (
    'technical_name', 'module_name', 'state', 'version', 'author', 'license', 'category', 'application',
)
# hav.sh stores texts in 255 characters and refuses a request with a longer one.
HAVSH_TEXT_SIZE = 255
HAVSH_PENDING_STATES = ('to_install', 'to_upgrade', 'to_remove')
HAVSH_NOT_INSTALLED_STATES = ('uninstalled', 'uninstallable')

# What started a sync.
HAVSH_ON_WRITE = 'write'            # a commit that wrote module rows
HAVSH_ON_LOAD = 'load'              # a registry was loaded: server start, odoo -i/-u, the end of an install
HAVSH_ON_SCHEDULE = 'schedule'      # the scheduled action or the daily autovacuum

# Test runs must not report the uncommitted modules of a test to a real hav.sh.
# The tests of this module switch it on.
HAVSH_SYNC_DURING_TESTS = False

_havsh_thread = threading.local()


def _havsh_sync_in_new_cursors(registry, trigger):
    """Run one sync. Never raises.

    A sync never shares a transaction with its caller. It runs after the caller's
    commit and uses short transactions of its own. While it talks to hav.sh only
    one of them is open, the idle one that holds the sync lock: it has read and
    locked no rows, so slow network never keeps database locks.

    Syncs of one database are serialized by an advisory lock, and each one reads
    the modules after it got the lock: hav.sh receives the snapshots in order.
    Nobody waits for the lock. A sync that finds it taken leaves, because whoever
    holds it looks again after releasing it, and a sync only takes the lock once
    it has seen something to send, so the holder is never someone about to leave
    without that second look. Where the holder cannot do what the leaving sync
    would have done, the scheduled action is asked to (``_havsh_hand_over``). A
    sync that breaks leaves the work to the next one.
    """
    if odoo_module.current_test and not HAVSH_SYNC_DURING_TESTS:
        return 0
    if getattr(_havsh_thread, 'syncing', False):
        return 0        # e.g. a registry reload caused by the sync's own cursor
    _havsh_thread.syncing = True
    try:
        return _havsh_sync_rounds(registry, trigger)
    except Exception:  # noqa: BLE001 a sync problem must never break the caller
        _logger.warning('hav.sh sync failed', exc_info=True)
        return 0
    finally:
        _havsh_thread.syncing = False


def _havsh_sync_model(cr):
    return api.Environment(cr, SUPERUSER_ID, {})['havsh.sync']


def _havsh_sync_rounds(registry, trigger):
    budget = {
        HAVSH_ON_WRITE: HAVSH_SYNC_BUDGET,
        HAVSH_ON_LOAD: HAVSH_SYNC_BUDGET_ON_LOAD,
        HAVSH_ON_SCHEDULE: HAVSH_SYNC_BUDGET_SCHEDULED,
    }[trigger]
    deadline = None
    sent = 0
    for _round in range(HAVSH_MAX_ROUNDS):
        with registry.cursor() as cr:
            if not _havsh_sync_model(cr)._havsh__plan(trigger):
                return sent
        with registry.cursor() as lock_cr:
            lock_cr.execute("SELECT pg_try_advisory_xact_lock(%s)", [HAVSH_LOCK_KEY])
            locked = lock_cr.fetchone()[0]
            if locked:
                sync = _havsh_sync_model(lock_cr)
                with registry.cursor() as cr:
                    plan = _havsh_sync_model(cr)._havsh__plan(trigger)
                if not plan:
                    continue
                # Only requests count against the budget, not the reading before them.
                deadline = deadline or time.monotonic() + budget
                outcome = sync._havsh__send(plan, deadline)
                if outcome['requests'] or not plan['to_send']:
                    with registry.cursor() as cr:
                        _havsh_sync_model(cr)._havsh__store(plan, outcome)
        if not locked:
            _logger.debug('hav.sh sync: another one is running and will pick up the changes')
            if trigger != HAVSH_ON_WRITE:
                # The running one may have been started by a write: it would
                # neither report modules stuck in an operation nor send every
                # module again.
                _havsh_hand_over(registry, HAVSH_HAND_OVER_DELAY)
            return sent
        sent += len(outcome['delivered'])
        if outcome['error']:
            return sent
        if outcome['out_of_time']:
            break
    # Time or rounds are used up. Whoever left because this sync held the lock
    # counts on a look that does not happen any more.
    _havsh_hand_over(registry)
    return sent


def _havsh_hand_over(registry, delay=None):
    """Ask the scheduled action to sync, now or after ``delay``. Best effort, never raises.

    The action may be switched off, have no worker, or be held back while a module
    waits to be installed, upgraded or removed; then the next commit that writes
    module rows or the next registry load takes over, and the daily autovacuum
    where a cron worker runs.
    """
    try:
        with registry.cursor() as cr:
            sync = _havsh_sync_model(cr)
            sync._havsh__limit_lock_waits()
            if delay or sync._havsh__plan(HAVSH_ON_SCHEDULE):
                sync._havsh__schedule_retry(fields.Datetime.now() + (delay or timedelta()))
    except Exception:  # noqa: BLE001
        _logger.debug('hav.sh sync: could not hand over to the scheduled action', exc_info=True)


class HavShSync(models.AbstractModel):
    """Mirrors the list of modules (``ir.module.module``) to hav.sh.

    The sync is state based: it compares every module with what hav.sh was last
    sent and posts only the modules that differ, together (one request per
    ``HAVSH_BATCH_SIZE`` modules). Nothing is sent when nothing changed. It is
    started

    - after a commit that wrote module rows (``ir.module.module`` create/write),
    - after a registry was loaded: server start, ``odoo -i/-u``, the end of an
      install from the Apps screen. This is the one sync of a module operation,
      with the final states, and it covers what the write hook cannot see: states
      written with SQL, modules loaded before this one, a new version on disk
      after an image update,
    - by the scheduled action and the daily autovacuum, which also retry what
      could not be delivered and send every module once a day.

    Everything here works without a module upgrade (``-u havsh``): it needs no
    new table, field or data record. What was delivered is kept in one
    ``ir_config_parameter`` row.
    """
    _name = 'havsh.sync'
    _description = 'HAV.SH Sync Service'

    # ---- Config resolution helpers
    def _havsh__clean(self, val):
        """Normalize values coming from config files.
        - Strip surrounding quotes (single/double)
        - Trim whitespace
        """
        if val is None:
            return None
        s = str(val).strip()
        # remove surrounding matching quotes if present
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            s = s[1:-1]
        return s

    def _havsh__get_conf_value(self, key, default=None):
        """Read a value from the [havsh] section in the main Odoo config file.
        Falls back to odoo.tools.config for backward compatibility.
        Does not rely on server_env helper.
        """
        val = None
        try:
            rcfile = None
            try:
                rcfile = config["config"]
            except Exception:
                try:
                    rcfile = config.get("config")
                except Exception:
                    rcfile = None
            if not rcfile:
                # Odoo 17.0 and 18.0 keep the path of the config file here only
                rcfile = getattr(config, "rcfile", None)
            if rcfile:
                cp = configparser.ConfigParser(interpolation=None)
                cp.read([rcfile])
                if cp.has_section("havsh"):
                    val = cp.get("havsh", key, fallback=None)
        except Exception:
            # Any parsing issue -> ignore and fallback
            pass
        if not val:
            try:
                val = config.get(key)
            except Exception:
                val = None
        cleaned = self._havsh__clean(val)
        return cleaned if cleaned is not None else default

    # Auth helpers
    def _havsh__get_auth(self):
        """Resolve auth token and UUID from configuration.
        Returns (token, uuid) or (None, None) if missing.
        """
        token = self._havsh__get_conf_value('havsh_client_token')
        uuid = self._havsh__get_conf_value('havsh_client_uuid')
        if not token or not uuid:
            return None, None
        return token, uuid

    def _havsh__headers(self, token):
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _havsh__base_url(self):
        """Resolve the base URL for hav.sh API from config.
        Strips any trailing slash to avoid double slashes when composing URLs.
        The value is read from the [havsh] section when available.
        """
        base = self._havsh__get_conf_value('havsh_client_url') or 'https://hav.sh'
        return base.rstrip('/')

    def _havsh__webhook_url(self):
        """Return the standard hav.sh webhook endpoint URL."""
        return f"{self._havsh__base_url()}/api/webhooks/service-odoo"

    def _havsh__envelope(self, uuid, event, data):
        """Build the webhook envelope."""
        return {"uuid": uuid, "event": event, "data": data}

    def _havsh__record_payload(self, rec):
        def text(value):
            # hav.sh answers a longer text with an error for the whole request
            return (value or '')[:HAVSH_TEXT_SIZE]

        return {
            'module_name': text(rec.shortdesc),
            'technical_name': text(rec.name),
            'application': rec.application,
            'state': rec.state.replace(" ", "_") if rec.state else '',
            'author': text(rec.author),
            'license': text(rec.license),
            'category': text(rec.category_id.name),
            'version': text(getattr(rec, 'installed_version', '') or getattr(rec, 'latest_version', '')),
            # not stored by hav.sh and no reason to send a module again; kept so
            # the payload stays what it was
            'updated_at': rec.write_date.isoformat() if rec.write_date else '',
        }

    def _havsh__ssl_verify(self):
        try:
            return self._havsh__get_conf_value('havsh_client_ssl_verify', 'true').lower() != 'false'
        except Exception:
            return True

    def _havsh__post(self, url, json_payload, token, session=None, timeout=None):
        """POST one webhook. Never raises.

        :return: ``(status, error)``. ``error`` is None when hav.sh accepted the
            request; ``status`` is None when no answer came.
        """
        try:
            resp = (session or requests).post(
                url,
                json=json_payload,
                headers=self._havsh__headers(token or ''),
                timeout=timeout or (HAVSH_CONNECT_TIMEOUT, HAVSH_READ_TIMEOUT),
                verify=self._havsh__ssl_verify(),
                # A redirect is not hav.sh accepting anything: following it would
                # turn the POST into a GET of some page that answers 200.
                allow_redirects=False,
            )
        except Exception as e:  # noqa: BLE001 broad, by design for resilience
            # without the object addresses and timings that differ on every attempt
            reason = re.sub(r' object at 0x[0-9a-f]+|\s*\((?:read|connect) timeout=[\d.]+\)', '', str(e))
            return None, f'{type(e).__name__}: {reason}'[:300]
        if not 200 <= resp.status_code < 300:
            return resp.status_code, f'HTTP {resp.status_code}: {(resp.text or "")[:300]}'
        try:
            accepted = resp.json().get('status') == 'ok'
        except Exception:  # noqa: BLE001 not JSON, or not an object
            accepted = False
        if not accepted:
            return resp.status_code, f'HTTP {resp.status_code} without the expected answer: {(resp.text or "")[:300]}'
        return resp.status_code, None

    def _havsh__ensure_auth(self):
        token, uuid = self._havsh__get_auth()
        if not token or not uuid:
            _logger.debug('hav.sh token/uuid missing in configuration. Skipping call.')
            return None, None
        return token, uuid

    def _havsh__token_digest(self, token):
        return hashlib.sha256((token or '').encode()).hexdigest()[:8]

    # ---- What was delivered (one ir_config_parameter row)
    def _havsh__database_identity(self):
        """Tell this database from a copy or a restored backup of it.

        Both create the database anew, which changes its oid; a rebuilt PostgreSQL,
        where oids start over, has another system identifier.
        """
        cr = self.env.cr
        cr.execute("SELECT oid FROM pg_database WHERE datname = current_database()")
        row = cr.fetchone()
        identity = str(row[0]) if row else ''
        try:
            with cr.savepoint(flush=False):
                cr.execute("SELECT system_identifier FROM pg_control_system()", log_exceptions=False)
                identity += f'@{cr.fetchone()[0]}'
        except Exception:  # noqa: BLE001 not allowed to ask: the oid has to do
            pass
        return identity

    def _havsh__target(self, uuid):
        """Identify where the stored state belongs: this service, this hav.sh, this database.

        The row lives in the database, so a copy or a restored backup brings along
        the state of another service or of an earlier time. It is then ignored and
        every module is sent again.
        """
        raw = '|'.join((uuid or '', self._havsh__base_url(), self._havsh__database_identity()))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _havsh__read_state(self, target):
        """Return the stored state, or an empty one when there is none for ``target``.

        Plain SQL on purpose, here and in ``_havsh__write_state``: writing through
        ``ir.config_parameter`` would drop the ORM caches of every worker.
        """
        empty = {
            'valid': False, 'digests': {}, 'full_at': None, 'ok_at': None,
            'fail_at': None, 'retry_at': None, 'retry_token': None, 'fail_count': 0,
        }
        self.env.cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [HAVSH_STATE_KEY])
        row = self.env.cr.fetchone()
        if not row or not row[0]:
            return empty
        try:
            state = json.loads(row[0])
            if state.get('v') != HAVSH_STATE_VERSION or state.get('target') != target:
                return empty
            digests = state.get('digests')
            return {
                'valid': True,
                'digests': dict(digests) if isinstance(digests, dict) else {},
                'full_at': self._havsh__parse_datetime(state.get('full_at')),
                'ok_at': self._havsh__parse_datetime(state.get('ok_at')),
                'fail_at': self._havsh__parse_datetime(state.get('fail_at')),
                'retry_at': self._havsh__parse_datetime(state.get('retry_at')),
                'retry_token': state.get('retry_token'),
                'fail_count': int(state.get('fail_count') or 0),
            }
        except Exception:  # noqa: BLE001 an unreadable state is an empty state
            return empty

    def _havsh__write_state(self, target, state):
        """Store the state. Its first keys answer "why is hav.sh not up to date?"."""
        value = json.dumps({
            'v': HAVSH_STATE_VERSION,
            'target': target,
            'ok_at': self._havsh__format_datetime(state.get('ok_at')),
            'error': state.get('error'),
            'fail_count': state.get('fail_count') or 0,
            'fail_at': self._havsh__format_datetime(state.get('fail_at')),
            'retry_at': self._havsh__format_datetime(state.get('retry_at')),
            'retry_token': state.get('retry_token'),
            'full_at': self._havsh__format_datetime(state.get('full_at')),
            'digests': state['digests'],
        }, separators=(',', ':'))
        self.env.cr.execute(
            """
            INSERT INTO ir_config_parameter (key, value, create_date, write_date)
            VALUES (%s, %s, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = EXCLUDED.write_date
            """,
            [HAVSH_STATE_KEY, value],
        )

    def _havsh__parse_datetime(self, value):
        try:
            return datetime.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None

    def _havsh__format_datetime(self, value):
        return value.isoformat() if value else None

    def _havsh__digest(self, payload):
        raw = json.dumps([payload.get(key) for key in HAVSH_DIGEST_FIELDS], separators=(',', ':'), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:8]

    # ---- Sync
    def _havsh__limit_lock_waits(self):
        # Never queue behind someone who holds these tables, e.g. a module install
        # adding a column to ir_module_module: the sync may run under Odoo's
        # registry lock. Giving up only means the same modules are sent later.
        self.env.cr.execute("SET LOCAL lock_timeout = '2s'")

    def _havsh__snapshot(self):
        """Return the payload of every module, as ``{technical_name: payload}``.

        ``prefetch_fields=False`` reads only the columns the payload uses: module
        rows carry large texts. The environment has no language, so names are the
        en_US ones whoever triggered the sync.
        """
        modules = self.env['ir.module.module'].with_context(prefetch_fields=False).search([], order='id')
        snapshot = {}
        for module in modules:
            payload = self._havsh__record_payload(module)
            if payload['technical_name']:
                snapshot[payload['technical_name']] = payload
        return snapshot

    def _havsh__plan(self, trigger):
        """Work out what hav.sh has to be sent. Reads only.

        :return: None when there is nothing to do: no token, a sync failed within
            the last minute, automatic syncs are paused after a failure, hav.sh
            already knows every module as it is, or a write only changed modules
            that wait to be installed, upgraded or removed. A plan without modules
            only clears a failure that is over.
        """
        token, uuid = self._havsh__ensure_auth()
        if not token:
            return None
        self._havsh__limit_lock_waits()
        scheduled = trigger == HAVSH_ON_SCHEDULE
        now = fields.Datetime.now()
        target = self._havsh__target(uuid)
        state = self._havsh__read_state(target)
        # The pause belongs to the token that failed: a corrected one is tried at once.
        if state['retry_token'] in (None, self._havsh__token_digest(token)):
            # Nobody tries again within a minute of a failure, not even the
            # scheduled sync: after a module operation the autovacuum and the
            # scheduled action both come round in the same minute.
            just_failed = state['fail_at'] and now - state['fail_at'] < HAVSH_RETRY_DELAYS[0]
            paused = not scheduled and state['retry_at'] and now < state['retry_at']
            if just_failed or paused:
                _logger.debug(
                    'hav.sh sync (%s) not tried: the last one failed at %s, automatic syncs pause until %s',
                    trigger, state['fail_at'], state['retry_at'],
                )
                return None

        snapshot = self._havsh__snapshot()
        current = {name: self._havsh__digest(payload) for name, payload in snapshot.items()}
        # Sending every module again is "forget what was delivered": what cannot
        # be sent this time simply stays to be sent, like any other change.
        resend_all = not state['valid'] or (scheduled and (
            not state['full_at'] or now - state['full_at'] >= HAVSH_FULL_RESYNC_INTERVAL
        ))
        delivered = {} if resend_all else {
            name: digest for name, digest in state['digests'].items() if name in current
        }
        undelivered = [name for name, digest in current.items() if delivered.get(name) != digest]
        to_send = undelivered
        if trigger == HAVSH_ON_WRITE:
            # hav.sh gets the result of an operation, not "to install" first: the
            # registry load that ends the operation sends the final states. A
            # module that stays in such a state is reported by the next registry
            # load (Odoo runs no scheduled action while one is in that state).
            to_send = [name for name in undelivered if snapshot[name]['state'] not in HAVSH_PENDING_STATES]
        if not to_send and (undelivered or not state['fail_count']):
            return None
        # What is installed matters most, should the time not suffice for everything.
        to_send.sort(key=lambda name: (snapshot[name]['state'] in HAVSH_NOT_INSTALLED_STATES, name))
        return {
            'trigger': trigger,
            'token': token,
            'uuid': uuid,
            'url': self._havsh__webhook_url(),
            'target': target,
            'now': now,
            'snapshot': snapshot,
            'current': current,
            'delivered': delivered,
            'to_send': to_send,
            'full_at': now if resend_all else state['full_at'],
            'resend_all': resend_all,
            'ok_at': state['ok_at'],
            'fail_count': state['fail_count'],
        }

    def _havsh__send(self, plan, deadline):
        """Post the planned modules, one request per batch, until one fails or time is up.

        No database access in here.

        :return: ``{'delivered': [names], 'requests': int, 'status': int|None,
            'error': str|None, 'out_of_time': bool}``
        """
        scheduled = plan['trigger'] == HAVSH_ON_SCHEDULE
        read_timeout = HAVSH_READ_TIMEOUT_SCHEDULED if scheduled else HAVSH_READ_TIMEOUT
        to_send = plan['to_send']
        outcome = {'delivered': [], 'requests': 0, 'status': None, 'error': None, 'out_of_time': False}
        with requests.Session() as session:
            for start in range(0, len(to_send), HAVSH_BATCH_SIZE):
                remaining = deadline - time.monotonic()
                if remaining < HAVSH_MIN_REQUEST_TIME:
                    outcome['out_of_time'] = True
                    break
                names = to_send[start:start + HAVSH_BATCH_SIZE]
                envelope = self._havsh__envelope(plan['uuid'], 'full-sync', {
                    'odoo_version': getattr(release, 'version', 'unknown'),
                    # Never the complete list, even when every module is sent: the
                    # modules missing from this request must not be taken as gone.
                    'partial': True,
                    'modules': [plan['snapshot'][name] for name in names],
                })
                outcome['requests'] += 1
                # Connecting normally takes no time, so the answer may take all
                # that is left; a slow connect makes the sync run over by that much.
                outcome['status'], outcome['error'] = self._havsh__post(
                    plan['url'], envelope, plan['token'], session=session,
                    timeout=(min(HAVSH_CONNECT_TIMEOUT, remaining), min(read_timeout, remaining)),
                )
                if outcome['error']:
                    break
                outcome['delivered'].extend(names)
        return outcome

    def _havsh__store(self, plan, outcome):
        """Remember what was delivered and, after a failure, when to try again."""
        self._havsh__limit_lock_waits()
        now = fields.Datetime.now()
        delivered = dict(plan['delivered'])
        delivered.update((name, plan['current'][name]) for name in outcome['delivered'])
        state = {
            'digests': delivered,
            'full_at': plan['full_at'],
            # when hav.sh last accepted a request
            'ok_at': plan['now'] if outcome['delivered'] else plan['ok_at'],
            'fail_count': 0,
        }
        left = len(plan['to_send']) - len(outcome['delivered'])
        if outcome['error']:
            fail_count = plan['fail_count'] + 1
            if outcome['status'] in HAVSH_REJECTED_STATUSES:
                delay = HAVSH_RETRY_DELAY_REJECTED
            else:
                delay = HAVSH_RETRY_DELAYS[min(fail_count, len(HAVSH_RETRY_DELAYS)) - 1]
            state.update(
                fail_count=fail_count, fail_at=now, retry_at=now + delay, error=outcome['error'],
                retry_token=self._havsh__token_digest(plan['token']),
            )
            _logger.warning(
                'hav.sh sync failed (%s in a row): %s. %s module(s) not delivered, automatic syncs pause until %s',
                fail_count, outcome['error'], left, state['retry_at'],
            )
        self._havsh__write_state(plan['target'], state)
        if outcome['error']:
            # A retry that comes before the minute in which nobody tries is over
            # would find nothing to do and be used up.
            self._havsh__schedule_retry(state['retry_at'], not_before=now + HAVSH_RETRY_DELAYS[0])
        if outcome['delivered']:
            _logger.info(
                'hav.sh sync (%s): sent %s module(s) in %s request(s)%s%s',
                plan['trigger'], len(outcome['delivered']), outcome['requests'] - bool(outcome['error']),
                ', every module is being sent again' if plan['resend_all'] else '',
                f', {left} left for the next sync' if left and not outcome['error'] else '',
            )

    def _havsh__schedule_retry(self, at, not_before=None):
        """Ask the scheduled action to sync at ``at``, unless it already runs
        between ``not_before`` and then. Best effort: it may be inactive or absent."""
        try:
            with self.env.cr.savepoint():
                cron = self.env.ref(HAVSH_CRON_XMLID, raise_if_not_found=False)
                if not cron or not cron.active:
                    return
                # One that is due by then does the job, also one that is overdue
                # because no cron worker runs ...
                waiting = [('cron_id', '=', cron.id), ('call_at', '<=', at)]
                if not_before:
                    waiting.append(('call_at', '>=', not_before))
                running_since = getattr(_havsh_thread, 'scheduled_since', None)
                if running_since:
                    # ... but not the one that started the scheduled action this
                    # sync runs in: it is still there for this sync to see, Odoo
                    # is rid of it only once the action is over.
                    waiting.append(('call_at', '>', running_since))
                if not self.env['ir.cron.trigger'].sudo().search_count(waiting):
                    cron._trigger(at=at)
        except Exception:  # noqa: BLE001
            _logger.debug('hav.sh sync: could not schedule a retry', exc_info=True)

    @api.model
    def _havsh_request_sync(self, trigger=HAVSH_ON_WRITE):
        """Sync once the current transaction has committed. Never raises.

        Callers only say "module data may have changed"; what to send is worked out
        after the commit, from the database. However often this is called in a
        transaction, it leads to one sync.
        """
        try:
            if odoo_module.current_test and not HAVSH_SYNC_DURING_TESTS:
                return
            postcommit = self.env.cr.postcommit
            request = postcommit.data.get('havsh_sync')
            if request is not None:
                if request and trigger == HAVSH_ON_LOAD:
                    request['trigger'] = trigger
                return
            if not self._havsh__ensure_auth()[0]:
                # off: not looked up again before this cursor next runs its
                # post-commit hooks or rolls back
                postcommit.data['havsh_sync'] = {}
                if trigger == HAVSH_ON_LOAD:
                    _logger.info('hav.sh sync is off: havsh_client_token/havsh_client_uuid are not configured')
                return
            request = postcommit.data['havsh_sync'] = {'trigger': trigger}
            registry = self.env.registry
            postcommit.add(lambda: _havsh_sync_in_new_cursors(registry, request['trigger']))
        except Exception:  # noqa: BLE001
            _logger.warning('hav.sh sync could not be scheduled', exc_info=True)

    def _register_hook(self):
        """Sync when a registry was loaded: server start, ``odoo -i/-u``, the end of
        an install from the Apps screen. The module states are final by then."""
        super()._register_hook()
        # A ready registry only sets its models up again (a custom field changed).
        if not self.env.registry.ready:
            self._havsh_request_sync(HAVSH_ON_LOAD)

    @api.model
    def _sync_all_to_havsh(self):
        """Entry point of the scheduled action "HAV.SH Full Sync".

        Existing databases call it by this name, so it stays. It sends what
        changed, and every module once a day.
        """
        _havsh_thread.scheduled_since = fields.Datetime.now()
        try:
            _havsh_sync_in_new_cursors(self.env.registry, HAVSH_ON_SCHEDULE)
        finally:
            _havsh_thread.scheduled_since = None

    @api.autovacuum
    def _havsh_sync_daily(self):
        """A second, daily, way in for the scheduled sync.

        The scheduled action of this module does not run everywhere: Odoo switches
        it off in a neutralized copy, where the autovacuum keeps running.
        """
        _havsh_sync_in_new_cursors(self.env.registry, HAVSH_ON_SCHEDULE)
