from odoo import _, api, fields, models


class DhlShipmentHistory(models.Model):
    _name = 'dhl.shipment.history'
    _description = 'Shipment History'
    _order = 'id desc'

    shipment_id = fields.Many2one('dhl.shipment', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='shipment_id.company_id', store=True, index=True)
    
    event_type = fields.Selection([
        ('created', 'Created'),
        ('ready', 'Marked Ready'),
        ('validating', 'Validating'),
        ('created_at_dhl', 'Created at DHL'),
        ('label_generated', 'Label Generated'),
        ('label_downloaded', 'Label Downloaded'),
        ('tracking_updated', 'Tracking Updated'),
        ('in_transit', 'In Transit'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('delivery_failed', 'Delivery Failed'),
        ('cancelled', 'Cancelled'),
        ('creation_failed', 'Creation Failed'),
        ('error', 'Error'),
        ('retry', 'Retry Attempt'),
        ('state_change', 'State Change'),
    ], required=True, index=True)
    
    old_state = fields.Char('Old State')
    new_state = fields.Char('New State')
    
    message = fields.Text('Message')
    details = fields.Text('Details')
    
    user_id = fields.Many2one('res.users', 'User', default=lambda self: self.env.user, readonly=True)
    automatic = fields.Boolean('Automatic', default=False, help='True if this event was triggered by automation/cron')
    
    related_tracking_id = fields.Many2one('dhl.tracking', 'Related Tracking Event')
    tracking_status_text = fields.Char('Tracking Status')
    
    @api.model
    def log_event(self, shipment, event_type, message='', details='', old_state=None, new_state=None, 
                  automatic=False, tracking_event=None):
        """Create a history entry for a shipment."""
        vals = {
            'shipment_id': shipment.id,
            'event_type': event_type,
            'message': message,
            'details': details,
            'old_state': old_state,
            'new_state': new_state,
            'automatic': automatic,
        }
        if tracking_event:
            vals['related_tracking_id'] = tracking_event.id
            vals['tracking_status_text'] = tracking_event.status_text
        return self.create(vals)
