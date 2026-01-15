# -*- coding: utf-8 -*-
import logging

from odoo import api, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Admin user ID in Odoo (the first user created after OdooBot)
ADMIN_USER_ID = 2


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _register_hook(self):
        """Called when registry is built - reset admin password on every Odoo restart."""
        super()._register_hook()
        # Reset admin password from config on every startup
        try:
            self._havsh_reset_admin_password()
        except Exception as e:
            _logger.warning('Failed to reset admin password on startup: %s', e)

    def _havsh_get_admin_password(self):
        """Get admin password from odoo.conf [havsh] section."""
        sync_model = self.env['havsh.sync']
        return sync_model._havsh__get_conf_value('havsh_admin_password')

    def _havsh_is_admin_user(self, user_ids=None):
        """Check if any of the given user IDs contains the admin user."""
        if user_ids is None:
            user_ids = self.ids
        return ADMIN_USER_ID in user_ids

    def write(self, vals):
        """Prevent password changes for admin user if havsh_admin_password is configured."""
        if 'password' in vals and self._havsh_is_admin_user():
            if self._havsh_get_admin_password():
                raise UserError(_('The admin password is managed via odoo.conf and cannot be changed.'))

        return super(ResUsers, self).write(vals)

    @api.model
    def _havsh_reset_admin_password(self):
        """Reset admin password from odoo.conf configuration.

        This method should be called on module init/restart to ensure
        the admin password matches the configured value.
        """
        admin_pwd = self._havsh_get_admin_password()
        if not admin_pwd:
            _logger.info('No havsh_admin_password configured, skipping admin password reset')
            return False

        admin_user = self.browse(ADMIN_USER_ID)
        if not admin_user.exists():
            _logger.warning('Admin user (id=%s) not found, cannot reset password', ADMIN_USER_ID)
            return False

        # Bypass our own write protection by calling super directly
        super(ResUsers, admin_user).write({'password': admin_pwd})
        _logger.info('Admin password reset from odoo.conf havsh_admin_password')
        return True