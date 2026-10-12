from odoo import SUPERUSER_ID, _, api, fields, models

GROUP_MANAGER = 'dhl_logistics_center.group_dhl_manager'


class DhlNotification(models.Model):
    """Persistent notification center entry. Creating one also pushes a toast
    to the recipient's browser through the Odoo bus."""
    _name = 'dhl.notification'
    _description = 'DHL Notification'
    _order = 'id desc'

    name = fields.Char('Title', required=True)
    message = fields.Text()
    notification_type = fields.Selection(
        [('success', 'Success'), ('error', 'Error'), ('warning', 'Warning'), ('info', 'Info')],
        default='info', required=True, index=True)
    user_id = fields.Many2one('res.users', 'Recipient', required=True, index=True,
                              ondelete='cascade', default=lambda self: self.env.uid)
    is_read = fields.Boolean(index=True)
    read_date = fields.Datetime(readonly=True)
    res_model = fields.Char('Related Model')
    res_id = fields.Many2oneReference('Related Record', model_field='res_model')
    record_name = fields.Char('Related To')

    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._send_bus()
        return records

    def _to_payload(self):
        self.ensure_one()
        return {
            'id': self.id,
            'title': self.name,
            'message': self.message or '',
            'type': self.notification_type,
            'res_model': self.res_model or False,
            'res_id': self.res_id or False,
            'record_name': self.record_name or '',
            'is_read': self.is_read,
            'create_date': fields.Datetime.to_string(self.create_date),
        }

    def _send_bus(self):
        bus = self.env['bus.bus']
        for rec in self:
            bus._sendone(rec.user_id.partner_id, 'dhl.notification', rec._to_payload())

    @api.model
    def _default_recipients(self):
        user = self.env.user
        if user.id != SUPERUSER_ID:
            return user
        group = self.env.ref(GROUP_MANAGER, raise_if_not_found=False)
        if not group:
            return user
        group = group.sudo()
        return group['user_ids'] if 'user_ids' in group._fields else group['users']

    @api.model
    def notify(self, ntype, title, message='', users=None, record=None, independent=True):
        """Create a notification (+ toast).

        independent=True writes through a separate cursor so the entry survives
        a rollback of the calling transaction -- required for error flows.
        """
        users = users or self._default_recipients()
        vals_list = [{
            'name': title,
            'message': message,
            'notification_type': ntype,
            'user_id': user.id,
            'res_model': record._name if record else False,
            'res_id': record.id if record else False,
            'record_name': record.display_name if record else False,
        } for user in users]
        if not vals_list:
            return self.browse()
        if independent:
            with self.pool.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env['dhl.notification'].create(vals_list)
            return self.browse()
        return self.sudo().create(vals_list)

    # ------------------------------------------------------------------
    # RPC used by the systray bell
    # ------------------------------------------------------------------
    @api.model
    def get_panel_data(self, limit=25):
        domain = [('user_id', '=', self.env.uid)]
        sudo = self.sudo()
        return {
            'unread': sudo.search_count(domain + [('is_read', '=', False)]),
            'items': [n._to_payload() for n in sudo.search(domain, limit=limit)],
        }

    @api.model
    def mark_read(self, ids):
        recs = self.sudo().search([('id', 'in', ids), ('user_id', '=', self.env.uid), ('is_read', '=', False)])
        recs.write({'is_read': True, 'read_date': fields.Datetime.now()})
        return True

    @api.model
    def mark_all_read(self):
        recs = self.sudo().search([('user_id', '=', self.env.uid), ('is_read', '=', False)])
        recs.write({'is_read': True, 'read_date': fields.Datetime.now()})
        return True

    # ------------------------------------------------------------------
    def action_mark_read(self):
        self.filtered(lambda n: not n.is_read).write({'is_read': True, 'read_date': fields.Datetime.now()})

    def action_open_record(self):
        self.ensure_one()
        self.action_mark_read()
        if not self.res_model or not self.res_id:
            return False
        return {'type': 'ir.actions.act_window', 'res_model': self.res_model,
                'res_id': self.res_id, 'views': [(False, 'form')], 'target': 'current'}
