import json
import logging

from odoo import http
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

BASE = '/stock-transfer/api'
ROUTE = dict(type='http', auth='bearer', csrf=False, cors='*', save_session=False)


def _truthy(val, default=False):
    if val is None or val == '':
        return default
    if isinstance(val, bool):
        return val
    return str(val).lower() in ('1', 'true', 'yes', 'y')


class GrgStockTransferApi(http.Controller):

    def _model(self):
        return request.env['stock.picking']

    def _company_id(self, data=None):
        data = data or {}
        return (data.get('company_id') or request.params.get('company_id')
                or request.httprequest.headers.get('X-Company-Id') or None)

    def _json_body(self):
        raw = request.httprequest.get_data(as_text=True)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise UserError('Request body is not valid JSON.')

    def _run(self, fn):
        try:
            return request.make_json_response({'ok': True, **fn()})
        except AccessError as e:  # subclass of UserError: must be caught first
            request.env.cr.rollback()
            return request.make_json_response({'ok': False, 'error': str(e.args[0])}, status=403)
        except (UserError, ValidationError, MissingError) as e:
            request.env.cr.rollback()
            return request.make_json_response({'ok': False, 'error': str(e.args[0])}, status=400)

    # 1) logged-in admin, company, all warehouses (admin's own one has default=true)
    @http.route(BASE + '/get-warehouses', methods=['GET'], **ROUTE)
    def get_warehouses(self, **kw):
        return self._run(lambda: self._model().grg_transfer_context(company_id=self._company_id()))

    # 2) products + available qty in source warehouse
    @http.route(BASE + '/products', methods=['GET'], **ROUTE)
    def products(self, search=None, barcode=None, in_stock_only=None, limit=50, offset=0,
                 source_warehouse_id=None, warehouse_id=None, **kw):
        return self._run(lambda: self._model().grg_transfer_products(
            search=search, barcode=barcode, in_stock_only=_truthy(in_stock_only, True),
            limit=limit, offset=offset, company_id=self._company_id(),
            warehouse_id=warehouse_id or source_warehouse_id or None))

    # 3) transfer list of products in one go
    @http.route(BASE + '/transfer', methods=['POST'], **ROUTE)
    def transfer(self, **kw):
        def go():
            data = self._json_body()
            return self._model().grg_transfer_create(
                dest_warehouse_id=data.get('dest_warehouse_id'),
                source_warehouse_id=data.get('source_warehouse_id'),
                items=data.get('items') or [],
                note=data.get('note'),
                uuid=data.get('uuid'),
                return_pdf=_truthy(data.get('return_pdf')),
                company_id=self._company_id(data))
        return self._run(go)

    # 4) history
    @http.route(BASE + '/transfers', methods=['GET'], **ROUTE)
    def transfers(self, limit=20, offset=0, dest_warehouse_id=None, date_from=None, date_to=None, **kw):
        return self._run(lambda: self._model().grg_transfer_list(
            limit=limit, offset=offset, dest_warehouse_id=dest_warehouse_id,
            date_from=date_from, date_to=date_to, company_id=self._company_id()))

    # 5) one transfer
    @http.route(BASE + '/transfer/<int:transfer_id>', methods=['GET'], **ROUTE)
    def transfer_get(self, transfer_id, return_pdf=None, **kw):
        return self._run(lambda: self._model().grg_transfer_get(
            transfer_id, return_pdf=_truthy(return_pdf)))

    # 6) PDF report (open in viewer, or ?download=1)
    @http.route(BASE + '/transfer/<int:transfer_id>/pdf', methods=['GET'], **ROUTE)
    def transfer_pdf(self, transfer_id, download=None, **kw):
        picking = self._model().browse(transfer_id).exists()
        if not picking:
            return request.make_json_response({'ok': False, 'error': 'Transfer not found.'}, status=404)
        try:
            pdf = picking.grg_transfer_pdf_bytes()
        except AccessError as e:
            return request.make_json_response({'ok': False, 'error': str(e.args[0])}, status=403)
        filename = 'Stock-Transfer-%s.pdf' % (picking.name or picking.id).replace('/', '-')
        disposition = 'attachment' if _truthy(download) else 'inline'
        return request.make_response(pdf, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Length', len(pdf)),
            ('Content-Disposition', '%s; filename="%s"' % (disposition, filename)),
        ])
