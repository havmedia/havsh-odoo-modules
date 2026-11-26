# -*- coding: utf-8 -*-
from odoo import api, models


class IrModuleModule(models.Model):
    _inherit = 'ir.module.module'

    # Fields whose change is worth a sync to hav.sh
    _HAVSH_SYNC_FIELDS = {
        'shortdesc',        # module_name
        'name',             # technical_name
        'application',      # application
        'state',            # state
        'author',           # author
        'license',          # license
        'category_id',      # category
        'latest_version',   # version
    }

    # Writing module rows only requests a sync. Nothing is sent from here: Odoo
    # writes these rows one at a time, hundreds of times per "Update Apps List" or
    # install, mostly with unchanged values. The sync runs once after the commit and
    # works out from the database what, if anything, hav.sh does not know yet.
    #
    # While a registry is being loaded (an install or upgrade is running) not even
    # that: the load ends with a sync of its own, once every module is done.

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if self.env.registry.ready and any(self._HAVSH_SYNC_FIELDS.intersection(vals) for vals in vals_list):
            self.env['havsh.sync']._havsh_request_sync()
        return records

    def write(self, vals):
        res = super().write(vals)
        if self.env.registry.ready and self._HAVSH_SYNC_FIELDS.intersection(vals):
            self.env['havsh.sync']._havsh_request_sync()
        return res
