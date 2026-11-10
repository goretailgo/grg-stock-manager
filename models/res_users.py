from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    grg_source_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Transfer Source Warehouse',
        help='Warehouse this user transfers stock FROM via the Stock Transfer API. '
             'Empty = the warehouse marked "Central" in the company, else the first warehouse.')

    grg_pos_config_ids = fields.Many2many(
        'pos.config', 'grg_pos_config_user_rel', 'user_id', 'config_id',
        string='Allowed Point of Sale',
        help='User can only see and open these POS shops (and their sessions, orders and reports). '
             'Leave empty to allow all POS.')

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        if any('grg_pos_config_ids' in vals for vals in vals_list):
            self.env.registry.clear_cache()
        return users

    def write(self, vals):
        res = super().write(vals)
        if 'grg_pos_config_ids' in vals:
            # record rules are cached per user: refresh them
            self.env.registry.clear_cache()
        return res
