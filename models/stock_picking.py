import base64

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import html2plaintext


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    grg_api_uuid = fields.Char('API Request ID', index=True, copy=False, readonly=True)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @api.model
    def _grg_env(self, company_id=None):
        """Return (picking model bound to company, company). Company = user's current company
        unless company_id is given and allowed for the user."""
        user = self.env.user
        if company_id:
            company = self.env['res.company'].browse(int(company_id)).exists()
            if not company or company not in user.company_ids:
                raise AccessError(_('You do not have access to company %s.', company_id))
        else:
            company = self.env.company
        model = self.with_company(company).with_context(allowed_company_ids=[company.id])
        return model, company

    @api.model
    def _grg_source_warehouse(self, company):
        Warehouse = self.env['stock.warehouse']
        wh_id = self.env.user.sudo().grg_source_warehouse_id.id
        wh = Warehouse.browse(wh_id).exists() if wh_id else Warehouse
        if wh and wh.company_id == company:
            return wh
        wh = Warehouse.search([('company_id', '=', company.id), ('grg_is_central', '=', True)], limit=1)
        if not wh:
            wh = Warehouse.search([('company_id', '=', company.id)], order='sequence, id', limit=1)
        if not wh:
            raise UserError(_('No warehouse found for company %s.', company.name))
        return wh

    @api.model
    def _grg_check_admin(self):
        if not self.env.user.has_group('stock.group_stock_manager'):
            raise AccessError(_('Only an Inventory Administrator can do stock transfers.'))

    @api.model
    def _grg_pick_source(self, company, source_warehouse_id=None):
        """Explicit source warehouse if given, else the user's default source."""
        if not source_warehouse_id:
            return self._grg_source_warehouse(company)
        wh = self.env['stock.warehouse'].browse(int(source_warehouse_id)).exists()
        if not wh or wh.company_id != company:
            raise UserError(_('Invalid source warehouse: %s', source_warehouse_id))
        return wh

    @api.model
    def _grg_wh_dict(self, wh):
        partner = wh.partner_id
        return {
            'id': wh.id,
            'name': wh.name,
            'code': wh.code,
            'location_id': wh.lot_stock_id.id,
            'location': wh.lot_stock_id.complete_name,
            'city': partner.city or '',
            'address': ', '.join(filter(None, [partner.street, partner.street2, partner.city, partner.zip])),
        }

    @api.model
    def _grg_product_dict(self, product):
        tmpl = product.product_tmpl_id
        return {
            'id': product.id,
            'name': product.display_name,
            'template_id': tmpl.id,
            'template_name': tmpl.name,
            'variant': ', '.join(product.product_template_attribute_value_ids.mapped('name')),
            'default_code': product.default_code or '',
            'barcode': product.barcode or '',
            'hsn': (tmpl.l10n_in_hsn_code or '') if 'l10n_in_hsn_code' in tmpl._fields else '',
            'uom': product.uom_id.name,
            'qty_on_hand': product.qty_available,
            'qty_available': product.free_qty,   # on hand minus reserved = what you can transfer now
            'sale_price': product.lst_price,
            'cost': product.standard_price,
        }

    @api.model
    def _grg_find_product(self, item, company):
        Product = self.env['product.product']
        base = [('is_storable', '=', True), ('company_id', 'in', [False, company.id])]
        if item.get('product_id'):
            p = Product.search(base + [('id', '=', int(item['product_id']))], limit=1)
            return p, 'product_id %s' % item['product_id']
        if item.get('barcode'):
            p = Product.search(base + [('barcode', '=', str(item['barcode']))], limit=1)
            return p, 'barcode %s' % item['barcode']
        if item.get('default_code'):
            p = Product.search(base + [('default_code', '=', str(item['default_code']))], limit=1)
            return p, 'default_code %s' % item['default_code']
        return Product, 'item without product_id/barcode/default_code'

    # ------------------------------------------------------------------
    # API 1: context (admin only): all warehouses, the admin's own warehouse is default
    # ------------------------------------------------------------------
    @api.model
    def grg_transfer_context(self, company_id=None):
        self._grg_check_admin()
        model, company = self._grg_env(company_id)
        src = model._grg_source_warehouse(company)
        all_wh = model.env['stock.warehouse'].search([('company_id', '=', company.id)], order='sequence, id')
        user = model.env.user
        return {
            'user': {'id': user.id, 'name': user.name, 'login': user.login},
            'company': {'id': company.id, 'name': company.name, 'currency': company.currency_id.name},
            'warehouses': [dict(model._grg_wh_dict(w), default=(w == src)) for w in all_wh],
        }

    # ------------------------------------------------------------------
    # API 2: products with available qty in the source warehouse
    # ------------------------------------------------------------------
    @api.model
    def grg_transfer_products(self, search=None, barcode=None, product_ids=None,
                              in_stock_only=True, limit=50, offset=0, company_id=None,
                              source_warehouse_id=None):
        model, company = self._grg_env(company_id)
        src = model._grg_pick_source(company, source_warehouse_id)
        loc = src.lot_stock_id
        domain = [('is_storable', '=', True), ('company_id', 'in', [False, company.id])]
        if barcode:
            domain += [('barcode', '=', str(barcode))]
        if search:
            domain += ['|', '|', ('name', 'ilike', search), ('default_code', 'ilike', search),
                       ('barcode', 'ilike', search)]
        if product_ids:
            domain += [('id', 'in', [int(i) for i in product_ids])]
        if in_stock_only:
            groups = model.env['stock.quant']._read_group(
                [('location_id', 'child_of', loc.id), ('quantity', '>', 0)],
                ['product_id'], ['quantity:sum', 'reserved_quantity:sum'])
            domain += [('id', 'in', [p.id for p, qty, res in groups if qty - res > 0])]
        Product = model.env['product.product'].with_context(location=loc.id)
        limit = min(int(limit or 50), 500)
        offset = int(offset or 0)
        total = Product.search_count(domain)
        products = Product.search(domain, limit=limit, offset=offset)
        return {
            'source_warehouse': model._grg_wh_dict(src),
            'total': total,
            'limit': limit,
            'offset': offset,
            'products': [model._grg_product_dict(p) for p in products],
        }

    # ------------------------------------------------------------------
    # API 3: transfer a list of products source -> destination in one go (validated)
    # ------------------------------------------------------------------
    @api.model
    def grg_transfer_create(self, dest_warehouse_id, items, note=None, uuid=None,
                            return_pdf=False, company_id=None, source_warehouse_id=None):
        self._grg_check_admin()
        if uuid:
            existing = self.search([('grg_api_uuid', '=', str(uuid))], limit=1)
            if existing:
                return existing._grg_transfer_result(return_pdf, duplicate=True)

        model, company = self._grg_env(company_id)
        src = model._grg_pick_source(company, source_warehouse_id)
        Warehouse = model.env['stock.warehouse']
        dest = Warehouse.browse(int(dest_warehouse_id or 0)).exists()
        if not dest or dest.company_id != company:
            raise UserError(_('Invalid destination warehouse: %s', dest_warehouse_id))
        if dest == src:
            raise UserError(_('Destination warehouse cannot be the same as source (%s).', src.name))
        if not items:
            raise UserError(_('No products to transfer.'))

        # normalise + merge duplicate products
        qty_by_product = {}
        errors = []
        for idx, item in enumerate(items, 1):
            if not isinstance(item, dict):
                errors.append(_('Line %s: invalid item', idx))
                continue
            product, ref = model._grg_find_product(item, company)
            if not product:
                errors.append(_('Line %(idx)s: product not found (%(ref)s)', idx=idx, ref=ref))
                continue
            try:
                qty = float(item.get('qty') or item.get('quantity') or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                errors.append(_('Line %(idx)s: qty must be > 0 for %(p)s', idx=idx, p=product.display_name))
                continue
            qty_by_product[product] = qty_by_product.get(product, 0) + qty
        if errors:
            raise UserError('\n'.join(errors))

        # stock check in source warehouse (free qty = on hand - reserved)
        short = []
        for product, qty in qty_by_product.items():
            avail = product.with_context(location=src.lot_stock_id.id).free_qty
            if product.uom_id.compare(qty, avail) > 0:
                short.append(_('%(p)s: requested %(q)s, available %(a)s',
                               p=product.display_name, q=qty, a=avail))
        if short:
            raise UserError(_('Insufficient stock in %s:\n', src.name) + '\n'.join(short))

        picking_type = src.int_type_id
        if not picking_type:
            raise UserError(_('Warehouse %s has no Internal Transfer operation type.', src.name))

        picking = model.create({
            'picking_type_id': picking_type.id,
            'location_id': src.lot_stock_id.id,
            'location_dest_id': dest.lot_stock_id.id,
            'company_id': company.id,
            'origin': _('Stock Transfer %(s)s → %(d)s', s=src.code, d=dest.code),
            'note': note or False,
            'grg_api_uuid': uuid and str(uuid) or False,
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': qty,
                'location_id': src.lot_stock_id.id,
                'location_dest_id': dest.lot_stock_id.id,
                'company_id': company.id,
                'picking_type_id': picking_type.id,
            }) for product, qty in qty_by_product.items()],
        })
        picking.action_confirm()
        picking.action_assign()
        not_reserved = picking.move_ids.filtered(
            lambda m: m.product_uom.compare(m.quantity, m.product_uom_qty) < 0)
        if not_reserved:
            raise UserError(_('Could not reserve full quantity for: %s',
                              ', '.join(not_reserved.product_id.mapped('display_name'))))
        picking.move_ids.picked = True
        picking.with_context(skip_sms=True, cancel_backorder=True).button_validate()
        if picking.state != 'done':
            raise UserError(_('Transfer %s could not be validated.', picking.name))
        return picking._grg_transfer_result(return_pdf)

    # ------------------------------------------------------------------
    # API 4/5: transfer details, history, PDF
    # ------------------------------------------------------------------
    def _grg_transfer_result(self, return_pdf=False, duplicate=False):
        self.ensure_one()
        lines = self.grg_report_lines()
        res = {
            'transfer_id': self.id,
            'reference': self.name,
            'state': self.state,
            'duplicate': duplicate,
            'date': fields.Datetime.to_string(self.date_done or self.scheduled_date),
            'created_by': self.create_uid.name,
            'source_warehouse': self._grg_wh_dict(self.location_id.warehouse_id) if self.location_id.warehouse_id else None,
            'destination_warehouse': self._grg_wh_dict(self.location_dest_id.warehouse_id) if self.location_dest_id.warehouse_id else None,
            'note': html2plaintext(self.note) if self.note else '',
            'lines': [{k: v for k, v in line.items() if k != 'product'} for line in lines],
            'total_items': len(lines),
            'total_qty': sum(line['qty'] for line in lines),
            'total_value': sum(line['value'] for line in lines),
            'pdf_url': '/stock-transfer/api/transfer/%s/pdf' % self.id,
        }
        if return_pdf:
            res.update(self.grg_transfer_get_pdf())
        return res

    def grg_report_lines(self):
        """Lines used by the API response and the PDF report."""
        self.ensure_one()
        lines = []
        moves = self.move_ids.filtered(lambda m: m.state != 'cancel')
        for move in moves:
            product = move.product_id
            qty = move.quantity if self.state == 'done' else move.product_uom_qty
            tmpl = product.product_tmpl_id
            lines.append({
                'product': product,
                'product_id': product.id,
                'name': product.display_name,
                'default_code': product.default_code or '',
                'barcode': product.barcode or '',
                'hsn': (tmpl.l10n_in_hsn_code or '') if 'l10n_in_hsn_code' in tmpl._fields else '',
                'qty': qty,
                'uom': move.product_uom.name,
                'cost': product.standard_price,
                'value': product.standard_price * qty,
            })
        return lines

    @api.model
    def grg_transfer_get(self, transfer_id, return_pdf=False):
        picking = self.browse(int(transfer_id)).exists()
        if not picking:
            raise UserError(_('Transfer %s not found.', transfer_id))
        return picking._grg_transfer_result(return_pdf)

    @api.model
    def grg_transfer_list(self, limit=20, offset=0, dest_warehouse_id=None,
                          date_from=None, date_to=None, company_id=None):
        model, company = self._grg_env(company_id)
        src = model._grg_source_warehouse(company)
        domain = [('picking_type_id', '=', src.int_type_id.id), ('state', '=', 'done'),
                  ('location_dest_id.warehouse_id', '!=', src.id)]
        if dest_warehouse_id:
            domain += [('location_dest_id.warehouse_id', '=', int(dest_warehouse_id))]
        if date_from:
            domain += [('date_done', '>=', date_from)]
        if date_to:
            domain += [('date_done', '<=', date_to)]
        limit = min(int(limit or 20), 200)
        offset = int(offset or 0)
        total = model.search_count(domain)
        pickings = model.search(domain, limit=limit, offset=offset, order='date_done desc, id desc')
        return {
            'total': total,
            'limit': limit,
            'offset': offset,
            'transfers': [{
                'transfer_id': p.id,
                'reference': p.name,
                'date': fields.Datetime.to_string(p.date_done),
                'destination_warehouse': p.location_dest_id.warehouse_id.name,
                'destination_warehouse_id': p.location_dest_id.warehouse_id.id,
                'created_by': p.create_uid.name,
                'total_items': len(p.move_ids),
                'total_qty': sum(p.move_ids.mapped('quantity')),
                'pdf_url': '/stock-transfer/api/transfer/%s/pdf' % p.id,
            } for p in pickings],
        }

    def grg_transfer_pdf_bytes(self):
        self.ensure_one()
        pdf, _fmt = self.env['ir.actions.report']._render_qweb_pdf(
            'grg_stock_transfer.action_report_grg_transfer', self.ids)
        return pdf

    def grg_transfer_get_pdf(self):
        self.ensure_one()
        return {
            'filename': 'Stock-Transfer-%s.pdf' % (self.name or self.id).replace('/', '-'),
            'pdf_base64': base64.b64encode(self.grg_transfer_pdf_bytes()).decode(),
        }
