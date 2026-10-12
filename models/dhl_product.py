from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class DhlProductCategory(models.Model):
    _name = 'dhl.product.category'
    _description = 'Product Category'
    _order = 'name'

    name = fields.Char(required=True)
    parent_id = fields.Many2one('dhl.product.category', ondelete='restrict')
    product_count = fields.Integer(compute='_compute_product_count')

    def _compute_product_count(self):
        data = dict(self.env['dhl.product']._read_group([('category_id', 'in', self.ids)], ['category_id'], ['__count']))
        for rec in self:
            rec.product_count = data.get(rec, 0)


class DhlProduct(models.Model):
    _name = 'dhl.product'
    _description = 'Product'
    _order = 'name'

    name = fields.Char(required=True, index=True)
    sku = fields.Char('SKU', required=True, index=True, copy=False)
    asin = fields.Char('Amazon ASIN', index=True, copy=False)
    marketplace = fields.Char(index=True, copy=False)
    external_product_id = fields.Char(index=True, copy=False)
    source = fields.Selection([('manual', 'Manual'), ('amazon', 'Amazon')], default='manual', required=True, index=True)
    sync_state = fields.Selection([('not_synced', 'Not Synced'), ('synced', 'Synced'), ('error', 'Sync Error')],
                                  default='not_synced', copy=False, index=True)
    last_sync_at = fields.Datetime(copy=False, readonly=True)
    description = fields.Text()
    category_id = fields.Many2one('dhl.product.category')
    active = fields.Boolean(default=True)
    image_1920 = fields.Image('Image', max_width=1920, max_height=1920)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    price = fields.Monetary(currency_field='currency_id')

    weight = fields.Float('Weight (kg)', digits=(10, 3))
    length = fields.Float('Length (cm)')
    width = fields.Float('Width (cm)')
    height = fields.Float('Height (cm)')

    hs_code = fields.Char('HS Code', help='Harmonized System code, needed for customs on international shipments.')
    country_of_origin_id = fields.Many2one('res.country', 'Country of Origin')

    @api.constrains('sku', 'company_id')
    def _check_sku_unique(self):
        for rec in self:
            dup = self.with_context(active_test=False).search_count([
                ('sku', '=', rec.sku), ('company_id', '=', rec.company_id.id), ('id', '!=', rec.id)])
            if dup:
                raise ValidationError(_("SKU '%s' already exists.", rec.sku))

    @api.constrains('asin', 'marketplace', 'company_id')
    def _check_asin_marketplace_unique(self):
        for rec in self.filtered('asin'):
            domain = [('asin', '=', rec.asin), ('company_id', '=', rec.company_id.id), ('id', '!=', rec.id)]
            if rec.marketplace:
                domain.append(('marketplace', '=', rec.marketplace))
            else:
                domain.append(('marketplace', '=', False))
            if self.with_context(active_test=False).search_count(domain):
                raise ValidationError(_("Amazon ASIN %(asin)s already exists for this marketplace.", asin=rec.asin))

    @api.model
    def upsert_amazon_product(self, data, marketplace):
        """Idempotent import boundary for a verified Amazon API adapter.

        `data` must contain only fields actually returned/authorized by the API.
        This method performs no network request and does not invent missing values.
        """
        if not isinstance(data, dict):
            raise ValidationError(_("Amazon product data must be an object."))
        sku = (data.get('sku') or '').strip()
        asin = (data.get('asin') or '').strip()
        if not sku and not asin:
            raise ValidationError(_("Amazon product needs a SKU or ASIN to match safely."))
        Product = self.with_context(active_test=False)
        company_domain = [('company_id', '=', self.env.company.id)]
        product = Product.browse()
        if sku:
            by_sku = Product.search(company_domain + [('sku', '=', sku)], limit=2)
            if len(by_sku) > 1:
                raise ValidationError(_("Amazon SKU match is ambiguous; review duplicate SKU records."))
            product = by_sku
        if asin:
            by_asin = Product.search(company_domain + [('asin', '=', asin),
                                                        ('marketplace', '=', marketplace or False)], limit=2)
            if len(by_asin) > 1:
                raise ValidationError(_("Amazon ASIN match is ambiguous; review duplicate ASIN records."))
            if product and by_asin and product != by_asin:
                raise ValidationError(_("Amazon SKU and ASIN point to different products; manual review is required."))
            if not product:
                product = by_asin
        if product and product.marketplace and marketplace and product.marketplace != marketplace:
            if asin and product.asin and product.asin != asin:
                raise ValidationError(_(
                    "SKU %(sku)s is already mapped to ASIN %(existing)s in marketplace %(market)s; "
                    "Amazon returned ASIN %(incoming)s for another marketplace. A marketplace listing mapping "
                    "is required before merging these records.", sku=sku, existing=product.asin,
                    market=product.marketplace, incoming=asin))
        vals = {'source': 'amazon', 'last_sync_at': fields.Datetime.now(), 'sync_state': 'synced'}
        # The current model has one marketplace field per product. Keep the first mapping rather than
        # silently flipping a shared SKU between marketplaces on each synchronization.
        if marketplace and (not product or not product.marketplace):
            vals['marketplace'] = marketplace
        allowed = {'name', 'sku', 'asin', 'external_product_id', 'description', 'image_1920', 'price', 'weight',
                   'length', 'width', 'height', 'hs_code'}
        vals.update({key: value for key, value in data.items() if key in allowed and value not in (None, '')})
        if not product:
            if not vals.get('name') or not vals.get('sku'):
                raise ValidationError(_("A new product requires a name and SKU."))
            vals['company_id'] = self.env.company.id
            product = self.create(vals)
            return product, 'created'
        product.write(vals)
        return product, 'updated'

    @api.constrains('weight', 'length', 'width', 'height', 'price')
    def _check_non_negative(self):
        for rec in self:
            if min(rec.weight, rec.length, rec.width, rec.height, rec.price) < 0:
                raise ValidationError(_("Weight, dimensions and price cannot be negative."))
