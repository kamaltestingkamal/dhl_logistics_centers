from odoo.tests import TransactionCase, tagged

from ..models.dhl_utils import body_to_text, classify_http_status, normalize_tracking_status


@tagged('post_install', '-at_install')
class TestUtils(TransactionCase):

    def test_not_delivered_is_not_delivered(self):
        # Regression: "nicht zugestellt" used to be reported as Delivered.
        for text in ("Die Sendung wurde nicht zugestellt", "Shipment not delivered",
                     "Zustellung nicht möglich", "Delivery attempt failed"):
            self.assertEqual(normalize_tracking_status(text), 'delivery_failed', text)

    def test_delivered(self):
        self.assertEqual(normalize_tracking_status("Die Sendung wurde erfolgreich zugestellt."), 'delivered')
        self.assertEqual(normalize_tracking_status("", 'delivered'), 'delivered')

    def test_future_tense_is_not_delivered(self):
        self.assertEqual(normalize_tracking_status("Die Sendung wird in Kürze zugestellt"), 'out_for_delivery')
        self.assertEqual(normalize_tracking_status("Out for delivery"), 'out_for_delivery')

    def test_other_statuses(self):
        self.assertEqual(normalize_tracking_status("Die Sendung wurde an den Absender zurückgesendet. Retoure"), 'returned')
        self.assertEqual(normalize_tracking_status("Im Paketzentrum bearbeitet"), 'in_transit')
        self.assertEqual(normalize_tracking_status("", None), 'unknown')

    def test_secrets_are_masked(self):
        text = body_to_text({'client_secret': 'S3CR3T', 'username': 'bob', 'nested': {'password': 'pw'}})
        self.assertNotIn('S3CR3T', text)
        self.assertNotIn('"pw"', text)
        self.assertIn('bob', text)
        raw = body_to_text('access_token=abc123&foo=bar')
        self.assertNotIn('abc123', raw)


    def test_pii_is_redacted_from_structured_payloads(self):
        text = body_to_text({'email': 'customer@example.com', 'phone': '+49 123456789',
                             'address': {'street': 'Main Street', 'city': 'Berlin'},
                             'status': 'in transit'})
        self.assertNotIn('customer@example.com', text)
        self.assertNotIn('Main Street', text)
        self.assertIn('in transit', text)

    def test_http_400_is_validation_not_authentication(self):
        self.assertEqual(classify_http_status(400), ('validation', False))
        self.assertEqual(classify_http_status(401), ('authentication', False))
        self.assertEqual(classify_http_status(503), ('api', True))
