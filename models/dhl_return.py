from odoo import _, api, fields, models


class DhlReturn(models.Model):
    _name = 'dhl.return'
    _description = 'Return'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(readonly=True, copy=False, default='New')
    state = fields.Selection(
        [('draft', 'Draft'), ('requested', 'Requested'), ('label_created', 'Label Created'),
         ('in_transit', 'In Transit'), ('received', 'Received'), ('completed', 'Completed'),
         ('cancelled', 'Cancelled')], default='draft', tracking=True, required=True, index=True)
    order_id = fields.Many2one('dhl.order', required=True, tracking=True)
    customer_id = fields.Many2one(related='order_id.customer_id', store=True)
    company_id = fields.Many2one(related='order_id.company_id', store=True, index=True)
    shipment_id = fields.Many2one('dhl.shipment', 'Original Shipment',
                                  domain="[('order_id', '=', order_id)]")
    reason = fields.Selection(
        [('damaged', 'Damaged'), ('wrong_item', 'Wrong item'), ('not_needed', 'No longer needed'),
         ('other', 'Other')], default='other', required=True)
    notes = fields.Text()
    return_tracking_number = fields.Char(copy=False)
    return_label = fields.Binary(attachment=True, copy=False)
    return_label_filename = fields.Char(copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('dhl.return') or 'New'
        return super().create(vals_list)

    # Status moves are manual for now; the DHL return label API call is a later feature.
    def action_request(self):
        self.filtered(lambda r: r.state == 'draft').write({'state': 'requested'})

    def action_mark_received(self):
        self.filtered(lambda r: r.state in ('requested', 'label_created', 'in_transit')).write({'state': 'received'})

    def action_complete(self):
        self.filtered(lambda r: r.state == 'received').write({'state': 'completed'})

    def action_cancel(self):
        self.filtered(lambda r: r.state not in ('completed',)).write({'state': 'cancelled'})
