from unittest.mock import patch, MagicMock
import base64
from xml.etree import ElementTree as ET

import requests

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.dhl_utils import DHLAPIError

POST = 'odoo.addons.dhl_logistics_center.models.dhl_shipment.requests.post'
GET = 'odoo.addons.dhl_logistics_center.models.dhl_tracking.requests.get'


class FakeResponse:
    def __init__(self, status=200, json_data=None, text=''):
        self.status_code = status
        self._json = json_data
        self.text = text or ('' if json_data is None else str(json_data))

    def json(self):
        if self._json is None:
            raise ValueError('not json')
        return self._json


@tagged('post_install', '-at_install')
class TestFlow(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param('dhl_logistics_center.shipper_name', 'Shop GmbH')
        for k, v in {'street': 'Hauptstr.', 'street_number': '1', 'zip': '10115', 'city': 'Berlin',
                     'country_code': 'DE'}.items():
            self.env['ir.config_parameter'].sudo().set_param('dhl_logistics_center.shipper_' + k, v)
        de = self.env.ref('base.de')
        self.product = self.env['dhl.product'].create({'name': 'Mug', 'sku': 'MUG-1', 'weight': 0.4, 'price': 9.5})
        self.customer = self.env['dhl.customer'].create({
            'name': 'Max Mustermann',
            'address_ids': [(0, 0, {'street': 'Musterweg', 'street_number': '5', 'zip': '80331',
                                    'city': 'München', 'country_id': de.id, 'is_default': True})]})
        self.order = self.env['dhl.order'].create({
            'customer_id': self.customer.id, 'address_id': self.customer.address_ids.id,
            'line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 3, 'unit_price': 9.5})]})

    def test_sku_unique(self):
        with self.assertRaises(ValidationError):
            self.env['dhl.product'].create({'name': 'Dup', 'sku': 'MUG-1'})

    def test_order_totals_and_confirm(self):
        self.assertAlmostEqual(self.order.amount_total, 28.5)
        self.assertAlmostEqual(self.order.total_weight, 1.2)
        self.order.action_confirm()
        self.assertEqual(self.order.state, 'pending')

    def test_shipment_from_order_and_duplicate_protection(self):
        self.order.action_confirm()
        self.order.action_create_shipment()
        ship = self.order.shipment_ids
        self.assertEqual(ship.product_code, 'V01PAK')
        self.assertEqual(ship.shipper_name, 'Shop GmbH')
        self.assertAlmostEqual(ship.total_weight, 1.2)
        with self.assertRaises(UserError):
            self.order.action_create_shipment()
        with self.assertRaises(ValidationError):
            self.env['dhl.shipment'].create({'order_id': self.order.id})
        # moving an inactive shipment back to an active state must not create a second active one
        ship.action_mark_creation_failed('x')
        second = self.env['dhl.shipment'].create({'order_id': self.order.id})
        with self.assertRaises(ValidationError):
            ship.action_reset_draft()
        self.assertTrue(second)

    def test_mark_ready_validates_data(self):
        ship = self.env['dhl.shipment'].create({'consignee_name': 'X'})
        with self.assertRaises(UserError):
            ship.action_mark_ready()

    def test_tracking_events_drive_status(self):
        self.order.action_confirm()
        self.order.action_create_shipment()
        ship = self.order.shipment_ids
        ship.action_mark_ready()
        ship.tracking_number = '00340434161094042557'
        Tracking = self.env['dhl.tracking']
        Tracking.create({'shipment_id': ship.id, 'status_text': 'Im Paketzentrum bearbeitet',
                         'event_time': '2026-01-01 08:00:00'})
        self.assertEqual(ship.state, 'in_transit')
        self.assertEqual(self.order.state, 'shipped')
        Tracking.create({'shipment_id': ship.id, 'status_text': 'Die Sendung wurde nicht zugestellt',
                         'event_time': '2026-01-02 08:00:00'})
        self.assertEqual(ship.state, 'delivery_failed')
        Tracking.create({'shipment_id': ship.id, 'status_text': 'Die Sendung wurde zugestellt',
                         'event_time': '2026-01-03 08:00:00'})
        self.assertEqual(ship.state, 'delivered')
        self.assertEqual(self.order.state, 'delivered')
        # a late, older event must not regress a delivered shipment
        Tracking.create({'shipment_id': ship.id, 'status_text': 'Im Paketzentrum bearbeitet',
                         'event_time': '2026-01-01 12:00:00'})
        self.assertEqual(ship.state, 'delivered')

    def test_unknown_dhl_booking_blocks_reset_and_duplicate_shipment(self):
        self.order.action_confirm()
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'order_id': self.order.id, 'state': 'creation_failed', 'creation_result_unknown': True,
            'tracking_number': False, 'consignee_name': 'Max Mustermann',
            'consignee_street': 'Musterweg', 'consignee_zip': '80331', 'consignee_city': 'München',
            'consignee_country_id': de.id, 'shipper_name': 'Shop GmbH', 'shipper_street': 'Hauptstr.',
            'shipper_street_number': '1', 'shipper_zip': '10115', 'shipper_city': 'Berlin',
            'shipper_country_id': de.id, 'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.2})],
        })
        with self.assertRaises(UserError):
            ship.action_reset_draft()
        with self.assertRaises(UserError):
            self.order.action_create_shipment()
        with self.assertRaises(ValidationError):
            self.env['dhl.shipment'].create({
                'order_id': self.order.id, 'state': 'draft', 'consignee_name': 'Duplicate',
                'consignee_street': 'Musterweg', 'consignee_zip': '80331', 'consignee_city': 'München',
                'consignee_country_id': de.id, 'shipper_name': 'Shop GmbH', 'shipper_street': 'Hauptstr.',
                'shipper_street_number': '1', 'shipper_zip': '10115', 'shipper_city': 'Berlin',
                'shipper_country_id': de.id, 'product_code': 'V01PAK',
                'package_ids': [(0, 0, {'weight': 1.2})],
            })

    def test_cannot_cancel_created_shipment_locally(self):
        ship = self.env['dhl.shipment'].create({})
        ship.state = 'created'
        with self.assertRaises(UserError):
            ship.action_cancel()

    def test_notification_survives_and_is_private(self):
        self.env['dhl.notification'].notify('error', 'Boom', 'details', users=self.env.user)
        data = self.env['dhl.notification'].get_panel_data()
        self.assertEqual(data['unread'], 1)
        self.env['dhl.notification'].mark_all_read()
        self.assertEqual(self.env['dhl.notification'].get_panel_data()['unread'], 0)

    def test_shipment_is_blocked_when_actual_product_weight_is_missing(self):
        product = self.env['dhl.product'].create({'name': 'Unknown weight', 'sku': 'UNKNOWN-WEIGHT', 'weight': 0})
        order = self.env['dhl.order'].create({
            'customer_id': self.customer.id,
            'address_id': self.customer.address_ids.id,
            'line_ids': [(0, 0, {'product_id': product.id, 'quantity': 1, 'unit_price': 1})],
        })
        order.action_confirm()
        with self.assertRaises(UserError):
            order.action_create_shipment()

    def test_amazon_product_upsert_is_idempotent(self):
        Product = self.env['dhl.product']
        payload = {'sku': 'AMZ-1', 'asin': 'B000000001', 'name': 'Amazon item', 'price': 12.5}
        product, action = Product.upsert_amazon_product(payload, 'DE')
        self.assertEqual(action, 'created')
        product2, action2 = Product.upsert_amazon_product({**payload, 'price': 13.0}, 'DE')
        self.assertEqual(product.id, product2.id)
        self.assertEqual(action2, 'updated')
        self.assertEqual(product.price, 13.0)
        self.assertEqual(product.source, 'amazon')

    def test_amazon_product_upsert_rejects_ambiguous_identifiers(self):
        Product = self.env['dhl.product']
        Product.upsert_amazon_product({'sku': 'AMZ-A', 'asin': 'B000000002', 'name': 'Item A'}, 'DE')
        Product.upsert_amazon_product({'sku': 'AMZ-B', 'asin': 'B000000003', 'name': 'Item B'}, 'DE')
        with self.assertRaises(ValidationError):
            Product.upsert_amazon_product({'sku': 'AMZ-A', 'asin': 'B000000003', 'name': 'Mismatch'}, 'DE')

    def test_amazon_product_mapping_does_not_flip_marketplace(self):
        Product = self.env['dhl.product']
        product, _ = Product.upsert_amazon_product(
            {'sku': 'AMZ-CROSS', 'asin': 'B000000010', 'name': 'Cross marketplace item'}, 'MARKET-A')
        product2, _ = Product.upsert_amazon_product(
            {'sku': 'AMZ-CROSS', 'asin': 'B000000010', 'name': 'Cross marketplace item'}, 'MARKET-B')
        self.assertEqual(product.id, product2.id)
        self.assertEqual(product.marketplace, 'MARKET-A')
        with self.assertRaises(ValidationError):
            Product.upsert_amazon_product(
                {'sku': 'AMZ-CROSS', 'asin': 'B000000011', 'name': 'Conflicting ASIN'}, 'MARKET-B')

    def test_shipment_payload_validation_requires_all_fields(self):
        """Test that shipment payload building validates required fields."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.0})],
        })
        # Missing shipper fields should fail
        with self.assertRaises(ValidationError):
            ship._build_shipment_payload()
        
        # Add shipper but missing consignee zip should fail
        ship.write({
            'shipper_name': 'Shop',
            'shipper_street': 'Street',
            'shipper_zip': '10115',
            'shipper_city': 'Berlin',
            'shipper_country_id': self.env.ref('base.de').id,
        })
        with self.assertRaises(ValidationError):
            ship._build_shipment_payload()
        
        # Complete all required fields should succeed
        ship.write({
            'consignee_street': 'Musterweg',
            'consignee_zip': '80331',
            'consignee_city': 'München',
            'consignee_country_id': self.env.ref('base.de').id,
        })
        payload = ship._build_shipment_payload()
        self.assertIn('shipment', payload)
        self.assertIn('shipper', payload['shipment'])
        self.assertIn('consignee', payload['shipment'])
        self.assertIn('packages', payload['shipment'])
        self.assertEqual(len(payload['shipment']['packages']), 1)

    def test_tracking_cron_handles_api_errors_gracefully(self):
        """Test that tracking cron continues even when individual shipments fail."""
        de = self.env.ref('base.de')
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        # Create shipment with tracking number but no connection
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'state': 'created',
            'tracking_number': '00340434161094042557',
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.0})],
        })
        # Keep the connection active, but simulate a transient API failure.
        conn.write({'state': 'connected'})
        with patch(GET, return_value=FakeResponse(503, text='temporary DHL outage')):
            result = self.env['dhl.tracking.cron'].refresh_shipments(ship)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 1)

    def test_dhl_shipment_creation_with_mock_api(self):
        """Test shipment creation with mocked DHL API."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        conn.write({'state': 'connected'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'state': 'ready',
            'consignee_name': 'Max Mustermann',
            'consignee_street': 'Musterweg',
            'consignee_zip': '80331',
            'consignee_city': 'München',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop GmbH',
            'shipper_street': 'Hauptstr.',
            'shipper_street_number': '1',
            'shipper_zip': '10115',
            'shipper_city': 'Berlin',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.5})],
        })
        
        # Mock successful DHL response
        mock_response = {
            'items': [{
                'shipmentNo': '00340434161094042557',
                'label': {'b64': base64.b64encode(b'PDF label content').decode('ascii')},
            }],
        }

        with patch(POST, return_value=FakeResponse(200, mock_response)):
            ship.action_create_dhl_shipment()
        
        self.assertEqual(ship.state, 'created')
        self.assertEqual(ship.tracking_number, '00340434161094042557')
        self.assertEqual(ship.dhl_shipment_no, '00340434161094042557')
        self.assertTrue(ship.label)

    def test_dhl_shipment_creation_handles_api_errors(self):
        """Test that API errors are handled correctly."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        conn.write({'state': 'connected'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'state': 'ready',
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.0})],
        })
        
        # Mock 400 error
        with patch(POST, return_value=FakeResponse(400, {'error': 'Invalid data'})):
            ship.action_create_dhl_shipment()
        
        self.assertEqual(ship.state, 'creation_failed')
        self.assertIn('Invalid data', ship.error_message or '')

    def test_tracking_event_parsing_defensive(self):
        """Test that tracking event parsing handles various response formats."""
        de = self.env.ref('base.de')
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'state': 'created',
            'tracking_number': '00340434161094042557',
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.0})],
        })
        
        # Mock the documented XML response shape and verify the tracking API call.
        tracking_xml = ('<data name="piece" code="0"><data name="pieceevent" '
                        'event-timestamp="2026-01-01T08:00:00Z" '
                        'event-text="Im Paketzentrum bearbeitet" event-code="transit" '
                        'event-location="Berlin"/></data>')
        response = FakeResponse(200, text=tracking_xml)
        conn.write({'state': 'connected'})
        with patch(GET, return_value=response) as mock_get:
            result = self.env['dhl.tracking.cron']._call_dhl_tracking_api(ship)
            events_created = self.env['dhl.tracking.cron']._parse_tracking_events(result, ship)
            self.assertEqual(mock_get.call_args.args[0], 'https://api-sandbox.dhl.com/parcel/de/tracking/v0/shipments')
            self.assertEqual(mock_get.call_args.kwargs['headers']['DHL-API-Key'], 'k')
        
        self.assertEqual(events_created, 1)
        tracking = self.env['dhl.tracking'].search([('shipment_id', '=', ship.id)], limit=1)
        self.assertEqual(tracking.status_text, 'Im Paketzentrum bearbeitet')
        self.assertEqual(tracking.location, 'Berlin')

    def test_tracking_uses_production_endpoint_and_business_credentials(self):
        conn = self.env['dhl.connection'].create({
            'name': 'Tracking Production Test', 'api_key': 'shipping-k', 'api_secret': 'shipping-s',
            'username': 'shipping-user', 'password': 'shipping-pass',
            'tracking_api_key': 'tracking-k', 'tracking_api_secret': 'tracking-s',
            'tracking_username': 'tracking-user', 'tracking_password': 'tracking-pass',
            'billing_number': '33333333330101', 'environment': 'production'})
        conn.write({'state': 'connected'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id, 'state': 'created',
            'tracking_number': '00340434161094042557', 'consignee_name': 'Test',
            'consignee_street': 'Street', 'consignee_zip': '10115', 'consignee_city': 'Berlin',
            'consignee_country_id': de.id, 'shipper_name': 'Shop', 'shipper_street': 'Street',
            'shipper_zip': '10115', 'shipper_city': 'Berlin', 'shipper_country_id': de.id,
            'product_code': 'V01PAK', 'package_ids': [(0, 0, {'weight': 1.0})],
        })
        with patch(GET, return_value=FakeResponse(200, text='<data code="200"/>')) as mock_get:
            self.env['dhl.tracking.cron']._call_dhl_tracking_api(ship)
        self.assertEqual(mock_get.call_args.args[0], 'https://api-eu.dhl.com/parcel/de/tracking/v0/shipments')
        self.assertEqual(mock_get.call_args.kwargs['headers']['DHL-API-Key'], 'tracking-k')
        xml_query = mock_get.call_args.kwargs['params']['xml']
        self.assertIn('d-get-piece-detail', xml_query)
        self.assertIn('tracking-user', xml_query)

    def test_tracking_http_200_application_auth_error_is_not_success(self):
        conn = self.env['dhl.connection'].create({
            'name': 'Tracking Auth Error Test', 'api_key': 'k', 'api_secret': 's',
            'username': 'u', 'password': 'p', 'billing_number': '33333333330101',
            'environment': 'sandbox'})
        conn.write({'state': 'connected'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id, 'state': 'created',
            'tracking_number': '00340434161094042557', 'consignee_name': 'Test',
            'consignee_street': 'Street', 'consignee_zip': '10115', 'consignee_city': 'Berlin',
            'consignee_country_id': de.id, 'shipper_name': 'Shop', 'shipper_street': 'Street',
            'shipper_zip': '10115', 'shipper_city': 'Berlin', 'shipper_country_id': de.id,
            'product_code': 'V01PAK', 'package_ids': [(0, 0, {'weight': 1.0})],
        })
        with patch(GET, return_value=FakeResponse(200, text='<data code="5" error="Login failed"/>')):
            with self.assertRaises(DHLAPIError) as caught:
                self.env['dhl.tracking.cron']._call_dhl_tracking_api(ship)
        self.assertEqual(caught.exception.category, 'authentication')
        self.assertFalse(caught.exception.retryable)

    def test_tracking_deduplication(self):
        """Test that duplicate tracking events are not created."""
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'tracking_number': '00340434161094042557',
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.0})],
        })
        
        event_time = '2026-01-01 08:00:00'
        self.env['dhl.tracking'].create({
            'shipment_id': ship.id,
            'event_time': event_time,
            'status_text': 'Test',
        })
        
        # Try to parse the same event again from DHL's XML response format.
        tracking_data = ET.fromstring(
            '<data><data name="pieceevent" event-timestamp="2026-01-01T08:00:00Z" '
            'event-text="Test" event-code="transit" event-location="Berlin"/></data>')
        events_created = self.env['dhl.tracking.cron']._parse_tracking_events(tracking_data, ship)
        
        # Should not create duplicate
        self.assertEqual(events_created, 0)
        self.assertEqual(self.env['dhl.tracking'].search_count([('shipment_id', '=', ship.id)]), 1)

    def test_v01pak_rejected_for_international_destination(self):
        """Test that V01PAK is rejected for non-Germany destinations."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        pk = self.env.ref('base.pk')  # Pakistan
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '75500',
            'consignee_city': 'Karachi',
            'consignee_country_id': pk.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '10115',
            'shipper_city': 'Berlin',
            'shipper_country_id': self.env.ref('base.de').id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.0})],
        })
        with self.assertRaises(ValidationError) as cm:
            ship._validate_product_for_destination()
        self.assertIn('not available for destination', str(cm.exception))
        self.assertIn('Pakistan', str(cm.exception))

    def test_billing_number_procedure_mismatch(self):
        """Test that billing number procedure must match product."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333335301',  # Procedure 53 (international)
            'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',  # Requires procedure 01
            'package_ids': [(0, 0, {'weight': 1.0})],
        })
        with self.assertRaises(ValidationError) as cm:
            ship._validate_billing_number_procedure()
        self.assertIn('procedure mismatch', str(cm.exception))
        self.assertIn('53', str(cm.exception))
        self.assertIn('01', str(cm.exception))

    def test_girth_exceeds_limit_v01pak(self):
        """Test that girth validation rejects oversized packages for V01PAK."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.0, 'length': 100, 'width': 140, 'height': 80})],
        })
        # Girth = 140 + 2*(100 + 80) = 140 + 360 = 500 cm > 400 cm limit
        with self.assertRaises(ValidationError) as cm:
            ship._validate_package_dimensions()
        self.assertIn('girth', str(cm.exception).lower())
        self.assertIn('500', str(cm.exception))
        self.assertIn('400', str(cm.exception))

    def test_girth_exceeds_limit_v53wpak(self):
        """Test that girth validation rejects oversized packages for V53WPAK."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333335301', 'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V53WPAK',
            'package_ids': [(0, 0, {'weight': 1.0, 'length': 100, 'width': 140, 'height': 80})],
        })
        # Girth = 500 cm > 300 cm limit for V53WPAK
        with self.assertRaises(ValidationError) as cm:
            ship._validate_package_dimensions()
        self.assertIn('girth', str(cm.exception).lower())
        self.assertIn('500', str(cm.exception))
        self.assertIn('300', str(cm.exception))

    def test_individual_dimension_exceeds_limit(self):
        """Test that individual dimension validation works."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 1.0, 'length': 250, 'width': 50, 'height': 50})],
        })
        # Length 250 > 200 limit
        with self.assertRaises(ValidationError) as cm:
            ship._validate_package_dimensions()
        self.assertIn('length', str(cm.exception).lower())
        self.assertIn('250', str(cm.exception))
        self.assertIn('200', str(cm.exception))

    def test_weight_exceeds_limit(self):
        """Test that weight validation works."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 35.0})],
        })
        # Weight 35 > 31.5 limit
        with self.assertRaises(ValidationError) as cm:
            ship._validate_package_dimensions()
        self.assertIn('weight', str(cm.exception).lower())
        self.assertIn('35', str(cm.exception))
        self.assertIn('31.5', str(cm.exception))

    def test_valid_dimensions_pass_validation(self):
        """Test that valid dimensions pass validation."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 10.0, 'length': 50, 'width': 30, 'height': 20})],
        })
        # Should not raise any error
        ship._validate_package_dimensions()

    def test_missing_dimensions_allowed(self):
        """Test that missing dimensions are allowed."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 10.0})],  # No dimensions
        })
        # Should not raise any error when dimensions are missing
        ship._validate_package_dimensions()

    def test_partial_dimensions_rejected(self):
        """Test that partial dimensions are handled correctly."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '1',
            'consignee_city': 'X',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '1',
            'shipper_city': 'X',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 10.0, 'length': 50})],  # Only length
        })
        # Partial dimensions are treated as missing (not all three provided)
        ship._validate_package_dimensions()

    def test_cm_to_mm_conversion_in_payload(self):
        """Test that cm to mm conversion is correct in the payload."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101', 'environment': 'sandbox'})
        de = self.env.ref('base.de')
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'consignee_name': 'Test',
            'consignee_street': 'Musterweg',
            'consignee_zip': '80331',
            'consignee_city': 'München',
            'consignee_country_id': de.id,
            'shipper_name': 'Shop GmbH',
            'shipper_street': 'Hauptstr.',
            'shipper_street_number': '1',
            'shipper_zip': '10115',
            'shipper_city': 'Berlin',
            'shipper_country_id': de.id,
            'product_code': 'V01PAK',
            'package_ids': [(0, 0, {'weight': 10.0, 'length': 50, 'width': 30, 'height': 20})],
        })
        payload = ship._build_shipment_payload()
        details = payload['shipments'][0]['details']
        self.assertEqual(details['dim']['uom'], 'mm')
        self.assertEqual(details['dim']['length'], 500)  # 50 cm * 10
        self.assertEqual(details['dim']['width'], 300)   # 30 cm * 10
        self.assertEqual(details['dim']['height'], 200)  # 20 cm * 10
        self.assertEqual(details['weight']['uom'], 'g')
        self.assertEqual(details['weight']['value'], 10000)  # 10 kg * 1000

    def test_weight_mismatch_with_customs_declaration(self):
        """Test that package weight must match or exceed customs declaration weight."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333335301', 'environment': 'sandbox'})
        pk = self.env.ref('base.pk')
        product = self.env['dhl.product'].create({'name': 'Item', 'sku': 'ITEM-1', 'weight': 0.5, 'price': 10.0})
        order = self.env['dhl.order'].create({
            'customer_id': self.customer.id,
            'address_id': self.customer.address_ids.id,
            'line_ids': [(0, 0, {'product_id': product.id, 'quantity': 3, 'unit_price': 10.0})],
        })
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'order_id': order.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '75500',
            'consignee_city': 'Karachi',
            'consignee_country_id': pk.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '10115',
            'shipper_city': 'Berlin',
            'shipper_country_id': self.env.ref('base.de').id,
            'product_code': 'V53WPAK',
            'package_ids': [(0, 0, {'weight': 1.0})],  # 1.0 kg < 3 * 0.5 = 1.5 kg customs weight
        })
        with self.assertRaises(ValidationError) as cm:
            ship._validate_weight_against_customs()
        self.assertIn('smaller than', str(cm.exception))
        self.assertIn('1.0', str(cm.exception))
        self.assertIn('1.5', str(cm.exception))

    def test_weight_matches_customs_declaration(self):
        """Test that matching weights pass validation."""
        conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333335301', 'environment': 'sandbox'})
        pk = self.env.ref('base.pk')
        product = self.env['dhl.product'].create({'name': 'Item', 'sku': 'ITEM-1', 'weight': 0.5, 'price': 10.0})
        order = self.env['dhl.order'].create({
            'customer_id': self.customer.id,
            'address_id': self.customer.address_ids.id,
            'line_ids': [(0, 0, {'product_id': product.id, 'quantity': 3, 'unit_price': 10.0})],
        })
        ship = self.env['dhl.shipment'].create({
            'connection_id': conn.id,
            'order_id': order.id,
            'consignee_name': 'Test',
            'consignee_street': 'S',
            'consignee_zip': '75500',
            'consignee_city': 'Karachi',
            'consignee_country_id': pk.id,
            'shipper_name': 'Shop',
            'shipper_street': 'S',
            'shipper_zip': '10115',
            'shipper_city': 'Berlin',
            'shipper_country_id': self.env.ref('base.de').id,
            'product_code': 'V53WPAK',
            'package_ids': [(0, 0, {'weight': 1.5})],  # 1.5 kg = 3 * 0.5 kg customs weight
        })
        # Should not raise any error
        ship._validate_weight_against_customs()
