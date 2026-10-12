import logging
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

from odoo import _, api, fields, models

from .dhl_utils import (
    DHLAPIError,
    classify_http_status,
    normalize_tracking_status,
    body_to_text,
)

_logger = logging.getLogger(__name__)

# DHL Parcel DE Tracking API - Customer Integration Testing
TRACKING_URLS = {
    'sandbox': 'https://api-sandbox.dhl.com/parcel/de/tracking/v0/shipments',
    'production': 'https://api-eu.dhl.com/parcel/de/tracking/v0/shipments',
}

REQUEST_TIMEOUT = 20
TRACKING_BATCH = 10
TRACKING_MIN_INTERVAL_MIN = 60

NORMALIZED_STATUSES = [
    ('pre_transit', 'Pre-transit'),
    ('in_transit', 'In Transit'),
    ('out_for_delivery', 'Out for Delivery'),
    ('delivered', 'Delivered'),
    ('delivery_failed', 'Delivery Failed'),
    ('returned', 'Returned'),
    ('exception', 'Exception'),
    ('unknown', 'Unknown'),
]


class DhlTracking(models.Model):
    """One tracking event of a shipment."""

    _name = 'dhl.tracking'
    _description = 'Tracking Event'
    _order = 'event_time desc, id desc'

    shipment_id = fields.Many2one(
        'dhl.shipment',
        required=True,
        ondelete='cascade',
        index=True,
    )

    company_id = fields.Many2one(
        related='shipment_id.company_id',
        store=True,
        index=True,
    )

    tracking_number = fields.Char(
        related='shipment_id.tracking_number',
        store=True,
        index=True,
    )

    event_time = fields.Datetime(
        default=fields.Datetime.now,
        required=True,
    )

    status_code = fields.Char('DHL Status Code')
    status_text = fields.Char('Status Text')

    normalized_status = fields.Selection(
        NORMALIZED_STATUSES,
        compute='_compute_normalized_status',
        store=True,
        index=True,
    )

    location = fields.Char()

    source = fields.Selection(
        [
            ('dhl', 'DHL API'),
            ('manual', 'Manual'),
        ],
        default='manual',
        required=True,
    )

    raw_payload = fields.Text()

    @api.depends('status_text', 'status_code')
    def _compute_normalized_status(self):
        for record in self:
            record.normalized_status = normalize_tracking_status(
                record.status_text,
                record.status_code,
            )

    @api.model_create_multi
    def create(self, vals_list):
        """Limit stored raw payload size before saving."""
        for vals in vals_list:
            if vals.get('raw_payload'):
                vals['raw_payload'] = body_to_text(
                    vals['raw_payload'],
                    limit=8000,
                )

        records = super().create(vals_list)

        shipments = records.mapped('shipment_id')
        if shipments:
            shipments._apply_latest_tracking()

        return records


class DhlTrackingCron(models.AbstractModel):
    """Tracking synchronization from DHL."""

    _name = 'dhl.tracking.cron'
    _description = 'DHL Tracking Sync'

    @api.model
    def _get_shipments_to_track(self, limit=TRACKING_BATCH):
        """Find shipments that are due for a tracking refresh."""
        threshold = fields.Datetime.now() - timedelta(
            minutes=TRACKING_MIN_INTERVAL_MIN
        )

        return self.env['dhl.shipment'].search([
            ('tracking_number', '!=', False),
            ('state', 'in', ('created', 'in_transit', 'delivery_failed')),
            ('connection_id.active', '=', True),
            ('connection_id.state', '=', 'connected'),
            '|',
            ('last_tracking_sync', '=', False),
            ('last_tracking_sync', '<', threshold),
        ], order='last_tracking_sync asc, id', limit=limit)

    @api.model
    def _call_dhl_tracking_api(self, shipment):
        """Call DHL Parcel DE Tracking API and return its XML response."""
        connection = shipment.connection_id.sudo()

        api_key = connection.tracking_api_key or connection.api_key
        api_secret = connection.tracking_api_secret or connection.api_secret
        if not api_key or not api_secret:
            raise DHLAPIError(
                _('DHL Tracking API Key or API Secret is missing.'),
                category='configuration',
                retryable=False,
            )

        # DHL documents these public-use credentials for the sandbox only.
        # Production uses the business-customer tracking user configured by the operator.
        if connection.environment == 'sandbox':
            tracking_user = connection.tracking_username or 'zt12345'
            tracking_password = connection.tracking_password or 'geheim'
            request_name = 'get-status-for-public-user'
        else:
            tracking_user = connection.tracking_username or connection.username
            tracking_password = connection.tracking_password or connection.password
            if not tracking_user or not tracking_password:
                raise DHLAPIError(
                    _('Configure the DHL business-customer tracking username and password for production.'),
                    category='configuration', retryable=False,
                )
            request_name = 'd-get-piece-detail'

        root = ET.Element('data', {
            'request': request_name,
            'appname': tracking_user,
            'password': tracking_password,
            'language-code': 'en',
            'piece-code': shipment.tracking_number,
        })

        if shipment.consignee_zip:
            root.set('zip-code', shipment.consignee_zip)

        xml_request = ET.tostring(
            root,
            encoding='unicode',
            short_empty_elements=True,
        )

        headers = {
            'Accept': 'application/xml',
            'DHL-API-Key': api_key,
        }

        started = time.monotonic()
        status = None
        raw = ''
        error = None
        data = None

        try:
            response = requests.get(
                TRACKING_URLS[connection.environment],
                params={'xml': xml_request},
                headers=headers,
                auth=(
                    api_key,
                    api_secret,
                ),
                timeout=REQUEST_TIMEOUT,
            )

            status = response.status_code
            raw = response.text

            if status not in (200, 404):
                category, retryable = classify_http_status(status)

                raise DHLAPIError(
                    _(
                        'DHL tracking failed (HTTP %(status)s): %(reason)s',
                        status=status,
                        reason=raw[:1000] or _('No details returned'),
                    ),
                    status_code=status,
                    payload=raw[:8000],
                    category=category,
                    retryable=retryable,
                )

            if status == 200:
                try:
                    data = ET.fromstring(raw)
                except ET.ParseError:
                    raise DHLAPIError(
                        _('DHL returned an invalid XML response.'),
                        status_code=status,
                        payload=raw[:8000],
                        category='invalid_response',
                        retryable=False,
                    )
                # DHL may report application-level errors inside HTTP 200 XML.
                # Do not log an authentication failure as a successful tracking call.
                response_code = (data.attrib.get('code') or '').strip()
                response_error = (data.attrib.get('error') or data.attrib.get('message') or '').strip()
                if response_code in {'5', '6', '62'}:
                    raise DHLAPIError(
                        _('DHL tracking authorization failed (code %(code)s): %(detail)s',
                          code=response_code, detail=response_error or _('Check tracking API key and web-service credentials.')),
                        status_code=status, payload={'code': response_code},
                        category='authentication', retryable=False)
                if response_code in {'-1000', '-2', '-1'}:
                    raise DHLAPIError(
                        _('DHL tracking service reported a temporary backend error (code %s).', response_code),
                        status_code=status, payload={'code': response_code},
                        category='api', retryable=True)

        except DHLAPIError as exc:
            error = exc

        except requests.Timeout:
            error = DHLAPIError(
                _(
                    'DHL did not respond in time (timeout after %s seconds).',
                    REQUEST_TIMEOUT,
                ),
                category='timeout',
                retryable=True,
            )

        except requests.ConnectionError:
            error = DHLAPIError(
                _("Could not reach DHL. Check the server's internet connection."),
                category='network',
                retryable=True,
            )

        except requests.RequestException as exc:
            error = DHLAPIError(
                _('Network error while contacting DHL: %s', exc),
                category='network',
                retryable=True,
            )

        self.env['dhl.api.log'].log_call(
            name='Tracking',
            connection_id=connection.id,
            company_id=connection.company_id.id,
            method='GET',
            endpoint=TRACKING_URLS[connection.environment],
            request_body=None,
            status_code=status,
            response_body=raw,
            success=error is None,
            duration_ms=int(
                (time.monotonic() - started) * 1000
            ),
            error_message=str(error) if error else False,
            res_model='dhl.shipment',
            res_id=shipment.id,
        )

        if error:
            raise error

        return None if status == 404 else data

    @staticmethod
    def _parse_timestamp(value):
        """Parse supported timestamps and return naive UTC for Odoo."""
        if not value or not isinstance(value, str):
            return None

        value = value.strip()

        # Try ISO 8601 first.
        try:
            parsed = datetime.fromisoformat(
                value.replace('Z', '+00:00')
            )
        except ValueError:
            parsed = None

        # Try common DHL date/time formats.
        if parsed is None:
            for fmt in (
                '%d.%m.%Y %H:%M:%S',
                '%d.%m.%Y %H:%M',
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d',
            ):
                try:
                    parsed = datetime.strptime(value, fmt)
                    break
                except ValueError:
                    continue

        if parsed is None:
            return None

        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(
                tzinfo=None
            )

        return parsed

    @api.model
    def _parse_tracking_events(self, tracking_data, shipment):
        """Parse XML event attributes and save valid tracking events."""
        if tracking_data is None:
            return 0

        def local_name(tag):
            return tag.rsplit('}', 1)[-1].lower()

        def get_attribute(element, *names):
            """Read an attribute case-insensitively."""
            wanted = {name.lower() for name in names}

            for key, value in element.attrib.items():
                if key.lower() in wanted and value:
                    return value.strip()

            return ''

        def get_child_text(element, *names):
            """Read matching XML child element text."""
            wanted = {name.lower() for name in names}

            for child in element.iter():
                if local_name(child.tag) in wanted and child.text:
                    value = child.text.strip()
                    if value:
                        return value

            return ''

        # Find XML nodes representing individual tracking events.
        event_nodes = []

        for node in tracking_data.iter():
            node_name = local_name(node.tag)
            attr_name = get_attribute(node, 'name').lower()

            if (
                node_name in ('pieceevent', 'piece-event', 'event')
                or attr_name in ('pieceevent', 'piece-event')
            ):
                event_nodes.append(node)

        # Do not fabricate events if the API returns no history.
        Tracking = self.env['dhl.tracking']
        created = 0

        for node in event_nodes:
            status_text = (
                get_attribute(
                    node,
                    'event-text',
                    'event-status',
                    'status',
                    'short-status',
                )
                or get_child_text(
                    node,
                    'event-text',
                    'event-status',
                    'status',
                )
            )

            timestamp = (
                get_attribute(
                    node,
                    'event-timestamp',
                    'timestamp',
                    'status-timestamp',
                )
                or get_child_text(
                    node,
                    'event-timestamp',
                    'timestamp',
                )
            )

            # Ignore nodes without actual event data.
            if not status_text or not timestamp:
                continue

            when = self._parse_timestamp(timestamp)

            if not when:
                _logger.warning(
                    'Unrecognized DHL event timestamp %r for shipment %s',
                    timestamp,
                    shipment.name,
                )
                continue

            status_code = (
                get_attribute(
                    node,
                    'standard-event-code',
                    'event-code',
                    'status-code',
                    'ice',
                )
                or get_child_text(
                    node,
                    'standard-event-code',
                    'event-code',
                    'status-code',
                )
            )

            location = (
                get_attribute(
                    node,
                    'event-location',
                    'location',
                    'event-country',
                )
                or get_child_text(
                    node,
                    'event-location',
                    'location',
                    'event-country',
                )
            )

            existing = Tracking.search_count([
                ('shipment_id', '=', shipment.id),
                ('event_time', '=', when),
                ('status_text', '=', status_text),
            ])

            if existing:
                continue

            Tracking.create({
                'shipment_id': shipment.id,
                'event_time': when,
                'status_text': status_text,
                'status_code': status_code,
                'location': location,
                'source': 'dhl',
                'raw_payload': ET.tostring(
                    node,
                    encoding='unicode',
                ),
            })

            created += 1

        _logger.info(
            'DHL tracking parsed %s new events for shipment %s',
            created,
            shipment.name,
        )

        return created

    @api.model
    def refresh_shipments(self, shipments):
        """Refresh tracking for shipments without aborting the whole batch."""
        summary = {
            'processed': 0,
            'failed': 0,
            'events': 0,
        }

        for index, shipment in enumerate(shipments):
            if index:
                # Keep requests at approximately one per second.
                time.sleep(1.1)

            summary['processed'] += 1

            try:
                data = self._call_dhl_tracking_api(shipment)

                summary['events'] += self._parse_tracking_events(
                    data,
                    shipment,
                )

                shipment.last_tracking_sync = fields.Datetime.now()

            except DHLAPIError as exc:
                summary['failed'] += 1
                shipment.last_tracking_sync = fields.Datetime.now()

                _logger.warning(
                    'Tracking failed for %s: %s',
                    shipment.name,
                    exc,
                )

                if exc.status_code == 429:
                    # Stop when the API quota/rate limit is reached.
                    break
            except Exception as exc:  # Keep a malformed record from aborting the rest of the batch.
                summary['failed'] += 1
                shipment.last_tracking_sync = fields.Datetime.now()
                _logger.exception('Unexpected tracking failure for shipment %s', shipment.name)
                self.env['dhl.automation.log'].sudo().create({
                    'name': _('DHL Tracking Record Failure'), 'state': 'failed',
                    'finished_at': fields.Datetime.now(),
                    'message': body_to_text('Unexpected tracking failure: %s' % exc, limit=1000),
                })

        return summary

    @api.model
    def _cron_update_tracking(self):
        """Scheduled tracking synchronization."""
        shipments = self._get_shipments_to_track()

        if not shipments:
            return

        summary = self.refresh_shipments(shipments)

        if not summary['failed']:
            state = 'success'
        elif summary['failed'] == summary['processed']:
            state = 'failed'
        else:
            state = 'partial'

        self.env['dhl.automation.log'].sudo().create({
            'name': _('DHL Tracking Sync'),
            'state': state,
            'finished_at': fields.Datetime.now(),
            'message': _(
                'Processed %(p)s shipments, %(f)s failed, %(e)s new events.',
                p=summary['processed'],
                f=summary['failed'],
                e=summary['events'],
            ),
        })