# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Migrate old 'failed' state to 'creation_failed' for consistency.

    The module now uses 'creation_failed' and 'delivery_failed' instead of
    a single 'failed' state. This migration updates existing records.
    """
    cr.execute("UPDATE dhl_shipment SET state='creation_failed' WHERE state='failed'")
