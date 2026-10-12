from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.amazon_logistics_automation import AMAZON_ENDPOINTS, _parse_amazon_datetime, _split_street


@tagged('post_install', '-at_install')
class TestAmazonAutomationHelpers(TransactionCase):
    def test_split_street_trailing_house_number(self):
        self.assertEqual(_split_street('Leipziger Platz 12'), ('Leipziger Platz', '12'))

    def test_split_street_leading_house_number(self):
        self.assertEqual(_split_street('45 Oak Avenue'), ('Oak Avenue', '45'))

    def test_split_street_does_not_invent_number(self):
        self.assertEqual(_split_street('Main Street'), ('Main Street', False))

    def test_parse_amazon_datetime_utc(self):
        value = _parse_amazon_datetime('2026-10-09T10:00:00Z')
        self.assertEqual(value.isoformat(), '2026-10-09T10:00:00')


    def test_product_sync_uses_one_marketplace_per_request(self):
        connection = self.env['dhl.amazon.connection'].create({
            'name': 'Marketplace loop test',
            'environment': 'sandbox',
            'seller_id': 'seller-test',
            'marketplace_ids': 'MARKET-A,MARKET-B',
            'lwa_client_id': 'client-test',
            'lwa_client_secret': 'secret-test',
            'refresh_token': 'refresh-test',
        })
        responses = [{'items': [], 'pagination': {}}, {'items': [], 'pagination': {}}]
        patch_path = 'odoo.addons.dhl_logistics_center.models.amazon_logistics_automation.DhlAmazonConnection._amazon_get'
        with patch(patch_path, autospec=True, side_effect=responses) as mock_get:
            result = connection.sync_products_now(max_pages=10)
        self.assertEqual(result['total'], 0)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(AMAZON_ENDPOINTS['sandbox']['eu'], 'https://sandbox.sellingpartnerapi-eu.amazon.com')
        self.assertEqual(AMAZON_ENDPOINTS['production']['eu'], 'https://sellingpartnerapi-eu.amazon.com')
        self.assertEqual(mock_get.call_args_list[0].kwargs['params']['marketplaceIds'], 'MARKET-A')
        self.assertEqual(mock_get.call_args_list[1].kwargs['params']['marketplaceIds'], 'MARKET-B')
        self.assertEqual(connection.product_sync_marketplace_index, 0)
        self.assertFalse(connection.product_sync_marketplace_id)

    def test_parse_invalid_amazon_datetime(self):
        self.assertFalse(_parse_amazon_datetime('not-a-date'))
