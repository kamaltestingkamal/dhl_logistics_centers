import logging
import re
import time

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .dhl_utils import (
    EU_COUNTRIES, DHLAPIError, body_to_text, classify_http_status, extract_dhl_error, to_iso3)

_logger = logging.getLogger(__name__)

# DHL Parcel DE shipment creation endpoint (API v2)
# VERIFY against current DHL developer portal before production
SHIPMENT_URLS = {
    'sandbox': 'https://api-sandbox.dhl.com/parcel/de/shipping/v2/orders',
    'production': 'https://api-eu.dhl.com/parcel/de/shipping/v2/orders',
}
REQUEST_TIMEOUT = 20
SHIPMENT_PROFILE = 'STANDARD_GRUPPENPROFIL'  # DHL default user-group profile
# Billing number = EKP (10 digits) + procedure (2) + participation (2). The procedure depends on the product.
PRODUCT_PROCEDURE = {'V01PAK': '01', 'V53WPAK': '53'}
# Product availability: V01PAK is domestic Germany only, V53WPAK is international.
PRODUCT_COUNTRY_RESTRICTIONS = {
    'V01PAK': {'allowed_destinations': ['DE']},  # Only Germany
    'V53WPAK': {'allowed_destinations': None},  # International (check DHL Business Customer Portal)
}
# Dimension limits (in cm) per product type. Values based on DHL documentation.
# V01PAK (domestic): max girth 400 cm, max weight 31.5 kg
# V53WPAK (international): max girth varies by destination - use conservative default
PRODUCT_DIMENSION_LIMITS = {
    'V01PAK': {
        'max_girth_cm': 400,
        'max_weight_kg': 31.5,
        'max_length_cm': 200,
        'max_width_cm': 100,
        'max_height_cm': 100,
    },
    'V53WPAK': {
        'max_girth_cm': 300,  # Conservative limit for international - verify in DHL portal
        'max_weight_kg': 31.5,
        'max_length_cm': 120,
        'max_width_cm': 60,
        'max_height_cm': 60,
    },
}

SHIPMENT_STATES = [
    ('draft', 'Draft'), ('ready', 'Ready'), ('validating', 'Validating'),
    ('created', 'Created'), ('in_transit', 'In Transit'), ('delivered', 'Delivered'),
    ('creation_failed', 'Creation Failed'), ('delivery_failed', 'Delivery Failed'),
    ('cancelled', 'Cancelled'),
]
INACTIVE_STATES = ('cancelled', 'creation_failed')
# NOTE: the public DHL tracking page does not know sandbox parcels, so no URL is shown for sandbox.
TRACKING_URL = 'https://www.dhl.de/en/privatkunden/dhl-sendungsverfolgung.html?piececode=%s'


class DhlShipment(models.Model):
    _name = 'dhl.shipment'
    _description = 'Shipment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(readonly=True, copy=False, default='New', index=True)
    state = fields.Selection(SHIPMENT_STATES, default='draft', required=True, tracking=True, index=True, copy=False)
    order_id = fields.Many2one('dhl.order', ondelete='restrict', index=True, tracking=True)
    customer_id = fields.Many2one('dhl.customer', related='order_id.customer_id', store=True, index=True)
    address_id = fields.Many2one('dhl.customer.address', 'Delivery Address',
                                 domain="[('customer_id', '=', customer_id)]")
    connection_id = fields.Many2one(
        'dhl.connection', default=lambda self: self.env['dhl.connection'].get_active().id)
    environment = fields.Selection(related='connection_id.environment')
    product_code = fields.Selection(
        [('V01PAK', 'DHL Paket (V01PAK)'), ('V53WPAK', 'DHL Paket International (V53WPAK)')],
        default='V01PAK', required=True, string='DHL Product')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)

    # Shipper (snapshot of Settings at creation time)
    shipper_name = fields.Char()
    shipper_company = fields.Char()
    shipper_street = fields.Char()
    shipper_street_number = fields.Char()
    shipper_zip = fields.Char()
    shipper_city = fields.Char()
    shipper_country_id = fields.Many2one('res.country')
    shipper_email = fields.Char()
    shipper_phone = fields.Char()

    # Consignee
    consignee_name = fields.Char()
    consignee_company = fields.Char()
    consignee_street = fields.Char()
    consignee_street_number = fields.Char()
    consignee_address_line2 = fields.Char()
    consignee_zip = fields.Char()
    consignee_city = fields.Char()
    consignee_country_id = fields.Many2one('res.country')
    consignee_email = fields.Char()
    consignee_phone = fields.Char()

    package_ids = fields.One2many('dhl.package', 'shipment_id', 'Packages', copy=True)
    package_count = fields.Integer(compute='_compute_packages', store=True)
    total_weight = fields.Float('Total Weight (kg)', compute='_compute_packages', store=True, digits=(10, 3))

    tracking_number = fields.Char(copy=False, index=True, tracking=True)
    dhl_shipment_no = fields.Char('DHL Shipment No.', copy=False, readonly=True)
    label = fields.Binary(attachment=True, copy=False)
    label_filename = fields.Char(copy=False)
    tracking_url = fields.Char(compute='_compute_tracking_url')
    shipping_cost = fields.Monetary(currency_field='currency_id')
    error_message = fields.Text(copy=False, readonly=True)
    creation_result_unknown = fields.Boolean(
        string='DHL Booking Result Unknown', default=False, copy=False, index=True,
        help='The booking request may have reached DHL, but the response could not confirm the result. Do not retry until reconciled.')
    tracking_ids = fields.One2many('dhl.tracking', 'shipment_id', 'Tracking Events')
    last_tracking_text = fields.Char(readonly=True, copy=False)
    last_tracking_sync = fields.Datetime(readonly=True, copy=False)
    delivered_at = fields.Datetime('Delivered At', readonly=True, copy=False, help='Timestamp when shipment was marked as delivered by DHL')

    # ------------------------------------------------------------------
    @api.depends('package_ids.weight')
    def _compute_packages(self):
        for rec in self:
            rec.package_count = len(rec.package_ids)
            rec.total_weight = sum(rec.package_ids.mapped('weight'))

    @api.depends('tracking_number', 'environment')
    def _compute_tracking_url(self):
        for rec in self:
            rec.tracking_url = (TRACKING_URL % rec.tracking_number
                                if rec.tracking_number and rec.environment != 'sandbox' else False)

    @api.model
    def _get_shipper_defaults(self):
        param = self.env['ir.config_parameter'].sudo().get_param
        country = self.env['res.country'].search(
            [('code', '=', (param('dhl_logistics_center.shipper_country_code') or 'DE').upper())], limit=1)
        return {
            'shipper_name': param('dhl_logistics_center.shipper_name'),
            'shipper_company': param('dhl_logistics_center.shipper_company'),
            'shipper_street': param('dhl_logistics_center.shipper_street'),
            'shipper_street_number': param('dhl_logistics_center.shipper_street_number'),
            'shipper_zip': param('dhl_logistics_center.shipper_zip'),
            'shipper_city': param('dhl_logistics_center.shipper_city'),
            'shipper_country_id': country.id,
            'shipper_email': param('dhl_logistics_center.shipper_email'),
            'shipper_phone': param('dhl_logistics_center.shipper_phone'),
        }

    @api.model_create_multi
    def create(self, vals_list):
        if (not self.env.su and not self.env.user.has_group('dhl_logistics_center.group_dhl_manager')
                and any(vals.get('creation_result_unknown') for vals in vals_list)):
            raise AccessError(_('Only a DHL manager or the internal booking workflow can set an unknown booking result.'))
        defaults = self._get_shipper_defaults()
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('dhl.shipment') or 'New'
            for key, value in defaults.items():
                vals.setdefault(key, value)
        # Friendly duplicate protection BEFORE the DB unique index can raise a raw IntegrityError.
        seen = set()
        for vals in vals_list:
            order_id = vals.get('order_id')
            if (not order_id or (vals.get('state', 'draft') in INACTIVE_STATES
                                 and not vals.get('creation_result_unknown'))):
                continue
            if order_id in seen:
                raise ValidationError(_("Two active shipments for the same order cannot be created at once."))
            seen.add(order_id)
            self._raise_if_other_active(order_id)
        return super().create(vals_list)

    def write(self, vals):
        if ('creation_result_unknown' in vals and not self.env.su
                and not self.env.user.has_group('dhl_logistics_center.group_dhl_manager')):
            raise AccessError(_('Only a DHL manager can change the unknown-booking safety flag.'))
        new_state = vals.get('state')
        if new_state and new_state not in INACTIVE_STATES:
            for rec in self.filtered(lambda s: s.order_id and s.state in INACTIVE_STATES):
                self._raise_if_other_active(rec.order_id.id, exclude=rec.id)
        return super().write(vals)

    @api.model
    def _raise_if_other_active(self, order_id, exclude=None):
        domain = [('order_id', '=', order_id), '|', ('state', 'not in', INACTIVE_STATES),
                  ('creation_result_unknown', '=', True)]
        if exclude:
            domain.append(('id', '!=', exclude))
        others = self.sudo().search(domain)
        if others:
            raise ValidationError(_(
                "Order %(order)s already has an active shipment (%(ship)s). "
                "Cancel it first to avoid a duplicate DHL shipment.",
                order=others[0].order_id.name, ship=', '.join(others.mapped('name'))))

    def _auto_init(self):
        """Create partial unique index for duplicate protection at DB level."""
        res = super()._auto_init()
        # Preserve the original index and add a stronger predicate for uncertain booking outcomes.
        # A versioned index avoids dropping/rebuilding the existing index on every registry init.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS dhl_shipment_order_active_unique_v2
            ON dhl_shipment (order_id)
            WHERE state NOT IN ('cancelled', 'creation_failed') OR creation_result_unknown = TRUE
        """)
        return res

    @api.onchange('address_id')
    def _onchange_address_id(self):
        if self.address_id:
            for key, value in self.address_id._to_shipment_vals().items():
                self[key] = value

    # ------------------------------------------------------------------
    # Duplicate protection
    # ------------------------------------------------------------------
    @api.constrains('order_id', 'state')
    def _check_single_active_shipment(self):
        """Python-level duplicate protection. DB-level unique index provides race condition safety."""
        for rec in self.filtered('order_id'):
            if rec.state in INACTIVE_STATES and not rec.creation_result_unknown:
                continue
            others = self.search([('order_id', '=', rec.order_id.id), ('id', '!=', rec.id),
                                  '|', ('state', 'not in', INACTIVE_STATES),
                                  ('creation_result_unknown', '=', True)])
            if others:
                raise ValidationError(_(
                    "Order %(order)s already has an active shipment (%(ship)s). "
                    "Cancel or mark it as creation_failed before creating a new one.",
                    order=rec.order_id.name, ship=', '.join(others.mapped('name'))))

    @api.constrains('tracking_number')
    def _check_tracking_unique(self):
        for rec in self.filtered('tracking_number'):
            if self.search_count([('tracking_number', '=', rec.tracking_number), ('id', '!=', rec.id)]):
                raise ValidationError(_("Tracking number %s is already used by another shipment.", rec.tracking_number))

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _missing_data(self):
        self.ensure_one()
        required = {
            'shipper_name': _('Shipper name'), 'shipper_street': _('Shipper street'),
            'shipper_zip': _('Shipper postal code'), 'shipper_city': _('Shipper city'),
            'shipper_country_id': _('Shipper country'),
            'consignee_name': _('Consignee name'), 'consignee_street': _('Consignee street'),
            'consignee_zip': _('Consignee postal code'), 'consignee_city': _('Consignee city'),
            'consignee_country_id': _('Consignee country'),
        }
        missing = [label for f, label in required.items() if not self[f]]
        for prefix, country_field, number_field in (
                ('Shipper', 'shipper_country_id', 'shipper_street_number'),
                ('Consignee', 'consignee_country_id', 'consignee_street_number')):
            if self[country_field] and self[country_field].code == 'DE' and not self[number_field]:
                missing.append(_('%s house number') % prefix)
        if not self.package_ids:
            missing.append(_('At least one package'))
        elif any(p.weight <= 0 for p in self.package_ids):
            missing.append(_('Weight > 0 on every package'))
        return missing

    def action_mark_ready(self):
        for rec in self:
            if rec.state != 'draft':
                continue
            missing = rec._missing_data()
            if missing:
                raise UserError(_("%(name)s cannot be marked ready. Missing:\n- %(items)s",
                                  name=rec.name, items='\n- '.join(missing)))
            rec.state = 'ready'
            missing_hs = rec._customs_missing_hs()
            if missing_hs:
                self.env['dhl.notification'].notify(
                    'warning', _('HS code missing'),
                    _("%(name)s goes outside the EU. HS code missing for: %(products)s. "
                      "DHL accepts it, but customs clearance may be slower.",
                      name=rec.name, products=', '.join(missing_hs)),
                    record=rec)

    def action_create_at_dhl(self):
        """Create shipment at DHL - wrapper for UI button."""
        self.action_mark_ready()
        return self.action_create_dhl_shipment()

    def action_reset_draft(self):
        for rec in self.filtered(lambda s: s.state in ('ready', 'creation_failed', 'delivery_failed', 'cancelled')):
            if rec.creation_result_unknown:
                raise UserError(_(
                    "%s may already exist at DHL, but the response was inconclusive. Check the DHL portal and use "
                    "'Confirm No DHL Booking' only after verifying that no booking exists.", rec.name))
            if rec.dhl_shipment_no:
                raise UserError(_(
                    "%(name)s already exists at DHL (%(no)s). Creating it again would book a second parcel.",
                    name=rec.name, no=rec.dhl_shipment_no))
        self.filtered(lambda s: s.state in ('ready', 'creation_failed', 'delivery_failed', 'cancelled')).write(
            {'state': 'draft', 'error_message': False})

    def action_confirm_no_dhl_booking(self):
        """Manager-only acknowledgement after checking the DHL portal for an uncertain booking."""
        if not self.env.user.has_group('dhl_logistics_center.group_dhl_manager'):
            raise UserError(_('Only a DHL manager can resolve an uncertain booking.'))
        for rec in self:
            if not rec.creation_result_unknown:
                continue
            if rec.state != 'creation_failed':
                raise UserError(_('Only a failed, uncertain DHL booking can be manually reconciled.'))
            if rec.dhl_shipment_no:
                raise UserError(_('A DHL shipment number is already stored; this record cannot be cleared as unbooked.'))
            rec.write({'creation_result_unknown': False})
            self.env['dhl.notification'].notify(
                'warning', _('DHL Booking Manually Reconciled'),
                _('%s was marked as not booked after a manager checked the DHL portal.', rec.name), record=rec)
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def action_mark_creation_failed(self, error_message, result_unknown=False):
        """Mark a failed booking; uncertain remote outcomes stay active for duplicate protection."""
        self.ensure_one()
        safe_error = body_to_text(error_message, limit=2000)
        self.sudo().write({
            'state': 'creation_failed',
            'error_message': safe_error,
            'creation_result_unknown': bool(result_unknown),
        })
        title = _('DHL Booking Result Unknown') if result_unknown else _('Shipment Creation Failed')
        body = (_("%(name)s may already exist at DHL. Do not retry until the DHL portal is checked. %(error)s",
                  name=self.name, error=safe_error) if result_unknown else
                _("%(name)s could not be created at DHL: %(error)s", name=self.name, error=safe_error))
        self.env['dhl.notification'].notify('error', title, body, record=self)

    def action_cancel(self):
        for rec in self:
            if rec.creation_result_unknown:
                raise UserError(_('%s has an uncertain DHL booking result. Reconcile it before changing its status.', rec.name))
            if rec.state == 'validating':
                raise UserError(_(
                    "%s is being created at DHL right now. Wait for the result before cancelling.", rec.name))
            if rec.state in ('created', 'in_transit', 'delivered', 'delivery_failed'):
                raise UserError(_(
                    "%s already exists at DHL. Cancelling it requires the DHL cancel API call, "
                    "which is not implemented yet.", rec.name))
        self.write({'state': 'cancelled'})

    # ------------------------------------------------------------------
    # DHL API Integration (Parcel DE Shipping v2, POST /orders)
    # ------------------------------------------------------------------
    def _get_billing_number(self):
        """Billing number for this product: EKP + procedure(product) + participation."""
        self.ensure_one()
        number = (self.connection_id.sudo().billing_number or '').strip()
        procedure = PRODUCT_PROCEDURE.get(self.product_code)
        if len(number) == 14 and number.isdigit() and procedure:
            return number[:10] + procedure + number[12:]
        return number

    def _address_block(self, prefix, with_phone=True):
        """Build a DHL contact address (shipper or consignee). Country must be ISO alpha-3."""
        self.ensure_one()
        country = self[f'{prefix}_country_id']
        iso3 = to_iso3(country.code)
        if not iso3:
            raise ValidationError(_("Country %s is not supported by the DHL integration yet.", country.display_name))
        name = self[f'{prefix}_name'] or ''
        company = self[f'{prefix}_company'] or ''
        block = {
            'name1': (company or name)[:50],
            'addressStreet': (self[f'{prefix}_street'] or '')[:50],
            'postalCode': self[f'{prefix}_zip'] or '',
            'city': (self[f'{prefix}_city'] or '')[:40],
            'country': iso3,
        }
        if company and name:
            block['name2'] = name[:50]
        if self[f'{prefix}_street_number']:
            block['addressHouse'] = self[f'{prefix}_street_number'][:10]
        line2 = getattr(self, f'{prefix}_address_line2', False)
        if line2:
            block['additionalAddressInformation1'] = line2[:60]
        if self[f'{prefix}_email']:
            block['email'] = self[f'{prefix}_email'][:80]
        # DHL: for shipments within Germany the consignee phone number cannot be transmitted.
        if with_phone and self[f'{prefix}_phone']:
            block['phone'] = self[f'{prefix}_phone'][:20]
        return block

    def _needs_customs(self):
        """Customs declaration is needed when the parcel leaves the EU customs area."""
        self.ensure_one()
        dest = self.consignee_country_id.code
        return bool(dest) and dest not in EU_COUNTRIES

    def _customs_missing_hs(self):
        self.ensure_one()
        if not self._needs_customs() or not self.order_id:
            return []
        return [line.product_id.name for line in self.order_id.line_ids
                if len(re.sub(r'\D', '', line.product_id.hs_code or '')) < 6]

    def _validate_product_for_destination(self):
        """Validate that the selected DHL product is available for the destination country."""
        self.ensure_one()
        product_code = self.product_code
        dest_country = self.consignee_country_id.code
        restrictions = PRODUCT_COUNTRY_RESTRICTIONS.get(product_code, {})

        if restrictions.get('allowed_destinations'):
            allowed = restrictions['allowed_destinations']
            if dest_country not in allowed:
                raise ValidationError(_(
                    "Product %(product)s is not available for destination %(country)s. "
                    "This product is only valid for: %(allowed)s. "
                    "Please select an appropriate international product for this destination.",
                    product=product_code, country=dest_country or 'Unknown',
                    allowed=', '.join(allowed)))

    def _validate_billing_number_procedure(self):
        """Validate that the billing number procedure matches the selected product."""
        self.ensure_one()
        billing_number = (self.connection_id.sudo().billing_number or '').strip()
        if len(billing_number) == 14 and billing_number.isdigit():
            actual_procedure = billing_number[10:12]
            expected_procedure = PRODUCT_PROCEDURE.get(self.product_code)
            if expected_procedure and actual_procedure != expected_procedure:
                raise ValidationError(_(
                    "Billing number procedure mismatch. The billing number procedure is '%(actual)s' "
                    "but product %(product)s requires procedure '%(expected)s'. "
                    "Please update the billing number or select a different product.",
                    actual=actual_procedure, product=self.product_code, expected=expected_procedure))

    def _validate_package_dimensions(self):
        """Validate package dimensions against DHL limits for the selected product."""
        self.ensure_one()
        package = self.package_ids[0]
        limits = PRODUCT_DIMENSION_LIMITS.get(self.product_code, {})

        # Check if dimensions are provided
        has_dimensions = bool(package.length and package.width and package.height)

        if has_dimensions:
            # Calculate girth: longest side + 2 * (other side 1 + other side 2)
            dims = sorted([package.length, package.width, package.height], reverse=True)
            girth_cm = dims[0] + 2 * (dims[1] + dims[2])

            # Validate girth
            max_girth = limits.get('max_girth_cm')
            if max_girth and girth_cm > max_girth:
                raise ValidationError(_(
                    "Package dimensions exceed the maximum girth for product %(product)s. "
                    "Entered dimensions: %(l)s x %(w)s x %(h)s cm. "
                    "Calculated girth: %(girth)s cm. Maximum allowed girth: %(max)s cm.",
                    product=self.product_code, l=package.length, w=package.width, h=package.height,
                    girth=girth_cm, max=max_girth))

            # Validate individual dimensions
            max_length = limits.get('max_length_cm')
            max_width = limits.get('max_width_cm')
            max_height = limits.get('max_height_cm')

            if max_length and package.length > max_length:
                raise ValidationError(_(
                    "Package length %(length)s cm exceeds the maximum %(max)s cm for product %(product)s.",
                    length=package.length, max=max_length, product=self.product_code))

            if max_width and package.width > max_width:
                raise ValidationError(_(
                    "Package width %(width)s cm exceeds the maximum %(max)s cm for product %(product)s.",
                    width=package.width, max=max_width, product=self.product_code))

            if max_height and package.height > max_height:
                raise ValidationError(_(
                    "Package height %(height)s cm exceeds the maximum %(max)s cm for product %(product)s.",
                    height=package.height, max=max_height, product=self.product_code))

        # Validate weight
        max_weight = limits.get('max_weight_kg')
        if max_weight and package.weight > max_weight:
            raise ValidationError(_(
                "Package weight %(weight)s kg exceeds the maximum %(max)s kg for product %(product)s.",
                weight=package.weight, max=max_weight, product=self.product_code))

    def _validate_weight_against_customs(self):
        """Validate that package weight matches or exceeds customs declaration weight."""
        self.ensure_one()
        if not self._needs_customs() or not self.order_id:
            return

        package = self.package_ids[0]
        customs_weight = sum(line.product_id.weight * line.quantity for line in self.order_id.line_ids)

        if package.weight < customs_weight:
            raise ValidationError(_(
                "Package weight (%(pkg_weight)s kg) is smaller than the total weight of items "
                "in the customs declaration (%(customs_weight)s kg). The package weight must be "
                "equal to or greater than the total item weight. Please increase the package weight.",
                pkg_weight=package.weight, customs_weight=round(customs_weight, 3)))

    def _build_customs(self):
        """`customs` block of the DHL request, built from the order lines (nothing is typed twice).

        Currency: DHL requires postal charges and all items in the SAME currency; the order currency is used.
        """
        self.ensure_one()
        order = self.order_id
        if not order or not order.line_ids:
            raise ValidationError(_(
                "%(country)s is outside the EU, so DHL needs a customs declaration. "
                "Create the shipment from an order with products.", country=self.consignee_country_id.display_name))
        currency = order.currency_id.name
        items = []
        for line in order.line_ids:
            product = line.product_id
            unit_grams = int(round(product.weight * 1000))
            quantity = int(round(line.quantity))
            if unit_grams <= 0:
                raise ValidationError(_("Product %s has no weight. Customs needs the weight of every item.", product.name))
            if quantity < 1 or abs(line.quantity - quantity) > 1e-6:
                raise ValidationError(_("Quantity of %s must be a whole number for customs.", product.name))
            item = {
                'itemDescription': (line.name or product.name or '')[:256],
                'packagedQuantity': quantity,
                'itemWeight': {'uom': 'g', 'value': unit_grams},
                'itemValue': {'currency': currency, 'value': round(line.unit_price, 2)},
            }
            hs_code = re.sub(r'\D', '', product.hs_code or '')
            if 6 <= len(hs_code) <= 11:
                item['hsCode'] = hs_code
            origin = to_iso3(product.country_of_origin_id.code)
            if origin:
                item['countryOfOrigin'] = origin
            items.append(item)
        return {
            # VERIFY: allowed exportType values. DHL's dry run (validate=true) reports a wrong value precisely.
            'exportType': 'COMMERCIAL_GOODS',
            'postalCharges': {'currency': currency, 'value': 0.0},
            'items': items,
        }

    def _build_shipment_payload(self):
        """Request body for POST /parcel/de/shipping/v2/orders (one parcel per shipment)."""
        self.ensure_one()
        missing = self._missing_data()
        if missing:
            raise ValidationError(_(
                "Cannot build shipment payload. Missing required data:\n- %(items)s",
                items='\n- '.join(missing)))
        if len(self.package_ids) != 1:
            raise ValidationError(_(
                "DHL Parcel DE needs one shipment per parcel. Multi-package shipments are not supported yet "
                "- keep exactly one package."))

        # Validate product and destination before building payload
        self._validate_product_for_destination()
        self._validate_billing_number_procedure()

        package = self.package_ids[0]

        # Validate dimensions before building payload
        self._validate_package_dimensions()

        # Validate weight against customs declaration
        self._validate_weight_against_customs()
        details = {'weight': {'uom': 'g', 'value': max(1, int(round(package.weight * 1000)))}}
        if package.length and package.width and package.height:
            details['dim'] = {
                'uom': 'mm',
                'length': max(1, int(round(package.length * 10))),
                'width': max(1, int(round(package.width * 10))),
                'height': max(1, int(round(package.height * 10))),
            }
        domestic = self.consignee_country_id.code == 'DE'
        shipment = {
            'product': self.product_code,
            'billingNumber': self._get_billing_number(),
            'refNo': (self.order_id.name or self.name or '')[:35],
            'shipDate': fields.Date.context_today(self).isoformat(),
            'shipper': self._address_block('shipper'),
            'consignee': self._address_block('consignee', with_phone=not domestic),
            'details': details,
        }
        # Add endorsement service for international shipments (RETURN or ABANDON)
        # According to DHL API spec: RETURN = return if undeliverable (default), ABANDON = destroy/auction
        if self._needs_customs():
            shipment['services'] = {'endorsement': 'RETURN'}  # Explicitly set to default to avoid warning
            shipment['customs'] = self._build_customs()
        return {'profile': SHIPMENT_PROFILE, 'shipments': [shipment]}

    def _call_dhl_shipment_api(self, connection, payload, validate=False):
        """POST /orders. With validate=True DHL only does a dry run (nothing is created).

        Returns the parsed response. Raises DHLAPIError with DHL's real reason
        (status.detail + per-item validationMessages) on any failure.
        """
        self.ensure_one()
        connection = connection.sudo()
        url = SHIPMENT_URLS[connection.environment]
        params = {'validate': 'true'} if validate else {
            'includeDocs': 'include', 'docFormat': 'PDF', 'mustEncode': 'false'}
        headers = {
            'Authorization': f'Bearer {connection.get_access_token()}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Accept-Language': 'en-US',
        }
        name = 'Shipment Validation' if validate else 'Shipment Creation'
        started = time.monotonic()
        status, raw, error, data = None, '', None, None
        try:
            response = requests.post(url, params=params, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            status, raw = response.status_code, response.text
            try:
                data = response.json()
            except ValueError:
                raise DHLAPIError(
                    _("DHL returned an unreadable (non-JSON) response (HTTP %s).", status),
                    status_code=status, category='invalid_response', retryable=bool(status and status >= 500))
            if not isinstance(data, dict):
                raise DHLAPIError(_("DHL returned an unexpected response format."),
                                  status_code=status, category='invalid_response')
            items = [i for i in (data.get('items') or []) if isinstance(i, dict)]
            # Check for ERROR-level validation messages (warnings are allowed)
            has_error = False
            warnings = []
            for item in items:
                sstatus = item.get('sstatus') or {}
                if (sstatus.get('status') or 200) >= 300:
                    # Check if there are any ERROR-level validation messages (not Warning)
                    for vm in item.get('validationMessages') or []:
                        if isinstance(vm, dict):
                            state = vm.get('validationState')
                            msg = vm.get('validationMessage')
                            if state == 'Error':
                                has_error = True
                            elif state == 'Warning' and msg:
                                warnings.append(msg)
                if has_error:
                    break
            # Notify about warnings but don't fail
            if warnings and not has_error:
                self.env['dhl.notification'].notify(
                    'warning', _('DHL Validation Warnings'),
                    _("DHL returned validation warnings (these will not block creation):\n- %(warnings)s",
                      warnings='\n- '.join(warnings)),
                    record=self)
            if status not in (200, 207) or has_error:
                reason = extract_dhl_error(data) or _("no details returned")
                category, retryable = classify_http_status(status)
                if status == 400 or (status in (200, 207) and has_error):
                    category, retryable = 'validation', False
                raise DHLAPIError(
                    _("DHL rejected the shipment (HTTP %(status)s): %(reason)s", status=status, reason=reason),
                    status_code=status, payload=data, category=category, retryable=retryable)
        except DHLAPIError as exc:
            error = exc
        except requests.Timeout:
            error = DHLAPIError(_("DHL did not respond in time (timeout after %s s).", REQUEST_TIMEOUT),
                                category='timeout', retryable=True)
        except requests.ConnectionError:
            error = DHLAPIError(_("Could not reach DHL. Check the server's internet connection."),
                                category='network', retryable=True)
        except requests.RequestException as exc:
            error = DHLAPIError(_("Network error while contacting DHL: %s", exc),
                                category='network', retryable=True)

        self.env['dhl.api.log'].log_call(
            name=name, connection_id=connection.id, company_id=connection.company_id.id,
            method='POST', endpoint=f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}",
            request_body=payload, status_code=status, response_body=raw,
            success=error is None, duration_ms=int((time.monotonic() - started) * 1000),
            error_message=str(error) if error else False, res_model='dhl.shipment', res_id=self.id)
        if error:
            raise error
        return data

    def action_create_dhl_shipment(self):
        """ready -> validating -> created | creation_failed

        1. the state is committed as 'validating' first, so a double click cannot start a second creation;
        2. DHL dry run (validate=true) - nothing is created if it fails;
        3. real creation; the result is saved and committed immediately so a later error cannot orphan
           a shipment that already exists at DHL.
        """
        Notification = self.env['dhl.notification']
        for rec in self:
            if rec.state != 'ready':
                continue
            rec.write({'state': 'validating', 'error_message': False})
            self.env.cr.commit()  # claim the shipment before talking to DHL

            created = None
            booking_attempted = False
            try:
                connection = rec.connection_id.sudo()
                if not connection or connection.state != 'connected':
                    raise UserError(_("DHL connection is not active. Please test the connection first."))
                payload = rec._build_shipment_payload()
                rec._call_dhl_shipment_api(connection, payload, validate=True)   # dry run
                booking_attempted = True
                data = rec._call_dhl_shipment_api(connection, payload)           # real creation
                created = (data.get('items') or [{}])[0]
                if not created.get('shipmentNo'):
                    created = None
                    raise DHLAPIError(_("DHL did not return a shipment number."), category='invalid_response')
            except DHLAPIError as exc:
                note = str(exc)
                if exc.category in ('timeout', 'network') and rec.state == 'validating':
                    note += _(" The result at DHL is unknown - check the DHL portal before trying again.")
                unknown = booking_attempted and (exc.category in ('timeout', 'network', 'invalid_response')
                                                  or bool(exc.status_code and exc.status_code >= 500))
                rec.action_mark_creation_failed(note, result_unknown=unknown)
                continue
            except (UserError, ValidationError) as exc:
                rec.action_mark_creation_failed(exc.args[0] if exc.args else str(exc),
                                                result_unknown=booking_attempted)
                continue
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Unexpected error creating DHL shipment %s", rec.name)
                rec.action_mark_creation_failed(_("Unexpected error: %s", exc),
                                                result_unknown=booking_attempted)
                continue

            shipment_no = created['shipmentNo']
            label = created.get('label') or {}
            vals = {'state': 'created', 'dhl_shipment_no': shipment_no, 'tracking_number': shipment_no,
                    'error_message': False}
            if label.get('b64'):
                vals.update(label=label['b64'], label_filename=f"{shipment_no}.pdf")
            try:
                rec.write(vals)
                self.env.cr.commit()  # never lose a shipment number that DHL already issued
            except Exception:
                _logger.critical("DHL CREATED shipment %s for %s but saving it in Odoo failed. "
                                 "See the API log, then enter the number manually.", shipment_no, rec.name,
                                 exc_info=True)
                raise
            Notification.notify(
                'success', _('Shipment Created'),
                _("%(name)s was created at DHL. Tracking number: %(tracking)s", name=rec.name, tracking=shipment_no),
                record=rec)
            if not label.get('b64'):
                Notification.notify('warning', _('Label missing'),
                                    _("%s was created but DHL returned no label data.", rec.name), record=rec)
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def action_refresh_tracking(self):
        """Manual 'Refresh tracking' button."""
        result = self.env['dhl.tracking.cron'].refresh_shipments(self)
        self.env['dhl.notification'].notify(
            'info', _('Tracking refreshed'),
            _("%(n)s new tracking events.", n=result.get('events', 0)), record=self[:1])
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    # ------------------------------------------------------------------
    # Tracking feed
    # ------------------------------------------------------------------
    def _apply_latest_tracking(self):
        """Update status from the newest tracking event. Never regresses
        a delivered or cancelled shipment."""
        Notification = self.env['dhl.notification']
        for rec in self:
            last = rec.tracking_ids.sorted(lambda e: (e.event_time, e.id))[-1:]
            if not last:
                continue
            rec.last_tracking_text = last.status_text
            if rec.state in ('delivered', 'cancelled'):
                continue
            status, new, old = last.normalized_status, None, rec.state
            if status == 'delivered':
                new = 'delivered'
            elif status in ('in_transit', 'out_for_delivery'):
                new = 'in_transit'
            elif status in ('delivery_failed', 'exception', 'returned'):
                new = 'delivery_failed'
            if new and new != old:
                update_vals = {'state': new, 'error_message': last.status_text if new == 'delivery_failed' else False}
                if new == 'delivered':
                    update_vals['delivered_at'] = last.event_time
                rec.write(update_vals)
                if new == 'delivered':
                    Notification.notify('success', _('Shipment Delivered'),
                                        _("%s was delivered.", rec.name), record=rec)
                elif new == 'delivery_failed':
                    Notification.notify('warning', _('Delivery Problem'),
                                        _("%(name)s: %(text)s", name=rec.name, text=last.status_text or status), record=rec)
        self.mapped('order_id')._sync_from_shipments()


class DhlPackage(models.Model):
    _name = 'dhl.package'
    _description = 'Shipment Package'

    shipment_id = fields.Many2one('dhl.shipment', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='shipment_id.company_id', store=True, index=True)
    name = fields.Char('Reference', default='Package')
    weight = fields.Float('Weight (kg)', digits=(10, 3), default=1.0)
    length = fields.Float('Length (cm)')
    width = fields.Float('Width (cm)')
    height = fields.Float('Height (cm)')

    @api.constrains('weight', 'length', 'width', 'height')
    def _check_values(self):
        for rec in self:
            if rec.weight <= 0:
                raise ValidationError(_("Package weight must be greater than zero."))
            if min(rec.length, rec.width, rec.height) < 0:
                raise ValidationError(_("Package dimensions cannot be negative."))