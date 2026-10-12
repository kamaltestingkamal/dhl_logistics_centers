from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAgent(TransactionCase):

    def setUp(self):
        super().setUp()
        self.agent = self.env['dhl.agent'].search([], limit=1) or self.env['dhl.agent'].create({})

    def test_health_check_reports_disconnected_dhl(self):
        self.env['dhl.connection'].search([]).unlink()
        keys = [i['key'] for i in self.env['dhl.agent'].get_attention_items()]
        self.assertIn('dhl_connection', keys)

    def test_health_check_flags_product_without_weight(self):
        de = self.env.ref('base.de')
        product = self.env['dhl.product'].create({'name': 'NoWeight', 'sku': 'NW-1'})
        customer = self.env['dhl.customer'].create({
            'name': 'C', 'address_ids': [(0, 0, {'street': 'S', 'zip': '1', 'city': 'X', 'country_id': de.id})]})
        self.env['dhl.order'].create({
            'customer_id': customer.id, 'line_ids': [(0, 0, {'product_id': product.id, 'unit_price': 1})]})
        items = {i['key']: i for i in self.env['dhl.agent'].get_attention_items()}
        self.assertIn('product_weight', items)
        self.assertIn(product.id, items['product_weight']['target']['domain'][0][2])

    def test_run_check_logs_and_is_read_only(self):
        before = self.env['dhl.order'].search_count([])
        self.agent.action_run_check()
        self.assertTrue(self.agent.last_run)
        self.assertTrue(self.agent.log_ids)
        self.assertEqual(self.env['dhl.order'].search_count([]), before)

    def test_subsystems_never_claim_unbuilt_features(self):
        subs = {s['key']: s for s in self.env['dhl.agent'].get_subsystems(self.agent)}
        # queue is 'planned' (not built), others are 'off' (not active/configured)
        self.assertEqual(subs['queue']['status'], 'planned')
        for key in ('tracking', 'retry', 'amazon'):
            self.assertEqual(subs[key]['status'], 'off')

    def test_dashboard_payload(self):
        data = self.env['dhl.dashboard'].get_data()
        self.assertIn(data['connection']['dhl']['state'],
                      ('connected', 'disconnected', 'auth_error', 'api_error'))
        self.assertFalse(data['connection']['amazon']['connected'])
