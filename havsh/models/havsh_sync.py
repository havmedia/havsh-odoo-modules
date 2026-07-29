# -*- coding: utf-8 -*-
import logging
import configparser

import requests

from odoo import api, models
from odoo.tools.config import config
from odoo import release

_logger = logging.getLogger(__name__)

# Default timeout for outbound HTTP calls to hav.sh (seconds)
HAVSH_HTTP_TIMEOUT = 5

class HavShSync(models.AbstractModel):
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
        """Build a standard webhook envelope.
        This keeps envelope creation consistent across event types.
        """
        return {"uuid": uuid, "event": event, "data": data}

    def _havsh__record_payload(self, rec):
        return {
            'module_name': rec.shortdesc or '',
            'technical_name': rec.name or '',
            'application': rec.application,
            'state': rec.state.replace(" ", "_") if rec.state else '',
            'author': rec.author or '',
            'license': rec.license or '',
            'category': rec.category_id.name or '',
            'version': getattr(rec, 'installed_version', '') or getattr(rec, 'latest_version', '') or '',
            'updated_at': rec.write_date.isoformat() if rec.write_date else '',
        }

    def _havsh__post(self, url, json_payload, token):
        try:
            headers = self._havsh__headers(token or '')
            try:
                verify_ssl = self._havsh__get_conf_value('havsh_client_ssl_verify', 'true').lower() != 'false'
            except Exception:
                verify_ssl = True
            resp = requests.post(url, json=json_payload, headers=headers, timeout=HAVSH_HTTP_TIMEOUT, verify=verify_ssl)
            if 200 <= resp.status_code < 300:
                _logger.info('hav.sh sync OK: POST %s (%s)', url, resp.status_code)
            else:
                _logger.warning('hav.sh sync HTTP %s for %s: %s', resp.status_code, url, resp.text)
        except Exception as e:  # noqa: BLE001 broad, by design for resilience
            _logger.warning('hav.sh sync failed for %s: %s', url, e)

    def _havsh__ensure_auth(self):
        token, uuid = self._havsh__get_auth()
        if not token or not uuid:
            _logger.warning('hav.sh token/uuid missing in configuration. Skipping call.')
            return None, None
        return token, uuid

    def _post_module_update(self, records, action):
        token, uuid = self._havsh__ensure_auth()
        if not token:
            return

        if not records:
            return
        url = self._havsh__webhook_url()

        for rec in records:
            payload = self._havsh__record_payload(rec)
            # Include the action explicitly in payload for downstream consumers
            if action:
                payload = dict(payload, action=action)
            envelope = self._havsh__envelope(uuid, action, payload)
            self._havsh__post(url, envelope, token)

    def _schedule_module_update(self, records, action):
        """Send a module sync to hav.sh AFTER the current transaction commits.

        The HTTP call must never run inside the transaction that writes
        ``ir.module.module``. "Update Apps List" and module install/upgrade write
        many module rows at once (each triggering the ``create``/``write`` override),
        and posting synchronously blocks on network I/O while the transaction — and
        its row locks on ``ir_module_module`` / ``ir_module_module_dependency`` — stays
        open for the whole round-trip. Under Odoo's REPEATABLE READ isolation that
        widens the window for "could not serialize access due to concurrent update"
        aborts (and Odoo's built-in serialization retry can't win, because every retry
        re-runs the same slow blocking POSTs), which crashes the operation for the user.

        Payloads are built now (cheap in-transaction reads) and posted from a
        ``cr.postcommit`` callback, so no network I/O happens until the transaction has
        committed and released its locks.
        """
        token, uuid = self._havsh__ensure_auth()
        if not token:
            return

        records = records.exists()
        if not records:
            return

        url = self._havsh__webhook_url()
        envelopes = [
            self._havsh__envelope(
                uuid,
                action,
                dict(self._havsh__record_payload(rec), action=action) if action else self._havsh__record_payload(rec),
            )
            for rec in records
        ]

        def _post_after_commit():
            for envelope in envelopes:
                self._havsh__post(url, envelope, token)

        self.env.cr.postcommit.add(_post_after_commit)

    @api.model
    def _sync_all_to_havsh(self):
        token, uuid = self._havsh__ensure_auth()
        if not token:
            return
        url = self._havsh__webhook_url()
        try:
            module_model = self.env['ir.module.module']
            modules = module_model.search([])
            data = {
                'odoo_version': getattr(release, 'version', 'unknown'),
                'modules': [self._havsh__record_payload(m) for m in modules],
            }
            envelope = self._havsh__envelope(uuid, "full-sync", data)
            self._havsh__post(url, envelope, token)
        except Exception as e:  # noqa: BLE001
            _logger.warning('hav.sh full sync failed: %s', e)
