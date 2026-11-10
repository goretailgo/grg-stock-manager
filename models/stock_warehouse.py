from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    grg_is_central = fields.Boolean(
        string='Central Warehouse',
        help='Default source warehouse for the Stock Transfer API '
             '(used when the API user has no Transfer Source Warehouse set).')
