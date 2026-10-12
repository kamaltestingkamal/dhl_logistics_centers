from odoo import fields, models

P = 'dhl_logistics_center.'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    dhl_shipper_name = fields.Char('Shipper Name', config_parameter=P + 'shipper_name')
    dhl_shipper_company = fields.Char('Company', config_parameter=P + 'shipper_company')
    dhl_shipper_street = fields.Char('Street', config_parameter=P + 'shipper_street')
    dhl_shipper_street_number = fields.Char('House No.', config_parameter=P + 'shipper_street_number')
    dhl_shipper_zip = fields.Char('Postal Code', config_parameter=P + 'shipper_zip')
    dhl_shipper_city = fields.Char('City', config_parameter=P + 'shipper_city')
    dhl_shipper_country_code = fields.Char('Country Code', config_parameter=P + 'shipper_country_code',
                                           help='ISO 2-letter code, e.g. DE')
    dhl_shipper_email = fields.Char('Email', config_parameter=P + 'shipper_email')
    dhl_shipper_phone = fields.Char('Phone', config_parameter=P + 'shipper_phone')
    dhl_default_weight = fields.Float('Default Package Weight (kg)', config_parameter=P + 'default_weight',
                                      default=1.0)
