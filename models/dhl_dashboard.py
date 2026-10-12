import datetime

from odoo import _, api, fields, models

ORDER_STATES = ['draft', 'pending', 'processing', 'shipped', 'delivered', 'cancelled', 'failed']
SHIPMENT_STATES = ['draft', 'ready', 'validating', 'created', 'in_transit', 'delivered',
                   'creation_failed', 'delivery_failed', 'cancelled']

DHL_STATUS_LABELS = {
    'connected': 'DHL Connected',
    'disconnected': 'DHL Disconnected',
    'auth_error': 'DHL Authentication problem',
    'api_error': 'DHL API problem',
}


class DhlDashboard(models.AbstractModel):
    _name = 'dhl.dashboard'
    _description = 'DHL Dashboard Data Provider'

    @api.model
    def get_connection_status(self):
        conns = self.env['dhl.connection'].sudo().search([('company_id', '=', self.env.company.id)])
        active = conns.filtered(lambda c: c.state == 'connected')[:1]
        shown = active or conns[:1]
        state = shown.state if shown else 'disconnected'
        return {
            'dhl': {
                'connected': state == 'connected',
                'state': state,
                'label': DHL_STATUS_LABELS.get(state, DHL_STATUS_LABELS['disconnected']),
                'has_connection': bool(conns),
                'id': shown.id or False,
                'name': shown.name or '',
                'environment': shown.environment or '',
            },
            # The Amazon connection status is populated by the Amazon integration model extension.
            'amazon': {'connected': False, 'available': False},
        }

    @api.model
    def _dashboard_api_log_rows(self, extra_domain, limit, include_message=False):
        """Expose only redacted DHL log summaries for the current company on the dashboard."""
        domain = [('company_id', '=', self.env.company.id)] + list(extra_domain)
        records = self.env['dhl.api.log'].sudo().search(domain, order='id desc', limit=limit)
        result = []
        for record in records:
            item = {'id': record.id, 'name': record.name,
                    'time': fields.Datetime.to_string(record.create_date)}
            if include_message:
                item['message'] = record.error_message or ''
            else:
                item['success'] = record.success
            result.append(item)
        return result

    @api.model
    def get_data(self):
        Order, Shipment = self.env['dhl.order'], self.env['dhl.shipment']
        orders = dict(Order._read_group([], ['state'], ['__count']))
        ships = dict(Shipment._read_group([], ['state'], ['__count']))
        cost = Shipment._read_group([('state', '!=', 'cancelled')], [], ['shipping_cost:sum'])[0][0] or 0.0

        today = fields.Date.today()
        days = [today - datetime.timedelta(days=i) for i in range(13, -1, -1)]
        since = datetime.datetime.combine(days[0], datetime.time.min)
        per_day = {}
        for day, count in Shipment._read_group([('create_date', '>=', since)], ['create_date:day'], ['__count']):
            per_day[day.date() if isinstance(day, datetime.datetime) else day] = count
        start_today = datetime.datetime.combine(today, datetime.time.min)

        def rows(model, domain, limit, fn):
            return [fn(r) for r in self.env[model].search(domain, limit=limit)]

        agent = self.env['dhl.agent'].sudo().search([], limit=1)
        Agent = self.env['dhl.agent']
        attention = Agent.get_attention_items()
        currency = self.env.company.currency_id
        return {
            'currency': {'symbol': currency.symbol, 'position': currency.position},
            'connection': self.get_connection_status(),
            'agent': {
                'exists': bool(agent), 'id': agent.id or False, 'name': agent.name or '',
                'state': agent.state if agent else 'stopped',
                'last_run': fields.Datetime.to_string(agent.last_run) if agent.last_run else False,
                'next_run': fields.Datetime.to_string(agent.next_run) if agent.next_run else False,
                'last_summary': agent.last_summary or '',
                'subsystems': Agent.get_subsystems(agent),
            },
            'attention': attention,
            'today': {
                'orders': Order.search_count([('create_date', '>=', start_today)]),
                'shipments': Shipment.search_count([('create_date', '>=', start_today)]),
                'tracking': self.env['dhl.tracking'].search_count([('create_date', '>=', start_today)]),
                'errors': self.env['dhl.api.log'].sudo().search_count(
                    [('company_id', '=', self.env.company.id), ('success', '=', False),
                     ('create_date', '>=', start_today)]),
            },
            'orders_by_state': {s: orders.get(s, 0) for s in ORDER_STATES},
            'shipments_by_state': {s: ships.get(s, 0) for s in SHIPMENT_STATES},
            'total_orders': sum(orders.values()),
            'total_shipments': sum(ships.values()),
            'returns': self.env['dhl.return'].search_count([('state', 'not in', ('completed', 'cancelled'))]),
            'shipping_cost': cost,
            'shipments_per_day': [{'day': d.strftime('%d %b'), 'count': per_day.get(d, 0)} for d in days],
            'recent_orders': rows('dhl.order', [], 6, lambda o: {
                'id': o.id, 'name': o.name, 'customer': o.customer_id.name, 'state': o.state,
                'amount': o.amount_total}),
            'recent_shipments': rows('dhl.shipment', [], 6, lambda s: {
                'id': s.id, 'name': s.name, 'tracking': s.tracking_number or '', 'state': s.state,
                'consignee': s.consignee_name or ''}),
            'recent_errors': self._dashboard_api_log_rows([('success', '=', False)], 5, include_message=True),
            'recent_activity': self._dashboard_api_log_rows([], 6, include_message=False),
            'tracking_updates': rows('dhl.tracking', [], 6, lambda t: {
                'id': t.id, 'shipment': t.shipment_id.name, 'text': t.status_text or '',
                'status': t.normalized_status, 'time': fields.Datetime.to_string(t.event_time)}),
        }
