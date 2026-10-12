from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

POST = 'odoo.addons.dhl_logistics_center.models.dhl_connection.requests.post'


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
class TestConnection(TransactionCase):

    def setUp(self):
        super().setUp()
        self.conn = self.env['dhl.connection'].create({
            'name': 'Test', 'api_key': 'k', 'api_secret': 's', 'username': 'u',
            'password': 'p', 'billing_number': '33333333330101'})

    def _logs(self):
        return self.env['dhl.api.log'].search([('connection_id', '=', self.conn.id)])

    def test_success_connects_and_notifies(self):
        with patch(POST, return_value=FakeResponse(200, {'access_token': 'TOK', 'expires_in': 1800})):
            self.conn.action_test_connection()
        self.assertEqual(self.conn.state, 'connected')
        note = self.env['dhl.notification'].search([('notification_type', '=', 'success')], limit=1)
        self.assertEqual(note.name, 'DHL Connected Successfully')
        self.assertTrue(self._logs().filtered('success'))
        self.assertNotIn('TOK', ' '.join(self._logs().mapped('response_body')), 'token must be masked in logs')

    def test_token_is_cached(self):
        with patch(POST, return_value=FakeResponse(200, {'access_token': 'TOK', 'expires_in': 1800})) as post:
            self.conn.get_access_token()
            self.conn.get_access_token()
        self.assertEqual(post.call_count, 1)

    def test_invalid_credentials(self):
        with patch(POST, return_value=FakeResponse(401, {'error_description': 'Bad credentials'})):
            self.conn.action_test_connection()
        self.assertEqual(self.conn.state, 'auth_error')
        self.assertEqual(self.conn.error_category, 'authentication')
        self.assertFalse(self.conn.error_retryable)
        self.assertIn('Bad credentials', self.conn.last_error)
        note = self.env['dhl.notification'].search([('notification_type', '=', 'error')], limit=1)
        self.assertEqual(note.name, 'DHL Connection Failed')
        self.assertEqual(note.message, 'Please check credentials or API configuration.')
        self.assertTrue(self._logs().filtered(lambda l: not l.success))

    def test_non_json_response_is_handled(self):
        # Regression: raw ValueError used to escape instead of a DHL error.
        with patch(POST, return_value=FakeResponse(502, None, '<html>Bad Gateway</html>')):
            self.conn.action_test_connection()
        self.assertEqual(self.conn.state, 'api_error')
        self.assertTrue(self.conn.error_retryable)
        self.assertIn('non-JSON', self.conn.last_error)

    def test_timeout_and_connection_error(self):
        for exc in (requests.Timeout(), requests.ConnectionError()):
            with patch(POST, side_effect=exc):
                self.conn.action_test_connection()
            self.assertEqual(self.conn.state, 'api_error')
            self.assertTrue(self.conn.error_retryable)

    def test_missing_credentials(self):
        conn = self.env['dhl.connection'].create({'name': 'Empty'})
        with self.assertRaises(UserError):
            conn.action_test_connection()

    def test_editing_credentials_disconnects(self):
        with patch(POST, return_value=FakeResponse(200, {'access_token': 'T', 'expires_in': 1800})):
            self.conn.action_test_connection()
        self.conn.write({'username': 'other'})
        self.assertEqual(self.conn.state, 'disconnected')
        self.assertFalse(self.conn.sudo().access_token)
