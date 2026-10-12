import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class DhlCustomer(models.Model):
    _name = 'dhl.customer'
    _description = 'Customer'
    _order = 'name'

    name = fields.Char(required=True, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    company_name = fields.Char()
    email = fields.Char()
    phone = fields.Char()
    amazon_buyer_ref = fields.Char('Amazon Buyer Ref', copy=False, index=True)
    source = fields.Selection([('manual', 'Manual'), ('amazon', 'Amazon')], default='manual', required=True, index=True)
    marketplace = fields.Char(index=True, copy=False)
    last_sync_at = fields.Datetime(copy=False, readonly=True)
    notes = fields.Text()
    active = fields.Boolean(default=True)
    address_ids = fields.One2many('dhl.customer.address', 'customer_id', 'Addresses')
    order_ids = fields.One2many('dhl.order', 'customer_id')
    order_count = fields.Integer(compute='_compute_counts')
    shipment_count = fields.Integer(compute='_compute_counts')

    @api.constrains('email')
    def _check_email_format(self):
        for rec in self.filtered('email'):
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", rec.email.strip()):
                raise ValidationError(_("Please enter a valid customer email address."))

    def _compute_counts(self):
        orders = dict(self.env['dhl.order']._read_group([('customer_id', 'in', self.ids)], ['customer_id'], ['__count']))
        ships = dict(self.env['dhl.shipment']._read_group([('customer_id', 'in', self.ids)], ['customer_id'], ['__count']))
        for rec in self:
            rec.order_count = orders.get(rec, 0)
            rec.shipment_count = ships.get(rec, 0)

    def _get_default_address(self):
        self.ensure_one()
        return self.address_ids.filtered('is_default')[:1] or self.address_ids[:1]

    def action_view_orders(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': 'Order History', 'res_model': 'dhl.order',
                'view_mode': 'list,form', 'domain': [('customer_id', '=', self.id)],
                'context': {'default_customer_id': self.id}}

    def action_view_shipments(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': 'Shipment History', 'res_model': 'dhl.shipment',
                'view_mode': 'list,form', 'domain': [('customer_id', '=', self.id)]}


class DhlCustomerAddress(models.Model):
    _name = 'dhl.customer.address'
    _description = 'Customer Address'
    _order = 'customer_id, is_default desc, id'

    customer_id = fields.Many2one('dhl.customer', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='customer_id.company_id', store=True, index=True)
    label = fields.Char(help='e.g. Home, Office')
    address_type = fields.Selection([('shipping', 'Shipping'), ('billing', 'Billing'), ('return', 'Return')],
                                    default='shipping', required=True)
    is_default = fields.Boolean('Default')
    contact_name = fields.Char(help='Recipient name printed on the label. Defaults to the customer name.')
    company_name = fields.Char()
    # DHL Parcel DE expects street name and house number separately.
    street = fields.Char('Street', required=True)
    street_number = fields.Char('House No.')
    address_line2 = fields.Char('Address Line 2')
    zip = fields.Char('Postal Code', required=True)
    city = fields.Char(required=True)
    state_name = fields.Char('State / Region')
    country_id = fields.Many2one('res.country', required=True)
    email = fields.Char()
    phone = fields.Char()
    validation_state = fields.Selection(
        [('not_validated', 'Not validated'), ('valid', 'Valid'), ('invalid', 'Invalid')],
        default='not_validated', readonly=True, copy=False)

    @api.depends('label', 'street', 'street_number', 'zip', 'city', 'country_id')
    def _compute_display_name(self):
        for rec in self:
            line = ' '.join(filter(None, [rec.street, rec.street_number]))
            place = ' '.join(filter(None, [rec.zip, rec.city]))
            rec.display_name = ', '.join(filter(None, [rec.label, line, place, rec.country_id.code]))

    @api.constrains('email')
    def _check_email_format(self):
        for rec in self.filtered('email'):
            email = rec.email.strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                raise ValidationError(_("Please enter a valid email address."))

    @api.constrains('street', 'zip', 'city', 'country_id')
    def _check_address_fields(self):
        for rec in self:
            if not rec.street.strip() or not rec.zip.strip() or not rec.city.strip() or not rec.country_id:
                raise ValidationError(_("Street, postal code, city and country are required."))

    def write(self, vals):
        address_fields = {'street', 'street_number', 'address_line2', 'zip', 'city', 'country_id'}
        if address_fields & set(vals) and 'validation_state' not in vals:
            vals = dict(vals, validation_state='not_validated')
        return super().write(vals)

    def _to_shipment_vals(self):
        self.ensure_one()
        return {
            'consignee_name': self.contact_name or self.customer_id.name,
            'consignee_company': self.company_name or self.customer_id.company_name,
            'consignee_street': self.street,
            'consignee_street_number': self.street_number,
            'consignee_address_line2': self.address_line2,
            'consignee_zip': self.zip,
            'consignee_city': self.city,
            'consignee_country_id': self.country_id.id,
            'consignee_email': self.email or self.customer_id.email,
            'consignee_phone': self.phone or self.customer_id.phone,
        }
