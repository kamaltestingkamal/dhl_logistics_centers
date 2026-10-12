import logging
import re
import time
from datetime import timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .dhl_utils import ERROR_CATEGORIES, DHLAPIError, classify_http_status

_logger = logging.getLogger(__name__)

GROUP_MANAGER = 'dhl_logistics_center.group_dhl_manager'

# DHL Parcel DE (Post & Parcel Germany) OAuth2 "password" (ROPC) token endpoints.
# VERIFY against the current DHL developer portal before going to production.
AUTH_URLS = {
    'sandbox': 'https://api-sandbox.dhl.com/parcel/de/account/auth/ropc/v1/token',
    'production': 'https://api-eu.dhl.com/parcel/de/account/auth/ropc/v1/token',
}
REQUEST_TIMEOUT = 20
TOKEN_SAFETY_MARGIN = 60  # seconds
# Changing any of these invalidates the connection until it is tested again.
CREDENTIAL_FIELDS = {'api_key', 'api_secret', 'username', 'password', 'billing_number', 'environment'}


class DhlConnection(models.Model):
    _name = 'dhl.connection'
    _description = 'DHL Connection'
    _order = 'sequence, id'

    name = fields.Char(required=True, default='DHL Parcel DE')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    environment = fields.Selection(
        [('sandbox', 'Sandbox'), ('production', 'Production')],
        required=True, default='sandbox')

    api_key = fields.Char('API Key', groups=GROUP_MANAGER)
    api_secret = fields.Char('API Secret', groups=GROUP_MANAGER)
    username = fields.Char('Username', groups=GROUP_MANAGER)
    password = fields.Char('Password', groups=GROUP_MANAGER)
    billing_number = fields.Char(
        'Billing Number (14 digits)', groups=GROUP_MANAGER,
        help='10-digit EKP + 2-digit procedure + 2-digit participation, e.g. 33333333330102. No spaces or dashes.')
    tracking_api_key = fields.Char(
        'Tracking API Key', groups=GROUP_MANAGER,
        help='Optional separate Parcel DE Tracking API key. If empty, the main API key is reused.')
    tracking_api_secret = fields.Char(
        'Tracking API Secret', groups=GROUP_MANAGER,
        help='Optional separate Parcel DE Tracking API secret. If empty, the main API secret is reused.')
    tracking_username = fields.Char(
        'Tracking Web-Service Username', groups=GROUP_MANAGER,
        help='Production tracking business-user ID. Sandbox defaults to DHL documented test credentials if empty.')
    tracking_password = fields.Char(
        'Tracking Web-Service Password', groups=GROUP_MANAGER,
        help='Production tracking business-user password. Sandbox defaults to DHL documented test credentials if empty.')

    @api.constrains('billing_number')
    def _check_billing_number(self):
        for rec in self.sudo():
            if rec.billing_number and not re.fullmatch(r'\d{14}', rec.billing_number):
                raise ValidationError(_(
                    "The billing number must be exactly 14 digits (10-digit EKP + 2-digit procedure + "
                    "2-digit participation), without spaces or dashes."))

    state = fields.Selection(
        [('disconnected', 'Disconnected'), ('connected', 'Connected'),
         ('auth_error', 'Authentication problem'), ('api_error', 'API problem')],
        default='disconnected', readonly=True, copy=False)
    error_category = fields.Selection(ERROR_CATEGORIES, readonly=True, copy=False)
    error_retryable = fields.Boolean('Retry may help', readonly=True, copy=False)
    access_token = fields.Char(copy=False, groups=GROUP_MANAGER)
    token_expires_at = fields.Datetime(copy=False)
    last_connected = fields.Datetime(readonly=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def get_active(self):
        """First connected connection (sudo: callers need status, not secrets)."""
        return self.sudo().search([('state', '=', 'connected')], limit=1)

    def write(self, vals):
        if CREDENTIAL_FIELDS & set(vals) and 'state' not in vals:
            vals = dict(vals, state='disconnected', access_token=False, token_expires_at=False)
        return super().write(vals)

    def _check_credentials_filled(self):
        self.ensure_one()
        rec = self.sudo()
        labels = {'api_key': _('API Key'), 'api_secret': _('API Secret'),
                  'username': _('Username'), 'password': _('Password'),
                  'billing_number': _('Billing Number')}
        missing = [label for f, label in labels.items() if not rec[f]]
        if missing:
            raise UserError(_("Please fill in: %s", ', '.join(missing)))

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def _fetch_token(self):
        """Request a token from DHL. Always logs; raises DHLAPIError on failure."""
        self.ensure_one()
        rec = self.sudo()
        url = AUTH_URLS[rec.environment]
        payload = {
            'grant_type': 'password',
            'client_id': rec.api_key,
            'client_secret': rec.api_secret,
            'username': rec.username,
            'password': rec.password,
        }
        started = time.monotonic()
        status, raw, error, token_data = None, '', None, None
        try:
            response = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
            status, raw = response.status_code, response.text
            try:
                data = response.json()
            except ValueError:
                # Non-JSON error pages (HTML 502/503, proxies...) must NOT leak a raw ValueError.
                raise DHLAPIError(
                    _("DHL returned an unreadable (non-JSON) response (HTTP %s).", status),
                    status_code=status, category='invalid_response',
                    retryable=bool(status and status >= 500))
            if not isinstance(data, dict):
                raise DHLAPIError(_("DHL returned an unexpected response format."),
                                  status_code=status, category='invalid_response')
            if status != 200 or not data.get('access_token'):
                detail = (data.get('error_description') or data.get('detail')
                          or data.get('title') or data.get('error') or _('Authentication rejected'))
                category, retryable = classify_http_status(status)
                raise DHLAPIError(
                    _("DHL authentication failed (HTTP %(status)s): %(detail)s", status=status, detail=detail),
                    status_code=status, payload=data, category=category, retryable=retryable)
            token_data = data
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
            name='Authentication (OAuth)', connection_id=rec.id, company_id=rec.company_id.id,
            method='POST', endpoint=url,
            request_body=payload, status_code=status, response_body=raw,
            success=error is None, duration_ms=int((time.monotonic() - started) * 1000),
            error_message=str(error) if error else False)
        if error:
            raise error
        return token_data

    def get_access_token(self, force=False):
        """Return a valid token, reusing the cached one until shortly before expiry."""
        self.ensure_one()
        rec = self.sudo()
        now = fields.Datetime.now()
        if (not force and rec.access_token and rec.token_expires_at
                and rec.token_expires_at > now + timedelta(seconds=TOKEN_SAFETY_MARGIN)):
            return rec.access_token
        data = rec._fetch_token()
        rec.write({
            'access_token': data['access_token'],
            'token_expires_at': now + timedelta(seconds=int(data.get('expires_in') or 300)),
        })
        return data['access_token']

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        self._check_credentials_filled()
        Notification = self.env['dhl.notification']
        try:
            self.get_access_token(force=True)
        except DHLAPIError as exc:
            self.sudo().write({
                'state': 'auth_error' if exc.category == 'authentication' else 'api_error',
                'last_error': str(exc), 'error_category': exc.category, 'error_retryable': exc.retryable,
                'access_token': False, 'token_expires_at': False})
            Notification.notify('error', _('DHL Connection Failed'), self._failure_hint(exc), record=self)
        else:
            self.sudo().write({'state': 'connected', 'last_connected': fields.Datetime.now(),
                               'last_error': False, 'error_category': False, 'error_retryable': False})
            Notification.notify('success', _('DHL Connected Successfully'),
                                _('DHL Parcel DE API connection is now active.'), record=self)
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    @api.model
    def _failure_hint(self, exc):
        """User-facing toast text. Never contains secrets; technical detail stays in last_error / logs."""
        if exc.category in ('network', 'timeout'):
            return _("DHL could not be reached. This is usually temporary - try again in a moment.")
        return _("Please check credentials or API configuration.")

    def action_disconnect(self):
        self.sudo().write({'state': 'disconnected', 'access_token': False,
                           'token_expires_at': False})
        self.env['dhl.notification'].notify(
            'warning', _('DHL Disconnected'), _('Shipping operations are paused until you reconnect.'))
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}
