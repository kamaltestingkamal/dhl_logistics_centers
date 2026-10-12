from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

ORDER_STATES = [
    ('draft', 'Draft'), ('pending', 'Pending'), ('processing', 'Processing'),
    ('shipped', 'Shipped'), ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'), ('failed', 'Failed'),
]
LIVE_SHIPMENT_STATES = ('created', 'in_transit', 'delivered', 'delivery_failed')


class DhlOrder(models.Model):
    _name = 'dhl.order'
    _description = 'Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(readonly=True, copy=False, default='New', index=True)
    state = fields.Selection(ORDER_STATES, default='draft', required=True, tracking=True, index=True, copy=False)
    source = fields.Selection([('manual', 'Manual'), ('amazon', 'Amazon')], default='manual', required=True)
    amazon_order_id = fields.Char('Amazon Order ID', copy=False, index=True)
    marketplace = fields.Char()
    customer_id = fields.Many2one('dhl.customer', required=True, tracking=True, index=True)
    address_id = fields.Many2one('dhl.customer.address', 'Shipping Address',
                                 domain="[('customer_id', '=', customer_id)]", tracking=True)
    order_date = fields.Datetime(default=fields.Datetime.now)
    line_ids = fields.One2many('dhl.order.line', 'order_id', 'Lines', copy=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    amount_total = fields.Monetary(compute='_compute_totals', store=True)
    total_weight = fields.Float('Weight (kg)', compute='_compute_totals', store=True, digits=(10, 3))
    notes = fields.Text()
    shipment_ids = fields.One2many('dhl.shipment', 'order_id')
    shipment_count = fields.Integer(compute='_compute_shipment_count')

    @api.depends('line_ids.subtotal', 'line_ids.weight')
    def _compute_totals(self):
        for rec in self:
            rec.amount_total = sum(rec.line_ids.mapped('subtotal'))
            rec.total_weight = sum(rec.line_ids.mapped('weight'))

    def _compute_shipment_count(self):
        data = dict(self.env['dhl.shipment']._read_group([('order_id', 'in', self.ids)], ['order_id'], ['__count']))
        for rec in self:
            rec.shipment_count = data.get(rec, 0)

    @api.constrains('amazon_order_id')
    def _check_amazon_order_unique(self):
        for rec in self.filtered('amazon_order_id'):
            if self.search_count([('amazon_order_id', '=', rec.amazon_order_id), ('id', '!=', rec.id)]):
                raise ValidationError(_("Amazon order %s has already been imported.", rec.amazon_order_id))

    @api.onchange('customer_id')
    def _onchange_customer_id(self):
        self.address_id = self.customer_id._get_default_address() if self.customer_id else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('dhl.order') or 'New'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_confirm(self):
        for rec in self:
            if not rec.line_ids:
                raise UserError(_("Order %s has no lines.", rec.name))
            if not rec.address_id:
                raise UserError(_("Order %s needs a shipping address.", rec.name))
        self.filtered(lambda o: o.state == 'draft').write({'state': 'pending'})

    def action_start_processing(self):
        self.filtered(lambda o: o.state == 'pending').write({'state': 'processing'})

    def action_cancel(self):
        for rec in self:
            if rec.shipment_ids.filtered(lambda s: s.state in LIVE_SHIPMENT_STATES or s.creation_result_unknown):
                raise UserError(_(
                    "Order %s has a shipment already created at DHL. Cancel that shipment first.", rec.name))
            rec.shipment_ids.filtered(lambda s: s.state not in ('cancelled',)).action_cancel()
        self.write({'state': 'cancelled'})

    def action_reset_draft(self):
        self.filtered(lambda o: o.state in ('cancelled', 'failed')).write({'state': 'draft'})

    def action_create_shipment(self):
        self.ensure_one()
        if self.state in ('cancelled', 'delivered', 'draft'):
            raise UserError(_("Confirm the order first (current status: %s).", dict(ORDER_STATES)[self.state]))
        if not self.address_id:
            raise UserError(_("Please select a shipping address first."))
        active = self.shipment_ids.filtered(
            lambda s: s.state not in ('cancelled', 'creation_failed') or s.creation_result_unknown)
        if active:
            raise UserError(_("%(order)s already has an active shipment (%(ship)s).",
                              order=self.name, ship=', '.join(active.mapped('name'))))
        weight = self.total_weight
        if weight <= 0:
            missing = self.line_ids.filtered(lambda line: line.product_id.weight <= 0).mapped('product_id.name')
            raise UserError(_("Shipment blocked: package weight is missing for %(products)s. Add the actual weight before booking.",
                              products=', '.join(missing) or _('order products'))) 
        shipper_country = self.env['dhl.shipment']._get_shipper_defaults().get('shipper_country_id')
        product_code = 'V01PAK' if self.address_id.country_id.id == shipper_country else 'V53WPAK'
        vals = dict(self.address_id._to_shipment_vals(), order_id=self.id, address_id=self.address_id.id,
                    product_code=product_code,
                    package_ids=[(0, 0, {'name': 'Package 1', 'weight': weight})])
        shipment = self.env['dhl.shipment'].create(vals)
        if self.state == 'pending':
            self.state = 'processing'
        # Mark ready and attempt DHL creation
        shipment.action_mark_ready()
        try:
            shipment.action_create_dhl_shipment()
        except Exception as exc:
            # Don't fail the entire action if DHL API fails
            self.env['dhl.notification'].notify(
                'warning', _('DHL Shipment Creation'),
                _("%(name)s was created locally but DHL API call failed: %(error)s",
                  name=shipment.name, error=str(exc)),
                record=shipment)
        return {'type': 'ir.actions.act_window', 'res_model': 'dhl.shipment', 'res_id': shipment.id,
                'views': [(False, 'form')], 'target': 'current'}

    def action_view_shipments(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Shipments'), 'res_model': 'dhl.shipment',
                'view_mode': 'list,form', 'domain': [('order_id', '=', self.id)]}

    def _sync_from_shipments(self):
        """Derive order status from its shipments (called after tracking updates)."""
        for rec in self.filtered(lambda o: o.state not in ('draft', 'cancelled')):
            ships = rec.shipment_ids.filtered(lambda s: s.state != 'cancelled')
            if not ships:
                continue
            states = set(ships.mapped('state'))
            if states == {'delivered'}:
                new = 'delivered'
            elif states & {'created', 'in_transit', 'delivered', 'delivery_failed'}:
                new = 'shipped'
            elif states == {'creation_failed'}:
                new = 'failed'
            else:
                continue
            if new != rec.state:
                rec.state = new


class DhlOrderLine(models.Model):
    _name = 'dhl.order.line'
    _description = 'Order Line'

    order_id = fields.Many2one('dhl.order', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='order_id.company_id', store=True, index=True)
    product_id = fields.Many2one('dhl.product', required=True)
    name = fields.Char('Description')
    quantity = fields.Float(default=1.0, digits=(10, 2))
    currency_id = fields.Many2one(related='order_id.currency_id')
    unit_price = fields.Monetary(currency_field='currency_id')
    subtotal = fields.Monetary(compute='_compute_subtotal', store=True, currency_field='currency_id')
    weight = fields.Float('Weight (kg)', compute='_compute_weight', store=True, digits=(10, 3))

    @api.depends('quantity', 'unit_price')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.unit_price

    @api.depends('quantity', 'product_id.weight')
    def _compute_weight(self):
        for line in self:
            line.weight = line.quantity * line.product_id.weight

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id:
            self.name = self.product_id.name
            self.unit_price = self.product_id.price

    @api.constrains('quantity')
    def _check_quantity(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(_("Quantity must be greater than zero."))
