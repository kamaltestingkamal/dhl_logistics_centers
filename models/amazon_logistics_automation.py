# -*- coding: utf-8 -*-
"""Amazon SP-API sync, safe retry queue, and CSV reports.

Amazon endpoints and response mapping are based on SP-API Orders v2026-01-01
and Listings Items v2021-08-01. Access to buyer/recipient fields depends on the
seller's authorization and assigned SP-API roles. This module never invents a
missing address, SKU, title, currency, or product weight.
"""
import base64
import csv
import io
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .dhl_utils import body_to_text, classify_http_status

_logger = logging.getLogger(__name__)

LWA_TOKEN_URL = 'https://api.amazon.com/auth/o2/token'
AMAZON_ENDPOINTS = {
    'sandbox': {
        'na': 'https://sandbox.sellingpartnerapi-na.amazon.com',
        'eu': 'https://sandbox.sellingpartnerapi-eu.amazon.com',
        'fe': 'https://sandbox.sellingpartnerapi-fe.amazon.com',
    },
    'production': {
        'na': 'https://sellingpartnerapi-na.amazon.com',
        'eu': 'https://sellingpartnerapi-eu.amazon.com',
        'fe': 'https://sellingpartnerapi-fe.amazon.com',
    },
}
AMAZON_TIMEOUT = 30
AMAZON_MANAGER_GROUP = 'dhl_logistics_center.group_dhl_manager'


class SafeRetryableError(Exception):
    retryable = True


class AmazonAPIError(Exception):
    def __init__(self, message, status_code=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _parse_amazon_datetime(value):
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return False
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso_utc(value):
    if not value:
        value = datetime.utcnow() - timedelta(days=2)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _split_street(value):
    """Split common house-number formats without guessing when no number is clear."""
    value = (value or '').strip()
    # German / many European addresses: "Leipziger Platz 12".
    match = re.match(r'^(.*\S)\s+(\d+[A-Za-z]?(?:[-/]\d+[A-Za-z]?)?)$', value)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    # UK/US format: "45 Oak Avenue".
    match = re.match(r'^(\d+[A-Za-z]?)\s+(.+)$', value)
    if match:
        return match.group(2).strip(), match.group(1).strip()
    return value, False


class DhlAmazonConnection(models.Model):
    _name = 'dhl.amazon.connection'
    _description = 'Amazon Selling Partner API Connection'
    _order = 'sequence, id'

    name = fields.Char(required=True, default='Amazon Seller Account')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    environment = fields.Selection([('sandbox', 'Sandbox'), ('production', 'Production')],
                                   required=True, default='sandbox', index=True,
                                   help='Defaults to Sandbox to prevent accidental production API calls.')
    region = fields.Selection([('na', 'North America'), ('eu', 'Europe'), ('fe', 'Far East')],
                              required=True, default='eu')
    seller_id = fields.Char('Seller ID', groups=AMAZON_MANAGER_GROUP)
    marketplace_ids = fields.Char(
        'Marketplace IDs', required=True, groups=AMAZON_MANAGER_GROUP,
        help='Comma-separated Amazon marketplace IDs for one SP-API region, e.g. A1PA6795UKMFR9 for Germany.')
    lwa_client_id = fields.Char('LWA Client ID', required=True, groups=AMAZON_MANAGER_GROUP)
    lwa_client_secret = fields.Char('LWA Client Secret', required=True, groups=AMAZON_MANAGER_GROUP)
    refresh_token = fields.Char('LWA Refresh Token', required=True, groups=AMAZON_MANAGER_GROUP)
    access_token = fields.Char(copy=False, groups=AMAZON_MANAGER_GROUP)
    token_expires_at = fields.Datetime(copy=False, groups=AMAZON_MANAGER_GROUP)
    state = fields.Selection([
        ('disconnected', 'Disconnected'), ('connected', 'Connected'),
        ('auth_error', 'Authentication Problem'), ('api_error', 'API Problem')],
        default='disconnected', readonly=True, copy=False, index=True)
    last_connected = fields.Datetime(readonly=True, copy=False)
    last_order_sync_at = fields.Datetime(readonly=True, copy=False)
    last_product_sync_at = fields.Datetime(readonly=True, copy=False)
    order_sync_window_start = fields.Datetime(copy=False, groups=AMAZON_MANAGER_GROUP)
    order_sync_page_token = fields.Char(copy=False, groups=AMAZON_MANAGER_GROUP)
    product_sync_window_start = fields.Datetime(copy=False, groups=AMAZON_MANAGER_GROUP)
    product_sync_page_token = fields.Char(copy=False, groups=AMAZON_MANAGER_GROUP)
    product_sync_marketplace_id = fields.Char(copy=False, groups=AMAZON_MANAGER_GROUP)
    product_sync_marketplace_index = fields.Integer(default=0, copy=False, groups=AMAZON_MANAGER_GROUP)
    last_error = fields.Text(readonly=True, copy=False, groups=AMAZON_MANAGER_GROUP)
    sync_order_count = fields.Integer(readonly=True, copy=False)
    sync_product_count = fields.Integer(readonly=True, copy=False)

    _sql_constraints = [
        ('amazon_name_company_unique', 'unique(name, company_id)', 'An Amazon connection with this name already exists for this company.'),
    ]

    def write(self, vals):
        credential_fields = {'environment', 'region', 'marketplace_ids', 'lwa_client_id', 'lwa_client_secret', 'refresh_token', 'seller_id'}
        if credential_fields.intersection(vals):
            vals = dict(vals, state='disconnected', access_token=False, token_expires_at=False,
                        last_error=False, last_connected=False)
        return super().write(vals)

    def _marketplace_list(self):
        self.ensure_one()
        marketplace_ids = self.sudo().marketplace_ids
        values = [item.strip() for item in (marketplace_ids or '').split(',') if item.strip()]
        if not values:
            raise UserError(_('Enter at least one Amazon marketplace ID.'))
        if len(values) > 50:
            raise ValidationError(_('Amazon Orders API accepts at most 50 marketplace IDs per request.'))
        return values

    def _get_lwa_access_token(self, force=False):
        self.ensure_one()
        rec = self.sudo()
        now = fields.Datetime.now()
        if not force and rec.access_token and rec.token_expires_at and rec.token_expires_at > now + timedelta(seconds=60):
            return rec.access_token
        payload = {
            'grant_type': 'refresh_token',
            'refresh_token': rec.refresh_token,
            'client_id': rec.lwa_client_id,
            'client_secret': rec.lwa_client_secret,
        }
        started = time.monotonic()
        status = False
        try:
            response = requests.post(LWA_TOKEN_URL, data=payload, timeout=AMAZON_TIMEOUT)
            status = response.status_code
            data = response.json() if response.content else {}
            if status != 200 or not isinstance(data, dict) or not data.get('access_token'):
                self.env['dhl.api.log'].sudo().log_call(
                    name='Amazon LWA Token', service='amazon', company_id=self.company_id.id, method='POST', endpoint=LWA_TOKEN_URL,
                    request_body={'grant_type': 'refresh_token', 'credentials': '[REDACTED]'},
                    status_code=status, response_body={'error': 'token_request_failed'}, success=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_message='Amazon LWA token request failed.')
                raise AmazonAPIError(_('Amazon authentication failed (HTTP %s).', status), status, status in (429, 500, 502, 503, 504))
        except requests.Timeout as exc:
            raise AmazonAPIError(_('Amazon authentication timed out.'), retryable=True) from exc
        except requests.RequestException as exc:
            raise AmazonAPIError(_('Could not reach Amazon authentication service.'), retryable=True) from exc
        except (ValueError, TypeError) as exc:
            raise AmazonAPIError(_('Amazon authentication returned an unreadable response.'), status) from exc
        rec.write({
            'access_token': data['access_token'],
            'token_expires_at': now + timedelta(seconds=int(data.get('expires_in') or 3600)),
        })
        self.env['dhl.api.log'].sudo().log_call(
            name='Amazon LWA Token', service='amazon', company_id=self.company_id.id, method='POST', endpoint=LWA_TOKEN_URL,
            request_body={'grant_type': 'refresh_token', 'credentials': '[REDACTED]'},
            status_code=status, response_body={'token_issued': True}, success=True,
            duration_ms=int((time.monotonic() - started) * 1000))
        return data['access_token']

    def _amazon_get(self, path, params=None, operation='Amazon API'):
        self.ensure_one()
        base = AMAZON_ENDPOINTS[self.environment][self.region]
        url = base + path
        token = self._get_lwa_access_token()
        headers = {
            'x-amz-access-token': token,
            'x-amz-date': datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
            'user-agent': 'DHL-Amazon-Logistics-Control-Center/19.0 (Language=Python/3)',
            'accept': 'application/json',
        }
        started = time.monotonic()
        status = False
        try:
            response = requests.get(url, params=params or {}, headers=headers, timeout=AMAZON_TIMEOUT)
            status = response.status_code
            if status >= 400:
                retryable = status == 429 or status >= 500
                self.env['dhl.api.log'].sudo().log_call(
                    name=operation, service='amazon', company_id=self.company_id.id, method='GET', endpoint=url,
                    request_body={'query_keys': sorted((params or {}).keys())}, status_code=status,
                    response_body={'error': 'amazon_api_request_failed'}, success=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_message='Amazon API request failed (HTTP %s).' % status)
                raise AmazonAPIError(_('Amazon API request failed (HTTP %s).', status), status, retryable)
            try:
                data = response.json()
            except ValueError as exc:
                raise AmazonAPIError(_('Amazon API returned an unreadable response.'), status) from exc
            if not isinstance(data, dict):
                raise AmazonAPIError(_('Amazon API returned an unexpected response format.'), status)
        except requests.Timeout as exc:
            raise AmazonAPIError(_('Amazon API request timed out.'), retryable=True) from exc
        except requests.ConnectionError as exc:
            raise AmazonAPIError(_('Could not connect to Amazon SP-API.'), retryable=True) from exc
        except requests.RequestException as exc:
            raise AmazonAPIError(_('Network error while contacting Amazon SP-API.'), retryable=True) from exc
        self.env['dhl.api.log'].sudo().log_call(
            name=operation, service='amazon', company_id=self.company_id.id, method='GET', endpoint=url,
            request_body={'query_keys': sorted((params or {}).keys())}, status_code=status,
            # Do not persist order responses: they can contain recipient PII.
            response_body={'response_received': True}, success=True,
            duration_ms=int((time.monotonic() - started) * 1000))
        return data

    def action_test_connection(self):
        for rec in self:
            try:
                rec._get_lwa_access_token(force=True)
                rec._amazon_get('/sellers/v1/marketplaceParticipations', operation='Amazon Connection Test')
            except AmazonAPIError as exc:
                rec.sudo().write({'state': 'auth_error' if exc.status_code in (401, 403) else 'api_error',
                                  'last_error': str(exc)})
                self.env['dhl.notification'].notify('error', _('Amazon Connection Failed'),
                    _('Please check LWA credentials, seller authorization, marketplace region, and required SP-API roles.'), record=rec)
            else:
                rec.sudo().write({'state': 'connected', 'last_connected': fields.Datetime.now(), 'last_error': False})
                self.env['dhl.notification'].notify('success', _('Amazon Connected Successfully'),
                    _('Amazon SP-API connection is active.'), record=rec)
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def _upsert_listing_product(self, item, marketplace):
        self.ensure_one()
        sku = (item.get('sku') or item.get('sellerSku') or '').strip()
        summaries = item.get('summaries') or []
        summary = summaries[0] if summaries and isinstance(summaries[0], dict) else {}
        asin = (summary.get('asin') or item.get('asin') or '').strip()
        title = (summary.get('itemName') or summary.get('title') or '').strip()
        if not sku:
            return False, 'missing_sku'
        Product = self.env['dhl.product'].sudo().with_company(self.company_id)
        existing = Product.search([('company_id', '=', self.company_id.id), ('sku', '=', sku)], limit=1)
        if not title and not existing:
            return False, 'missing_title'
        values = {'sku': sku, 'marketplace': marketplace}
        if title:
            values['name'] = title
        if asin:
            values['asin'] = asin
        product, status = Product.upsert_amazon_product(values, marketplace)
        return product, status

    def sync_products_now(self, max_pages=10):
        """Sync listing items one marketplace per request and persist resumable pagination."""
        self.ensure_one()
        if not self.sudo().seller_id:
            raise UserError(_('Set the Amazon Seller ID before synchronizing product listings.'))
        marketplaces = self._marketplace_list()
        if not max_pages or max_pages < 1:
            raise ValidationError(_('max_pages must be a positive integer.'))

        # Amazon searchListingsItems accepts one marketplace ID per request.
        # Preserve both the marketplace index and page token so a bounded cron can resume safely.
        current_marketplace = self.sudo().product_sync_marketplace_id or False
        if current_marketplace in marketplaces:
            marketplace_index = marketplaces.index(current_marketplace)
        else:
            marketplace_index = min(max(self.sudo().product_sync_marketplace_index or 0, 0), len(marketplaces) - 1)
        page_token = self.sudo().product_sync_page_token or False
        resuming = bool(page_token or current_marketplace or marketplace_index > 0)
        if resuming:
            # False is meaningful here: it means the original run was a full listing sync.
            window_start = self.sudo().product_sync_window_start or False
        else:
            window_start = self.sudo().product_sync_window_start or (
                self.last_product_sync_at - timedelta(minutes=3) if self.last_product_sync_at else False)

        total = created = updated = skipped = pages = 0
        while marketplace_index < len(marketplaces) and pages < max_pages:
            marketplace = marketplaces[marketplace_index]
            params = {
                'marketplaceIds': marketplace,
                'includedData': 'summaries',
                'pageSize': 20,
                'sortBy': 'lastUpdatedDate',
                'sortOrder': 'DESC',
            }
            if window_start:
                params['lastUpdatedAfter'] = _iso_utc(window_start)
            if page_token and current_marketplace == marketplace:
                params['pageToken'] = page_token
            else:
                page_token = False

            data = self._amazon_get(
                '/listings/2021-08-01/items/%s' % quote(self.sudo().seller_id, safe=''),
                params=params, operation='Amazon Product Listings Sync')
            items = data.get('items') or []
            for item in items:
                total += 1
                item_summaries = item.get('summaries') or []
                item_marketplace = marketplace
                if item_summaries and isinstance(item_summaries[0], dict):
                    item_marketplace = item_summaries[0].get('marketplaceId') or marketplace
                try:
                    product, status = self._upsert_listing_product(item, item_marketplace)
                except (ValidationError, UserError):
                    skipped += 1
                    continue
                except Exception as exc:  # Keep one malformed listing from aborting the batch.
                    skipped += 1
                    _logger.exception('Unexpected Amazon listing import failure for connection %s', self.id)
                    continue
                if not product:
                    skipped += 1
                elif status == 'created':
                    created += 1
                else:
                    updated += 1

            pages += 1
            pagination = data.get('pagination') or {}
            page_token = data.get('nextToken') or pagination.get('nextToken') or False
            current_marketplace = marketplace
            if page_token:
                # The next invocation resumes this exact marketplace and page, even if a page is empty.
                self.sudo().write({
                    'product_sync_page_token': page_token,
                    'product_sync_marketplace_id': marketplace,
                    'product_sync_marketplace_index': marketplace_index,
                    'product_sync_window_start': window_start or False,
                    'sync_product_count': total,
                })
                break

            # No next page for this marketplace. Continue to the next one if any.
            page_token = False
            marketplace_index += 1
            current_marketplace = marketplaces[marketplace_index] if marketplace_index < len(marketplaces) else False
            self.sudo().write({
                'product_sync_page_token': False,
                'product_sync_marketplace_id': current_marketplace or False,
                'product_sync_marketplace_index': marketplace_index,
                'product_sync_window_start': window_start or False,
                'sync_product_count': total,
            })

        complete = marketplace_index >= len(marketplaces) and not page_token
        state = 'success' if complete and not skipped else ('partial' if not complete or total > skipped else 'failed')
        if complete:
            self.sudo().write({
                'last_product_sync_at': fields.Datetime.now(), 'sync_product_count': total,
                'product_sync_page_token': False, 'product_sync_marketplace_id': False,
                'product_sync_marketplace_index': 0, 'product_sync_window_start': False,
            })
        self.env['dhl.sync.log'].sudo().create({
            'name': _('Amazon Product Listings Sync'), 'company_id': self.company_id.id, 'source': 'amazon', 'operation': 'products',
            'state': state, 'records_processed': total - skipped, 'records_failed': skipped,
            'message': _('Read %(total)s listings: %(created)s created, %(updated)s updated, %(skipped)s skipped. %(resume)s Product titles and ASINs are imported only when Amazon returns them; weight, dimensions and images are not guessed.',
                         total=total, created=created, updated=updated, skipped=skipped,
                         resume=_('More pages or marketplaces remain and will resume on the next cron run.') if not complete else ''),
        })
        return {'total': total, 'created': created, 'updated': updated, 'skipped': skipped}

    def action_sync_products(self):
        for rec in self:
            try:
                summary = rec.sync_products_now()
            except (AmazonAPIError, UserError, ValidationError) as exc:
                self.env['dhl.notification'].notify('error', _('Amazon Product Sync Failed'), str(exc), record=rec)
                if isinstance(exc, AmazonAPIError) and exc.retryable:
                    self.env['dhl.retry.job'].queue_retry('amazon_products_sync', amazon_connection=rec, error=str(exc))
            else:
                self.env['dhl.notification'].notify('success', _('Amazon Product Sync Finished'),
                    _('%(created)s created, %(updated)s updated, %(skipped)s skipped.', **summary), record=rec)
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def _find_or_create_customer_and_address(self, order_data, marketplace):
        recipient = (order_data.get('recipient') or {}).get('deliveryAddress') or {}
        buyer = order_data.get('buyer') or {}
        name = (recipient.get('name') or buyer.get('buyerName') or '').strip()
        email = (buyer.get('buyerEmail') or '').strip()
        phone = (recipient.get('phone') or '').strip()
        raw_street = (recipient.get('addressLine1') or '').strip()
        city = (recipient.get('city') or '').strip()
        postal = (recipient.get('postalCode') or '').strip()
        country_code = (recipient.get('countryCode') or '').strip().upper()
        if not name or not raw_street or not city or not postal or not country_code:
            return False, False, 'recipient address is incomplete or restricted by Amazon permissions'
        country = self.env['res.country'].sudo().search([('code', '=', country_code)], limit=1)
        if not country:
            return False, False, 'country code %s is not configured in Odoo' % country_code
        street, house_number = _split_street(raw_street)
        street2 = ' '.join(filter(None, [recipient.get('addressLine2'), recipient.get('addressLine3')])).strip()
        Customer = self.env['dhl.customer'].sudo().with_company(self.company_id)
        customer_domain = [('company_id', '=', self.company_id.id)]
        customer = Customer.browse()
        if email:
            customer = Customer.search(customer_domain + [('email', '=', email)], limit=1)
        if not customer and phone:
            customer = Customer.search(customer_domain + [('name', '=', name), ('phone', '=', phone)], limit=1)
        if not customer:
            candidates = Customer.search(customer_domain + [('name', '=', name)], limit=20)
            for candidate in candidates:
                if candidate.address_ids.filtered(lambda a: a.street == street and a.street_number == (house_number or False)
                                                   and (a.address_line2 or '') == (street2 or '') and a.zip == postal
                                                   and a.city == city and a.country_id == country):
                    customer = candidate
                    break
        customer_values = {'name': name, 'source': 'amazon', 'marketplace': marketplace,
                           'last_sync_at': fields.Datetime.now(), 'company_id': self.company_id.id}
        if email:
            customer_values['email'] = email
        if phone:
            customer_values['phone'] = phone
        if customer:
            # Do not overwrite existing contact data with blank Amazon values.
            customer.write({key: value for key, value in customer_values.items() if value not in (False, '', None)})
        else:
            customer = Customer.create(customer_values)
        Address = self.env['dhl.customer.address'].sudo().with_company(self.company_id)
        address = Address.search([
            ('customer_id', '=', customer.id), ('street', '=', street),
            ('street_number', '=', house_number or False), ('address_line2', '=', street2 or False),
            ('zip', '=', postal), ('city', '=', city), ('country_id', '=', country.id),
        ], limit=1)
        address_values = {
            'customer_id': customer.id, 'label': 'Amazon Delivery Address', 'address_type': 'shipping',
            'contact_name': name, 'street': street, 'street_number': house_number or False,
            'address_line2': street2 or False, 'zip': postal, 'city': city,
            'state_name': recipient.get('stateOrRegion') or recipient.get('districtOrCounty') or False,
            'country_id': country.id, 'email': email or False, 'phone': phone or False,
        }
        if address:
            address.write({key: value for key, value in address_values.items() if key != 'customer_id' and value})
        else:
            address_values['is_default'] = not bool(customer.address_ids.filtered('is_default'))
            address = Address.create(address_values)
        return customer, address, False

    def _import_order(self, order_data, marketplace):
        amazon_order_id = (order_data.get('orderId') or '').strip()
        if not amazon_order_id:
            return 'skipped', 'Amazon response had no orderId'
        fulfillment = order_data.get('fulfillment') or {}
        # This DHL workflow is for merchant-fulfilled parcels. Do not book DHL for FBA.
        if fulfillment.get('fulfilledBy') == 'AMAZON':
            return 'skipped', 'Amazon-fulfilled order; DHL booking is not applicable'
        items = order_data.get('orderItems') or []
        if not items:
            return 'skipped', 'Amazon did not return orderItems for this order'
        proceeds = order_data.get('proceeds') or {}
        grand_total = proceeds.get('grandTotal') or {}
        currency_code = grand_total.get('currencyCode')
        if not currency_code:
            for item in items:
                unit = ((item.get('product') or {}).get('price') or {}).get('unitPrice') or {}
                if unit.get('currencyCode'):
                    currency_code = unit['currencyCode']
                    break
        if not currency_code:
            return 'skipped', 'Amazon did not provide an order/item currency; no default currency was guessed'
        currency = self.env['res.currency'].sudo().search(
            [('name', '=', currency_code), ('active', '=', True)], limit=1)
        if not currency:
            return 'skipped', 'Currency %s is not configured in Odoo' % currency_code
        customer, address, issue = self._find_or_create_customer_and_address(order_data, marketplace)
        if issue:
            return 'skipped', issue
        Order = self.env['dhl.order'].sudo().with_company(self.company_id)
        order = Order.search([('company_id', '=', self.company_id.id), ('amazon_order_id', '=', amazon_order_id)], limit=1)
        status = (fulfillment.get('fulfillmentStatus') or fulfillment.get('status')
                  or order_data.get('fulfillmentStatus') or '').upper()
        mapped_state = {'PENDING': 'pending', 'PENDING_AVAILABILITY': 'pending', 'UNSHIPPED': 'pending',
                        'PARTIALLY_SHIPPED': 'processing', 'SHIPPED': 'shipped', 'CANCELLED': 'cancelled'}.get(status, 'pending')
        if order:
            live_shipments = order.shipment_ids.filtered(lambda s: s.state in ('created', 'in_transit', 'delivered', 'delivery_failed'))
            if mapped_state == 'cancelled' and live_shipments:
                self.env['dhl.notification'].notify('warning', _('Amazon Order Cancellation Needs Review'),
                    _('Amazon order %s was cancelled but a DHL shipment exists. No automatic DHL cancellation was attempted.', amazon_order_id), record=order)
                mapped_state = order.state
            elif live_shipments and mapped_state in ('pending', 'processing'):
                # Do not regress a local order workflow because an Amazon status is delayed or partial.
                mapped_state = order.state
        order_values = {
            'source': 'amazon', 'amazon_order_id': amazon_order_id, 'marketplace': marketplace,
            'customer_id': customer.id, 'address_id': address.id, 'currency_id': currency.id,
            'company_id': self.company_id.id, 'state': mapped_state,
        }
        order_date = _parse_amazon_datetime(order_data.get('createdTime'))
        if order_date:
            order_values['order_date'] = order_date
        if order:
            order.write(order_values)
            action = 'updated'
        else:
            order = Order.create(order_values)
            action = 'created'
        Line = self.env['dhl.order.line'].sudo().with_company(self.company_id)
        for item in items:
            product_data = item.get('product') or {}
            sku = (product_data.get('sellerSku') or '').strip()
            asin = (product_data.get('asin') or '').strip()
            title = (product_data.get('title') or '').strip()
            if not sku:
                return 'partial', 'order %s has an item without sellerSku; order saved but line needs review' % amazon_order_id
            Product = self.env['dhl.product'].sudo().with_company(self.company_id)
            product = Product.search([('company_id', '=', self.company_id.id), ('sku', '=', sku)], limit=1)
            if not product and asin:
                product = Product.search([('company_id', '=', self.company_id.id), ('asin', '=', asin), ('marketplace', '=', marketplace)], limit=1)
            if not product and not title:
                return 'partial', 'order %s item %s has no title; product was not invented' % (amazon_order_id, sku)
            product_values = {'sku': sku, 'asin': asin or False}
            if title:
                product_values['name'] = title
            elif product:
                product_values['name'] = product.name
            product, _product_action = Product.upsert_amazon_product(product_values, marketplace)
            unit_price = (product_data.get('price') or {}).get('unitPrice') or {}
            line_currency = unit_price.get('currencyCode')
            if line_currency and line_currency != currency.name:
                return 'partial', 'order %s line currency differs from order currency; line was not imported' % amazon_order_id
            price = unit_price.get('amount')
            if price is None:
                existing_line = Line.search([('order_id', '=', order.id), ('amazon_order_item_id', '=', item.get('orderItemId'))], limit=1) if item.get('orderItemId') else Line.browse()
                if not existing_line:
                    return 'partial', 'order %s item %s has no price; line was not assigned a guessed value' % (amazon_order_id, sku)
                price = existing_line.unit_price
            quantity = item.get('quantityOrdered') or 0
            if float(quantity) <= 0:
                return 'partial', 'order %s item %s has invalid quantity' % (amazon_order_id, sku)
            item_id = str(item.get('orderItemId') or '').strip()
            if not item_id:
                return 'partial', 'order %s item %s has no Amazon orderItemId; line was not duplicated or guessed' % (amazon_order_id, sku)
            line = Line.search([('order_id', '=', order.id), ('amazon_order_item_id', '=', item_id)], limit=1) if item_id else Line.browse()
            line_values = {'order_id': order.id, 'product_id': product.id, 'name': title or product.name,
                           'quantity': float(quantity), 'unit_price': float(price)}
            if item_id:
                line_values['amazon_order_item_id'] = item_id
            if line:
                line.write(line_values)
            else:
                Line.create(line_values)
        return action, False

    def sync_orders_now(self, max_pages=10):
        self.ensure_one()
        marketplaces = self._marketplace_list()
        overlap = timedelta(minutes=3)
        since = self.last_order_sync_at - overlap if self.last_order_sync_at else datetime.utcnow() - timedelta(days=2)
        if self.sudo().order_sync_page_token and self.sudo().order_sync_window_start:
            since = self.sudo().order_sync_window_start
            page_token = self.sudo().order_sync_page_token
        else:
            page_token = False
        pages = processed = failed = created = updated = skipped = partial = 0
        messages = []
        while pages < max_pages:
            params = {
                'marketplaceIds': ','.join(marketplaces),
                'lastUpdatedAfter': _iso_utc(since),
                'includedData': 'BUYER,RECIPIENT,PROCEEDS,FULFILLMENT',
                'maxResultsPerPage': 100,
            }
            if page_token:
                params['paginationToken'] = page_token
            data = self._amazon_get('/orders/2026-01-01/orders', params=params, operation='Amazon Orders Sync')
            response = data.get('response') if isinstance(data.get('response'), dict) else (data.get('payload') if isinstance(data.get('payload'), dict) else data)
            orders = response.get('orders') or []
            for order_data in orders:
                processed += 1
                marketplace = ((order_data.get('salesChannel') or {}).get('marketplaceId') or marketplaces[0])
                try:
                    result, issue = self._import_order(order_data, marketplace)
                except (ValidationError, UserError, ValueError, TypeError) as exc:
                    failed += 1
                    messages.append('Order import validation issue: %s' % str(exc)[:200])
                    continue
                except Exception as exc:  # Keep a malformed order from aborting the remaining batch.
                    failed += 1
                    messages.append('Unexpected order import failure; see server log.')
                    _logger.exception('Unexpected Amazon order import failure for connection %s', self.id)
                    continue
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                elif result == 'partial':
                    partial += 1
                    if issue:
                        messages.append(issue)
                else:
                    skipped += 1
                    if issue and len(messages) < 5:
                        messages.append(issue)
            pages += 1
            pagination = response.get('pagination') or {}
            page_token = pagination.get('nextToken') or response.get('nextToken')
            self.sudo().write({'order_sync_page_token': page_token or False,
                               'order_sync_window_start': since if page_token else False})
            if not page_token or not orders:
                page_token = False
                break
        now = fields.Datetime.now()
        if page_token:
            # Keep the exact query window and token so the next cron resumes instead of restarting page 1.
            self.sudo().write({'sync_order_count': processed, 'order_sync_page_token': page_token,
                               'order_sync_window_start': since})
        else:
            self.sudo().write({'last_order_sync_at': now, 'sync_order_count': processed,
                               'order_sync_page_token': False, 'order_sync_window_start': False})
        state = 'partial' if page_token else ('success' if not (failed or partial) else ('partial' if processed > failed else 'failed'))
        self.env['dhl.sync.log'].sudo().create({
            'name': _('Amazon Orders Sync'), 'source': 'amazon', 'operation': 'orders', 'state': state,
            'records_processed': created + updated, 'records_failed': failed + partial,
            'message': _('Read %(processed)s orders: %(created)s created, %(updated)s updated, %(skipped)s skipped, %(partial)s partial, %(failed)s failed. %(notes)s',
                         processed=processed, created=created, updated=updated, skipped=skipped,
                         partial=partial, failed=failed, notes='; '.join(messages[:5]) + (('; more pages will resume on the next cron run') if page_token else '')),
        })
        if skipped or partial or failed:
            self.env['dhl.notification'].notify('warning', _('Amazon Orders Sync Needs Attention'),
                _('%(created)s created, %(updated)s updated, %(skipped)s skipped, %(partial)s partial, %(failed)s failed. Check Sync History for details.',
                  created=created, updated=updated, skipped=skipped, partial=partial, failed=failed), record=self)
        else:
            self.env['dhl.notification'].notify('success', _('Amazon Orders Synced'),
                _('%(created)s new orders and %(updated)s updated orders.', created=created, updated=updated), record=self)
        return {'processed': processed, 'created': created, 'updated': updated, 'skipped': skipped,
                'partial': partial, 'failed': failed}

    def action_sync_orders(self):
        for rec in self:
            try:
                rec.sync_orders_now()
            except (AmazonAPIError, UserError, ValidationError) as exc:
                rec.sudo().write({'state': 'auth_error' if exc.status_code in (401, 403) else 'api_error', 'last_error': str(exc)})
                self.env['dhl.notification'].notify('error', _('Amazon Orders Sync Failed'), str(exc), record=rec)
                if exc.retryable:
                    self.env['dhl.retry.job'].queue_retry('amazon_orders_sync', amazon_connection=rec, error=str(exc))
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    @api.model
    def _cron_sync_active_connections(self):
        for rec in self.sudo().search([('active', '=', True), ('state', '=', 'connected')]):
            for operation in ('orders', 'products'):
                try:
                    if operation == 'orders':
                        rec.sync_orders_now()
                    else:
                        rec.sync_products_now()
                except AmazonAPIError as exc:
                    rec.write({'state': 'auth_error' if exc.status_code in (401, 403) else 'api_error', 'last_error': str(exc)})
                    if exc.retryable:
                        self.env['dhl.retry.job'].sudo().queue_retry(
                            'amazon_orders_sync' if operation == 'orders' else 'amazon_products_sync',
                            amazon_connection=rec, error=str(exc))
                    break
                except Exception as exc:  # Keep one connection from stopping the batch.
                    _logger.exception('Amazon %s sync failed for connection %s', operation, rec.id)
                    self.env['dhl.automation.log'].sudo().create({
                        'name': _('Amazon %s Sync' % operation.title()), 'state': 'failed',
                        'finished_at': fields.Datetime.now(), 'message': body_to_text(str(exc), limit=1000)})
                    break


class DhlOrderLineAmazon(models.Model):
    _inherit = 'dhl.order.line'

    amazon_order_item_id = fields.Char('Amazon Order Item ID', copy=False, index=True)

    _sql_constraints = [
        ('amazon_order_item_unique', 'unique(order_id, amazon_order_item_id)',
         'An Amazon order item can only be imported once per order.'),
    ]


class DhlSyncLogAmazon(models.Model):
    _inherit = 'dhl.sync.log'

    operation = fields.Selection(selection_add=[('products', 'Product Sync'), ('returns', 'Return Sync')], ondelete={'products': 'set default', 'returns': 'set default'})


class DhlReturnAmazonSync(models.Model):
    _inherit = 'dhl.return'

    source = fields.Selection([('manual', 'Manual'), ('amazon', 'Amazon')], default='manual', required=True, index=True)
    amazon_return_id = fields.Char('Amazon Return ID', index=True, copy=False)
    marketplace = fields.Char(copy=False)
    last_sync_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        ('amazon_return_unique', 'unique(company_id, amazon_return_id)', 'This Amazon return has already been imported.'),
    ]

    @api.model
    def upsert_amazon_return(self, data, marketplace):
        """Idempotent import boundary for normalized return data from a verified Amazon source.

        This does not call an undocumented return endpoint. A future Amazon Returns API/report
        adapter must pass verified return ID, order ID, and reason fields here.
        """
        if not isinstance(data, dict) or not data.get('return_id') or not data.get('amazon_order_id'):
            raise ValidationError(_('Amazon return data must contain return_id and amazon_order_id.'))
        order = self.env['dhl.order'].search([('amazon_order_id', '=', data['amazon_order_id']),
                                              ('company_id', '=', self.env.company.id)], limit=1)
        if not order:
            raise ValidationError(_('Cannot import the return because its Amazon order is not imported.'))
        rec = self.search([('company_id', '=', self.env.company.id), ('amazon_return_id', '=', data['return_id'])], limit=1)
        vals = {'source': 'amazon', 'amazon_return_id': data['return_id'], 'marketplace': marketplace or False,
                'order_id': order.id, 'last_sync_at': fields.Datetime.now(), 'notes': data.get('reason') or False}
        if rec:
            rec.write(vals)
            return rec, 'updated'
        vals['reason'] = data.get('reason_code') if data.get('reason_code') in ('damaged', 'wrong_item', 'not_needed', 'other') else 'other'
        return self.create(vals), 'created'


class DhlRetryJob(models.Model):
    _name = 'dhl.retry.job'
    _description = 'Safe Automation Retry Job'
    _order = 'next_attempt_at, id'

    name = fields.Char(required=True)
    operation = fields.Selection([
        ('amazon_orders_sync', 'Amazon Orders Sync'),
        ('amazon_products_sync', 'Amazon Product Sync'),
        ('dhl_tracking_refresh', 'DHL Tracking Refresh'),
    ], required=True, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    amazon_connection_id = fields.Many2one('dhl.amazon.connection', ondelete='cascade')
    shipment_id = fields.Many2one('dhl.shipment', ondelete='cascade')
    state = fields.Selection([('pending', 'Pending'), ('retrying', 'Retrying'), ('done', 'Done'), ('failed', 'Failed')],
                             default='pending', required=True, index=True)
    retry_count = fields.Integer(default=0)
    max_retries = fields.Integer(default=5)
    next_attempt_at = fields.Datetime(default=fields.Datetime.now, index=True)
    last_attempt_at = fields.Datetime(readonly=True)
    last_error = fields.Text()

    @api.model
    def queue_retry(self, operation, amazon_connection=None, shipment=None, error=''):
        if operation not in ('amazon_orders_sync', 'amazon_products_sync', 'dhl_tracking_refresh'):
            raise ValidationError(_('This operation is not allowed in the safe retry queue.'))
        domain = [('operation', '=', operation), ('state', 'in', ('pending', 'retrying')),
                  ('company_id', '=', (amazon_connection.company_id.id if amazon_connection else shipment.company_id.id))]
        if amazon_connection:
            domain.append(('amazon_connection_id', '=', amazon_connection.id))
        if shipment:
            domain.append(('shipment_id', '=', shipment.id))
        existing = self.sudo().search(domain, limit=1)
        if existing:
            existing.write({'last_error': body_to_text(error, limit=2000)})
            return existing
        return self.sudo().create({
            'name': dict(self._fields['operation'].selection).get(operation, operation),
            'operation': operation, 'company_id': amazon_connection.company_id.id if amazon_connection else shipment.company_id.id,
            'amazon_connection_id': amazon_connection.id if amazon_connection else False,
            'shipment_id': shipment.id if shipment else False, 'last_error': body_to_text(error, limit=2000),
            'next_attempt_at': fields.Datetime.now() + timedelta(minutes=5),
        })

    def _execute_safe_operation(self):
        self.ensure_one()
        if self.operation == 'amazon_orders_sync':
            if not self.amazon_connection_id:
                raise UserError(_('Amazon connection is missing.'))
            self.amazon_connection_id.sync_orders_now()
        elif self.operation == 'amazon_products_sync':
            if not self.amazon_connection_id:
                raise UserError(_('Amazon connection is missing.'))
            self.amazon_connection_id.sync_products_now()
        elif self.operation == 'dhl_tracking_refresh':
            if not self.shipment_id or not self.shipment_id.tracking_number:
                raise UserError(_('Shipment or tracking number is missing.'))
            summary = self.env['dhl.tracking.cron'].refresh_shipments(self.shipment_id)
            if summary.get('failed'):
                raise SafeRetryableError(_('DHL tracking refresh failed.'))
        else:
            raise UserError(_('This operation is not allowed in the safe retry queue.'))

    def action_retry_now(self):
        for rec in self:
            if rec.state == 'done':
                continue
            rec._run_one()
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def _run_one(self):
        self.ensure_one()
        self.write({'state': 'retrying', 'last_attempt_at': fields.Datetime.now()})
        try:
            self._execute_safe_operation()
        except Exception as exc:
            retryable = bool(getattr(exc, 'retryable', False)) or isinstance(exc, requests.RequestException)
            count = self.retry_count + 1
            exhausted = count >= self.max_retries or not retryable
            minutes = min(5 * (2 ** count), 360)
            self.write({'retry_count': count, 'state': 'failed' if exhausted else 'pending',
                        'last_error': body_to_text(str(exc), limit=2000),
                        'next_attempt_at': fields.Datetime.now() + timedelta(minutes=minutes)})
            if exhausted:
                self.env['dhl.notification'].notify('error', _('Automation Retry Failed'),
                    _('%s needs manual attention. See the retry queue for the safe error summary.', self.name), record=self)
            return False
        self.write({'state': 'done', 'last_error': False, 'retry_count': self.retry_count + 1})
        return True

    @api.model
    def _cron_run_due(self, limit=20):
        due = self.sudo().search([('state', '=', 'pending'), ('next_attempt_at', '<=', fields.Datetime.now())],
                                 order='next_attempt_at, id', limit=limit)
        for job in due:
            job._run_one()


class DhlReportWizard(models.TransientModel):
    _name = 'dhl.report.wizard'
    _description = 'Logistics Report Export'

    report_type = fields.Selection([('orders', 'Orders'), ('shipments', 'Shipments'), ('returns', 'Returns')],
                                   required=True, default='orders')
    date_from = fields.Date(required=True, default=lambda self: fields.Date.today() - timedelta(days=30))
    date_to = fields.Date(required=True, default=fields.Date.today)

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for rec in self:
            if rec.date_from and rec.date_to and rec.date_from > rec.date_to:
                raise ValidationError(_('Start date must be on or before end date.'))

    def action_export_csv(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise UserError(_('Start date must be on or before end date.'))
        start = datetime.combine(self.date_from, datetime.min.time())
        end = datetime.combine(self.date_to + timedelta(days=1), datetime.min.time())
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        if self.report_type == 'orders':
            writer.writerow(['Order', 'Amazon Order ID', 'Source', 'Order Date', 'Customer', 'Marketplace', 'Status', 'Currency', 'Total'])
            records = self.env['dhl.order'].search([('company_id', '=', self.env.company.id), ('order_date', '>=', start), ('order_date', '<', end)], order='order_date desc')
            for rec in records:
                writer.writerow([rec.name, rec.amazon_order_id or '', rec.source, fields.Datetime.to_string(rec.order_date) if rec.order_date else '',
                                 rec.customer_id.name, rec.marketplace or '', rec.state, rec.currency_id.name, rec.amount_total])
        elif self.report_type == 'shipments':
            writer.writerow(['Shipment', 'Order', 'Tracking Number', 'Status', 'Created', 'Delivered', 'Shipping Cost', 'Currency'])
            records = self.env['dhl.shipment'].search([('company_id', '=', self.env.company.id), ('create_date', '>=', start), ('create_date', '<', end)], order='create_date desc')
            for rec in records:
                writer.writerow([rec.name, rec.order_id.name or '', rec.tracking_number or '', rec.state,
                                 fields.Datetime.to_string(rec.create_date) if rec.create_date else '',
                                 fields.Datetime.to_string(rec.delivered_at) if 'delivered_at' in rec._fields and rec.delivered_at else '',
                                 rec.shipping_cost, rec.currency_id.name if 'currency_id' in rec._fields else self.env.company.currency_id.name])
        else:
            writer.writerow(['Return', 'Amazon Return ID', 'Order', 'Customer', 'Status', 'Reason', 'Return Tracking', 'Created'])
            records = self.env['dhl.return'].search([('company_id', '=', self.env.company.id), ('create_date', '>=', start), ('create_date', '<', end)], order='create_date desc')
            for rec in records:
                writer.writerow([rec.name, rec.amazon_return_id if 'amazon_return_id' in rec._fields else '', rec.order_id.name,
                                 rec.customer_id.name, rec.state, rec.reason, rec.return_tracking_number or '',
                                 fields.Datetime.to_string(rec.create_date) if rec.create_date else ''])
        filename = 'logistics_%s_%s_%s.csv' % (self.report_type, self.date_from, self.date_to)
        attachment = self.env['ir.attachment'].create({
            'name': filename, 'type': 'binary', 'datas': base64.b64encode(output.getvalue().encode('utf-8')),
            'mimetype': 'text/csv', 'res_model': self._name, 'res_id': self.id,
        })
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id, 'target': 'self'}


class DhlDashboardAmazonStatus(models.AbstractModel):
    _inherit = 'dhl.dashboard'

    @api.model
    def get_connection_status(self):
        result = super().get_connection_status()
        connection = self.env['dhl.amazon.connection'].sudo().search([
            ('active', '=', True), ('company_id', '=', self.env.company.id)], order='sequence, id', limit=1)
        state = connection.state if connection else 'disconnected'
        labels = {
            'connected': _('Amazon Connected'), 'auth_error': _('Amazon Authentication Problem'),
            'api_error': _('Amazon API Problem'), 'disconnected': _('Connect Amazon'),
        }
        result['amazon'] = {
            'connected': bool(connection and state == 'connected'),
            'available': bool(connection), 'state': state,
            'label': labels.get(state, _('Amazon Not Connected')),
            'id': connection.id or False, 'name': connection.name or '', 'region': connection.region or '',
            'environment': connection.environment or 'sandbox',
        }
        return result


class DhlAgentAmazonAutomation(models.Model):
    _inherit = 'dhl.agent'

    @api.model
    def get_subsystems(self, agent=None):
        items = super().get_subsystems(agent)
        amazon = self.env['dhl.amazon.connection'].sudo().search([
            ('active', '=', True), ('company_id', '=', self.env.company.id)], order='sequence, id', limit=1)
        retries = self.env['dhl.retry.job'].sudo().search_count([
            ('company_id', '=', self.env.company.id), ('state', '=', 'pending')])
        tracking_cron = self.env['ir.cron'].sudo().search([
            ('model_id.model', '=', 'dhl.tracking.cron'), ('active', '=', True)], limit=1)
        retry_cron = self.env['ir.cron'].sudo().search([
            ('model_id.model', '=', 'dhl.retry.job'), ('active', '=', True)], limit=1)
        dhl_connection = self.env['dhl.connection'].sudo().search([
            ('company_id', '=', self.env.company.id), ('active', '=', True), ('state', '=', 'connected')], limit=1)
        for item in items:
            if item.get('key') == 'amazon':
                item.update({'status': 'active' if amazon and amazon.state == 'connected' else 'off',
                             'note': _('Connected (%s)', amazon.environment) if amazon and amazon.state == 'connected' else _('Connection/authorization required')})
            elif item.get('key') == 'tracking':
                item.update({'status': 'active' if tracking_cron and dhl_connection else 'off',
                             'note': _('Scheduled; requires a connected DHL account') if tracking_cron else _('Tracking cron is disabled')})
            elif item.get('key') == 'queue':
                item.update({'status': 'planned', 'note': _('Automatic shipment booking queue is not implemented')})
            elif item.get('key') == 'retry':
                item.update({'status': 'active' if retry_cron else 'off',
                             'note': _('%s pending safe retries', retries) if retry_cron else _('Retry cron is disabled')})
        items.append({'key': 'reports', 'label': _('Reports'), 'status': 'active', 'note': _('CSV exports available')})
        return items
