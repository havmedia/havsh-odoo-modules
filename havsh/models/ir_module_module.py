# -*- coding: utf-8 -*-
from odoo import api, models


class IrModuleModule(models.Model):
    _inherit = 'ir.module.module'

    # Fields that trigger sync to hav.sh when changed
    _HAVSH_SYNC_FIELDS = {
        'shortdesc',        # module_name
        'name',             # technical_name
        'application',      # application
        'state',            # state
        'author',           # author
        'category_id',      # category
        'installed_version', # version
        'latest_version',   # version
        'write_date',       # updated_at
    }

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Only sync if at least one relevant field is present in any vals dict
        should_sync = any(
            any(field in vals for field in self._HAVSH_SYNC_FIELDS)
            for vals in vals_list
        )
        if should_sync:
            self.env['havsh.sync']._schedule_module_update(records, action='create-module')
        return records

    def write(self, vals):
        res = super().write(vals)
        # Only sync if at least one relevant field is being updated
        if any(field in vals for field in self._HAVSH_SYNC_FIELDS):
            self.env['havsh.sync']._schedule_module_update(self, action='update-module')
        return res