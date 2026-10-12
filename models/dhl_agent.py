import datetime
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

AGENT_STATES = [
    ('stopped', 'Stopped'),
    ('running', 'Running'),
    ('paused', 'Paused'),
    ('error', 'Error'),
]
TASK_STATES = [
    ('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'),
    ('failed', 'Failed'), ('cancelled', 'Cancelled'),
]
CHECK_INTERVAL_MINUTES = 15
SEVERITY_ORDER = {'error': 0, 'warning': 1, 'info': 2}


class DhlAgent(models.Model):
    """Logistics AI Agent - foundation.

    What it really does today: a READ-ONLY health check (every 15 min while
    'running', or on demand) that looks at connections, products, addresses,
    shipments and API errors and reports what needs a human. It never changes
    business data. Task execution (Amazon sync, tracking, retries) arrives with
    the features that those tasks depend on and is NOT simulated here.
    """
    _name = 'dhl.agent'
    _description = 'Logistics AI Agent'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id'

    name = fields.Char(default='Logistics AI Agent', required=True)
    state = fields.Selection(AGENT_STATES, default='stopped', required=True, tracking=True, index=True)
    last_run = fields.Datetime('Last check', readonly=True)
    next_run = fields.Datetime('Next check', readonly=True)
    last_error = fields.Text(readonly=True, copy=False)
    last_summary = fields.Char('Last result', readonly=True, copy=False)

    task_ids = fields.One2many('dhl.agent.task', 'agent_id', 'Tasks')
    log_ids = fields.One2many('dhl.agent.log', 'agent_id', 'Logs')
    total_tasks = fields.Integer(compute='_compute_stats')
    completed_tasks = fields.Integer(compute='_compute_stats')
    failed_tasks = fields.Integer(compute='_compute_stats')
    pending_tasks = fields.Integer(compute='_compute_stats')
    log_count = fields.Integer(compute='_compute_stats')

    @api.depends('task_ids.state', 'log_ids')
    def _compute_stats(self):
        for rec in self:
            tasks = rec.task_ids
            rec.total_tasks = len(tasks)
            rec.completed_tasks = len(tasks.filtered(lambda t: t.state == 'completed'))
            rec.failed_tasks = len(tasks.filtered(lambda t: t.state == 'failed'))
            rec.pending_tasks = len(tasks.filtered(lambda t: t.state == 'pending'))
            rec.log_count = len(rec.log_ids)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _log(self, message, log_type='info'):
        for rec in self:
            self.env['dhl.agent.log'].create({'agent_id': rec.id, 'message': message, 'log_type': log_type})

    def action_start(self):
        self.write({'state': 'running', 'last_error': False})
        self._log(_('Agent started'))

    def action_stop(self):
        self.write({'state': 'stopped', 'next_run': False})
        self._log(_('Agent stopped'))

    def action_pause(self):
        self.write({'state': 'paused', 'next_run': False})
        self._log(_('Agent paused'))

    def action_resume(self):
        self.write({'state': 'running'})
        self._log(_('Agent resumed'))

    def action_view_tasks(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Agent Tasks'), 'res_model': 'dhl.agent.task',
                'view_mode': 'list,form', 'domain': [('agent_id', '=', self.id)]}

    def action_view_logs(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Agent Logs'), 'res_model': 'dhl.agent.log',
                'view_mode': 'list,form', 'domain': [('agent_id', '=', self.id)]}

    # ------------------------------------------------------------------
    # Health check (read-only)
    # ------------------------------------------------------------------
    @api.model
    def get_attention_items(self):
        """Everything that needs a human, computed live from real data."""
        env = self.env
        items = []

        def add(key, severity, text, count=0, model=None, domain=None, res_id=None, name=None):
            items.append({'key': key, 'severity': severity, 'text': text, 'count': count,
                          'target': {'model': model, 'domain': domain or [], 'res_id': res_id,
                                     'name': name or text} if model else False})

        # DHL connection
        conn = env['dhl.connection'].sudo().search([('company_id', '=', env.company.id), ('state', '=', 'connected')], limit=1)
        if not conn:
            conn = env['dhl.connection'].sudo().search([('company_id', '=', env.company.id)], limit=1)
            state = conn.state if conn else 'disconnected'
            texts = {
                'disconnected': _("DHL is not connected. Shipments cannot be created."),
                'auth_error': _("DHL rejected the credentials. Update them and test the connection."),
                'api_error': _("DHL is not responding correctly. Test the connection again."),
            }
            add('dhl_connection', 'error' if state != 'api_error' else 'warning', texts.get(state, texts['disconnected']),
                model='dhl.connection', res_id=conn.id or None, name=_('DHL Connection'))

        # Open orders: missing product weight / address
        Order = env['dhl.order']
        open_orders = Order.search([('state', 'in', ('draft', 'pending', 'processing'))])
        no_weight = open_orders.line_ids.product_id.filtered(lambda p: p.weight <= 0)
        if no_weight:
            add('product_weight', 'warning',
                _("%s products on open orders have no package weight.", len(no_weight)),
                len(no_weight), 'dhl.product', [('id', 'in', no_weight.ids)], name=_('Products without weight'))
        ship_orders = open_orders.filtered(lambda o: o.state in ('pending', 'processing'))
        no_addr = ship_orders.filtered(lambda o: not o.address_id)
        if no_addr:
            add('order_address', 'warning', _("%s orders have no shipping address.", len(no_addr)),
                len(no_addr), 'dhl.order', [('id', 'in', no_addr.ids)], name=_('Orders without address'))
        bad_addr = ship_orders.address_id.filtered(lambda a: a.validation_state == 'invalid')
        if bad_addr:
            add('address_invalid', 'error', _("%s shipping addresses are invalid.", len(bad_addr)),
                len(bad_addr), 'dhl.customer.address', [('id', 'in', bad_addr.ids)], name=_('Invalid addresses'))

        # Shipments
        Shipment = env['dhl.shipment']
        blocked = Shipment.search([('state', '=', 'draft')], limit=200).filtered(lambda s: s._missing_data())
        if blocked:
            add('shipment_blocked', 'warning', _("%s draft shipments are blocked by missing data.", len(blocked)),
                len(blocked), 'dhl.shipment', [('id', 'in', blocked.ids)], name=_('Blocked shipments'))
        failed = Shipment.search_count([('state', 'in', ('creation_failed', 'delivery_failed'))])
        if failed:
            add('shipment_failed', 'error', _("%s shipments failed.", failed), failed, 'dhl.shipment',
                [('state', 'in', ('creation_failed', 'delivery_failed'))], name=_('Failed shipments'))

        # Recent API errors
        since = fields.Datetime.now() - datetime.timedelta(days=1)
        api_errors = env['dhl.api.log'].sudo().search_count([
            ('company_id', '=', env.company.id), ('success', '=', False), ('create_date', '>=', since)])
        if api_errors:
            can_view_logs = env.user.has_group('dhl_logistics_center.group_dhl_manager')
            add('api_errors', 'warning', _("%s API calls failed in the last 24 hours.", api_errors), api_errors,
                'dhl.api.log' if can_view_logs else None,
                [('success', '=', False), ('create_date', '>=', since)] if can_view_logs else [],
                name=_('Failed API calls'))

        items.sort(key=lambda i: SEVERITY_ORDER.get(i['severity'], 9))
        return items

    @api.model
    def get_subsystems(self, agent=None):
        """Honest status of each automation area. 'planned' means NOT built yet."""
        running = bool(agent and agent.state == 'running')
        return [
            {'key': 'health', 'label': _('Health checks'), 'status': 'active' if running else 'off',
             'note': _('Every %s min', CHECK_INTERVAL_MINUTES)},
            {'key': 'tracking', 'label': _('DHL tracking sync'), 'status': 'off', 'note': _('Requires an active DHL connection and scheduled job')},
            {'key': 'queue', 'label': _('Shipment queue'), 'status': 'planned', 'note': _('Automatic booking queue is not enabled')},
            {'key': 'retry', 'label': _('Retry queue'), 'status': 'off', 'note': _('Safe retry scheduler status is checked separately')},
            {'key': 'amazon', 'label': _('Amazon sync'), 'status': 'off', 'note': _('Connect and authorize an Amazon seller account')},
        ]

    def action_run_check(self):
        """Run the read-only health check now and record the result."""
        for rec in self:
            try:
                items = self.get_attention_items()
            except Exception as exc:  # noqa: BLE001 - agent must never crash its caller
                _logger.exception("Agent health check failed")
                rec.write({'state': 'error', 'last_error': str(exc)})
                rec._log(_("Health check failed: %s", exc), 'error')
                continue
            summary = (_("%s items need attention", len(items)) if items else _("Everything looks fine"))
            now = fields.Datetime.now()
            rec.write({'last_run': now, 'last_summary': summary, 'last_error': False,
                       'next_run': now + datetime.timedelta(minutes=CHECK_INTERVAL_MINUTES)
                       if rec.state == 'running' else False})
            rec._log(summary, 'warning' if items else 'success')
            self.env['dhl.automation.log'].sudo().create({
                'name': _('Agent health check'), 'state': 'success', 'finished_at': now, 'message': summary})
        return True

    def action_run_check_button(self):
        self.action_run_check()
        self.env['dhl.notification'].notify('info', _('Health check finished'), self[:1].last_summary or '')
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    @api.model
    def _cron_run(self):
        self.sudo().search([('state', '=', 'running')]).action_run_check()


class DhlAgentTask(models.Model):
    _name = 'dhl.agent.task'
    _description = 'Agent Task'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    agent_id = fields.Many2one('dhl.agent', required=True, ondelete='cascade', index=True)
    name = fields.Char(required=True)
    task_type = fields.Selection([
        ('amazon_sync', 'Amazon Sync'), ('dhl_tracking', 'DHL Tracking'),
        ('shipment_creation', 'Shipment Creation'), ('retry_operation', 'Retry Operation'),
        ('validation', 'Validation'), ('notification', 'Notification'), ('other', 'Other'),
    ], required=True, default='other')
    state = fields.Selection(TASK_STATES, default='pending', required=True, tracking=True, index=True)
    model_name = fields.Char('Related Model')
    res_id = fields.Integer('Related Record ID')
    record_name = fields.Char('Record Name')
    started_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)
    duration_seconds = fields.Float('Duration (s)', readonly=True)
    error_message = fields.Text(readonly=True, copy=False)
    priority = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('urgent', 'Urgent')],
                                default='medium', required=True)
    user_id = fields.Many2one('res.users', 'Created By', default=lambda self: self.env.user, readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    def action_start(self):
        self.write({'state': 'running', 'started_at': fields.Datetime.now()})

    def _finish(self, state, error=False):
        now = fields.Datetime.now()
        for rec in self:
            started = rec.started_at or now
            rec.write({'state': state, 'completed_at': now, 'started_at': started,
                       'duration_seconds': (now - started).total_seconds(), 'error_message': error})

    def action_complete(self):
        self._finish('completed')

    def action_fail(self, error_message=False):
        self._finish('failed', error_message)

    def action_cancel(self):
        self.write({'state': 'cancelled'})

    def action_retry(self):
        self.write({'state': 'pending', 'error_message': False, 'started_at': False, 'completed_at': False})


class DhlAgentLog(models.Model):
    _name = 'dhl.agent.log'
    _description = 'Agent Log'
    _order = 'id desc'

    agent_id = fields.Many2one('dhl.agent', required=True, ondelete='cascade', index=True)
    message = fields.Text(required=True)
    log_type = fields.Selection([('info', 'Info'), ('success', 'Success'), ('warning', 'Warning'),
                                 ('error', 'Error')], required=True, default='info', index=True)
    task_id = fields.Many2one('dhl.agent.task', 'Related Task', ondelete='set null')
    model_name = fields.Char('Related Model')
    res_id = fields.Integer('Related Record ID')
    user_id = fields.Many2one('res.users', 'User', default=lambda self: self.env.user, readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
