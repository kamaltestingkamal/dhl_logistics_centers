from odoo import SUPERUSER_ID, api, fields, models

from .dhl_utils import body_to_text


class DhlApiLog(models.Model):
    """Append-only API call log. Written through its own cursor so entries
    are kept even when the business transaction rolls back."""
    _name = 'dhl.api.log'
    _description = 'API Log'
    _order = 'id desc'

    name = fields.Char('Operation', required=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, index=True)
    service = fields.Selection([('dhl', 'DHL'), ('amazon', 'Amazon')], default='dhl', required=True)
    connection_id = fields.Many2one('dhl.connection', ondelete='set null')
    method = fields.Char()
    endpoint = fields.Char()
    request_body = fields.Text()
    status_code = fields.Integer('HTTP Status')
    response_body = fields.Text()
    success = fields.Boolean(index=True)
    duration_ms = fields.Integer('Duration (ms)')
    error_message = fields.Text()
    res_model = fields.Char()
    res_id = fields.Integer()

    @api.model
    def log_call(self, **vals):
        if not vals.get('company_id'):
            if vals.get('connection_id'):
                connection = self.env['dhl.connection'].sudo().browse(vals['connection_id'])
                vals['company_id'] = connection.company_id.id or self.env.company.id
            else:
                vals['company_id'] = self.env.company.id
        vals['request_body'] = body_to_text(vals.get('request_body'))
        vals['response_body'] = body_to_text(vals.get('response_body'))
        vals['error_message'] = body_to_text(vals.get('error_message'), limit=2000)
        vals.setdefault('service', 'dhl')
        with self.pool.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            # In test contexts, the connection might not exist in the separate cursor.
            # If foreign key violation occurs, log without the connection reference.
            try:
                return env['dhl.api.log'].create(vals).id
            except Exception as e:
                if 'foreign key constraint' in str(e) and vals.get('connection_id'):
                    # Remove connection_id and retry
                    vals_without_conn = vals.copy()
                    vals_without_conn['connection_id'] = False
                    return env['dhl.api.log'].create(vals_without_conn).id
                raise


class DhlSyncLog(models.Model):
    _name = 'dhl.sync.log'
    _description = 'Sync Log'
    _order = 'id desc'

    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, index=True)
    source = fields.Selection([('amazon', 'Amazon'), ('dhl', 'DHL')], required=True, default='amazon')
    operation = fields.Selection(
        [('orders', 'Order Sync'), ('tracking', 'Tracking Sync'), ('confirmation', 'Shipment Confirmation')],
        default='orders')
    state = fields.Selection([('success', 'Success'), ('partial', 'Partial'), ('failed', 'Failed')],
                             default='success', index=True)
    records_processed = fields.Integer()
    records_failed = fields.Integer()
    message = fields.Text()


class DhlAutomationLog(models.Model):
    _name = 'dhl.automation.log'
    _description = 'Automation Log'
    _order = 'id desc'

    name = fields.Char('Job', required=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, index=True)
    state = fields.Selection([('success', 'Success'), ('partial', 'Partial'), ('failed', 'Failed'), ('skipped', 'Skipped')],
                             default='success', index=True)
    started_at = fields.Datetime(default=fields.Datetime.now)
    finished_at = fields.Datetime()
    message = fields.Text()