# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.addons.havsh.models.havsh_sync import HavShSync


class TestModuleSyncDeferred(TransactionCase):
    """The hav.sh module sync must never do HTTP inside the ir.module.module
    transaction: doing so holds row locks open across the network round-trip and
    causes 'could not serialize access due to concurrent update' aborts during
    'Update Apps List' / module install. The POST must be deferred to post-commit.
    """

    def _sync_module(self):
        # base is always installed; latest_version is a plain, harmless sync field.
        return self.env.ref('base.module_base')

    def test_write_defers_post_until_after_commit(self):
        module = self._sync_module()
        calls = []

        with patch.object(HavShSync, '_havsh__ensure_auth', return_value=('tok', 'uid')), \
                patch.object(HavShSync, '_havsh__post', side_effect=lambda *a, **k: calls.append(a)):
            module.write({'latest_version': '20.0.0.0.99'})

            # Nothing may hit the network while the transaction (and its locks) is open.
            self.assertEqual(
                calls, [],
                'hav.sh POST ran inside the write transaction; it must be deferred to post-commit',
            )

            # It is registered as a post-commit callback and runs once locks are released.
            self.env.cr.postcommit.run()
            self.assertEqual(
                len(calls), 1,
                'hav.sh POST did not run after the transaction committed',
            )

    def test_write_without_auth_posts_nothing(self):
        module = self._sync_module()

        with patch.object(HavShSync, '_havsh__ensure_auth', return_value=(None, None)), \
                patch.object(HavShSync, '_havsh__post') as post:
            module.write({'latest_version': '20.0.0.0.98'})
            self.env.cr.postcommit.run()
            post.assert_not_called()

    def test_non_sync_field_write_posts_nothing(self):
        module = self._sync_module()

        with patch.object(HavShSync, '_havsh__ensure_auth', return_value=('tok', 'uid')), \
                patch.object(HavShSync, '_havsh__post') as post:
            # 'demo' is not in _HAVSH_SYNC_FIELDS, so no sync should be scheduled.
            module.write({'demo': module.demo})
            self.env.cr.postcommit.run()
            post.assert_not_called()
