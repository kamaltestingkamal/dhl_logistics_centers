"""Pure-python helpers (no ORM) so they can be unit tested in isolation."""
import json
import re


# Error categories (smart error management). `retryable` tells the retry engine
# whether trying again can help. Authentication / validation problems
# are NOT retryable: they need a human.
ERROR_CATEGORIES = [
    ('authentication', 'Authentication'),
    ('validation', 'Validation'),
    ('network', 'Network'),
    ('timeout', 'Timeout'),
    ('api', 'API'),
    ('invalid_response', 'Invalid response'),
    ('validation', 'Validation'),
]


class DHLAPIError(Exception):
    """Any failure talking to DHL (network, HTTP error, unreadable response)."""

    def __init__(self, message, status_code=None, payload=None, category='api', retryable=False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload
        self.category = category
        self.retryable = retryable

    def __str__(self):
        return str(self.message)


def classify_http_status(status):
    """Return (category, retryable) for a failed HTTP status."""
    if status == 400:
        return 'validation', False
    if status in (401, 403):
        return 'authentication', False
    if status == 429 or (status and status >= 500):
        return 'api', True
    return 'api', False


SENSITIVE_KEYS = {
    'password', 'client_secret', 'api_secret', 'access_token',
    'refresh_token', 'authorization', 'secret', 'token', 'dhl-api-key',
    'email', 'phone', 'telephone', 'mobile', 'street', 'streetname',
    'street_number', 'housenumber', 'postalcode', 'postcode', 'zip',
    'address', 'addressline', 'address_line2', 'name1', 'customername',
    'recipient', 'consignee', 'shipper', 'buyerinfo', 'buyer_info',
    'buyer_email', 'buyer_phone', 'contact_name', 'name2', 'first_name', 'last_name',
}


def _is_sensitive_key(key):
    key = re.sub(r'[^a-z0-9]', '', str(key).lower())
    return (key in {re.sub(r'[^a-z0-9]', '', item) for item in SENSITIVE_KEYS}
            or any(part in key for part in ('email', 'phone', 'telephone', 'mobile', 'password',
                                             'secret', 'token', 'authorization', 'street',
                                             'address', 'postalcode', 'postcode', 'buyerinfo',
                                             'recipient', 'consignee', 'shipper', 'housenumber')))


def mask_sensitive(obj):
    """Redact credentials and direct personal identifiers before persistent logging."""
    if isinstance(obj, dict):
        return {k: ('[REDACTED]' if _is_sensitive_key(k) else mask_sensitive(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_sensitive(i) for i in obj]
    return obj


_RAW_TOKEN_RE = re.compile(
    r'(\"?(?:access_token|refresh_token|password|client_secret|api_secret|authorization|dhl-api-key)\"?\s*[:=]\s*)\"?[^\",&\s}]+\"?', re.I)


def body_to_text(raw, limit=20000):
    """Serialize payloads for diagnostics while redacting secrets and PII.

    Non-JSON text is treated as untrusted and common email/phone patterns are
    redacted too. Callers should prefer structured, minimal log metadata.
    """
    if raw is None or raw == '':
        return ''
    if isinstance(raw, (dict, list)):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            text = _RAW_TOKEN_RE.sub(r'\1"[REDACTED]"', str(raw))
            text = re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[EMAIL REDACTED]', text)
            text = re.sub(r'(?<!\w)(?:\+?\d[\d .()/-]{7,}\d)(?!\w)', '[PHONE REDACTED]', text)
            return text[:limit]
    return json.dumps(mask_sensitive(data), indent=2, ensure_ascii=False, default=str)[:limit]


# --------------------------------------------------------------------------
# Tracking status normalisation
# --------------------------------------------------------------------------
# ORDER MATTERS. Negative phrases ("nicht zugestellt", "not delivered") are
# tested BEFORE the positive "delivered" match, otherwise they contain the word
# "zugestellt"/"delivered" and are wrongly reported as delivered (known bug of
# the previous code base). Future-tense phrases ("wird zugestellt") are also
# tested before "delivered".
_NEG = re.compile(
    r"(nicht\s+zugestellt|not\s+(?:been\s+)?delivered|undeliverable|nicht\s+zustellbar|"
    r"zustellung\s+nicht|zustellversuch|delivery\s+(?:attempt\s+)?(?:failed|unsuccessful)|"
    r"could\s*n[o']?t\s+be\s+delivered|unable\s+to\s+deliver|failed\s+delivery)", re.I)
_RETURNED = re.compile(r"(retoure|return(?:ed)?\s+to\s+sender|zur[uü]ck\s+an\s+(?:den\s+)?absender)", re.I)
_OUT_FOR_DELIVERY = re.compile(
    r"(will\s+be\s+delivered|wird\b[^.]{0,40}\bzugestellt|out\s+for\s+delivery|"
    r"zustellfahrzeug|in\s+zustellung|delivery\s+vehicle)", re.I)
_DELIVERED = re.compile(r"\b(zugestellt|delivered)\b", re.I)
_PRE_TRANSIT = re.compile(r"(elektronisch\s+angek[uü]ndigt|data\s+received|pre-?transit|label\s+created)", re.I)
_IN_TRANSIT = re.compile(r"(transport|unterwegs|in\s+transit|sortier|paketzentrum|parcel\s+center|hub|depot)", re.I)

# DHL unified tracking 'statusCode' values -- VERIFY against the live API docs
# before relying on them in production.
_CODE_MAP = {
    'delivered': 'delivered', 'transit': 'in_transit', 'pre-transit': 'pre_transit',
    'failure': 'exception', 'returned': 'returned', 'unknown': 'unknown',
}


def normalize_tracking_status(text, code=None):
    text = (text or '').strip()
    if text:
        if _NEG.search(text):
            return 'delivery_failed'
        if _RETURNED.search(text):
            return 'returned'
        if _OUT_FOR_DELIVERY.search(text):
            return 'out_for_delivery'
        if _DELIVERED.search(text):
            return 'delivered'
    mapped = _CODE_MAP.get((code or '').strip().lower())
    if mapped:
        return mapped
    if text:
        if _PRE_TRANSIT.search(text):
            return 'pre_transit'
        if _IN_TRANSIT.search(text):
            return 'in_transit'
    return 'unknown'


# --------------------------------------------------------------------------
# DHL Parcel DE helpers
# --------------------------------------------------------------------------
# DHL Parcel DE Shipping v2 expects ISO 3166-1 ALPHA-3 country codes ("DEU").
ISO2_TO_ISO3 = {
    'AD': 'AND', 'AE': 'ARE', 'AF': 'AFG', 'AG': 'ATG', 'AI': 'AIA', 'AL': 'ALB', 'AM': 'ARM', 'AO': 'AGO',
    'AQ': 'ATA', 'AR': 'ARG', 'AS': 'ASM', 'AT': 'AUT', 'AU': 'AUS', 'AW': 'ABW', 'AX': 'ALA', 'AZ': 'AZE',
    'BA': 'BIH', 'BB': 'BRB', 'BD': 'BGD', 'BE': 'BEL', 'BF': 'BFA', 'BG': 'BGR', 'BH': 'BHR', 'BI': 'BDI',
    'BJ': 'BEN', 'BL': 'BLM', 'BM': 'BMU', 'BN': 'BRN', 'BO': 'BOL', 'BQ': 'BES', 'BR': 'BRA', 'BS': 'BHS',
    'BT': 'BTN', 'BV': 'BVT', 'BW': 'BWA', 'BY': 'BLR', 'BZ': 'BLZ', 'CA': 'CAN', 'CC': 'CCK', 'CD': 'COD',
    'CF': 'CAF', 'CG': 'COG', 'CH': 'CHE', 'CI': 'CIV', 'CK': 'COK', 'CL': 'CHL', 'CM': 'CMR', 'CN': 'CHN',
    'CO': 'COL', 'CR': 'CRI', 'CU': 'CUB', 'CV': 'CPV', 'CW': 'CUW', 'CX': 'CXR', 'CY': 'CYP', 'CZ': 'CZE',
    'DE': 'DEU', 'DJ': 'DJI', 'DK': 'DNK', 'DM': 'DMA', 'DO': 'DOM', 'DZ': 'DZA', 'EC': 'ECU', 'EE': 'EST',
    'EG': 'EGY', 'EH': 'ESH', 'ER': 'ERI', 'ES': 'ESP', 'ET': 'ETH', 'FI': 'FIN', 'FJ': 'FJI', 'FK': 'FLK',
    'FM': 'FSM', 'FO': 'FRO', 'FR': 'FRA', 'GA': 'GAB', 'GB': 'GBR', 'GD': 'GRD', 'GE': 'GEO', 'GF': 'GUF',
    'GG': 'GGY', 'GH': 'GHA', 'GI': 'GIB', 'GL': 'GRL', 'GM': 'GMB', 'GN': 'GIN', 'GP': 'GLP', 'GQ': 'GNQ',
    'GR': 'GRC', 'GS': 'SGS', 'GT': 'GTM', 'GU': 'GUM', 'GW': 'GNB', 'GY': 'GUY', 'HK': 'HKG', 'HM': 'HMD',
    'HN': 'HND', 'HR': 'HRV', 'HT': 'HTI', 'HU': 'HUN', 'ID': 'IDN', 'IE': 'IRL', 'IL': 'ISR', 'IM': 'IMN',
    'IN': 'IND', 'IO': 'IOT', 'IQ': 'IRQ', 'IR': 'IRN', 'IS': 'ISL', 'IT': 'ITA', 'JE': 'JEY', 'JM': 'JAM',
    'JO': 'JOR', 'JP': 'JPN', 'KE': 'KEN', 'KG': 'KGZ', 'KH': 'KHM', 'KI': 'KIR', 'KM': 'COM', 'KN': 'KNA',
    'KP': 'PRK', 'KR': 'KOR', 'KW': 'KWT', 'KY': 'CYM', 'KZ': 'KAZ', 'LA': 'LAO', 'LB': 'LBN', 'LC': 'LCA',
    'LI': 'LIE', 'LK': 'LKA', 'LR': 'LBR', 'LS': 'LSO', 'LT': 'LTU', 'LU': 'LUX', 'LV': 'LVA', 'LY': 'LBY',
    'MA': 'MAR', 'MC': 'MCO', 'MD': 'MDA', 'ME': 'MNE', 'MF': 'MAF', 'MG': 'MDG', 'MH': 'MHL', 'MK': 'MKD',
    'ML': 'MLI', 'MM': 'MMR', 'MN': 'MNG', 'MO': 'MAC', 'MP': 'MNP', 'MQ': 'MTQ', 'MR': 'MRT', 'MS': 'MSR',
    'MT': 'MLT', 'MU': 'MUS', 'MV': 'MDV', 'MW': 'MWI', 'MX': 'MEX', 'MY': 'MYS', 'MZ': 'MOZ', 'NA': 'NAM',
    'NC': 'NCL', 'NE': 'NER', 'NF': 'NFK', 'NG': 'NGA', 'NI': 'NIC', 'NL': 'NLD', 'NO': 'NOR', 'NP': 'NPL',
    'NR': 'NRU', 'NU': 'NIU', 'NZ': 'NZL', 'OM': 'OMN', 'PA': 'PAN', 'PE': 'PER', 'PF': 'PYF', 'PG': 'PNG',
    'PH': 'PHL', 'PK': 'PAK', 'PL': 'POL', 'PM': 'SPM', 'PN': 'PCN', 'PR': 'PRI', 'PS': 'PSE', 'PT': 'PRT',
    'PW': 'PLW', 'PY': 'PRY', 'QA': 'QAT', 'RE': 'REU', 'RO': 'ROU', 'RS': 'SRB', 'RU': 'RUS', 'RW': 'RWA',
    'SA': 'SAU', 'SB': 'SLB', 'SC': 'SYC', 'SD': 'SDN', 'SE': 'SWE', 'SG': 'SGP', 'SH': 'SHN', 'SI': 'SVN',
    'SJ': 'SJM', 'SK': 'SVK', 'SL': 'SLE', 'SM': 'SMR', 'SN': 'SEN', 'SO': 'SOM', 'SR': 'SUR', 'SS': 'SSD',
    'ST': 'STP', 'SV': 'SLV', 'SX': 'SXM', 'SY': 'SYR', 'SZ': 'SWZ', 'TC': 'TCA', 'TD': 'TCD', 'TF': 'ATF',
    'TG': 'TGO', 'TH': 'THA', 'TJ': 'TJK', 'TK': 'TKL', 'TL': 'TLS', 'TM': 'TKM', 'TN': 'TUN', 'TO': 'TON',
    'TR': 'TUR', 'TT': 'TTO', 'TV': 'TUV', 'TW': 'TWN', 'TZ': 'TZA', 'UA': 'UKR', 'UG': 'UGA', 'UM': 'UMI',
    'US': 'USA', 'UY': 'URY', 'UZ': 'UZB', 'VA': 'VAT', 'VC': 'VCT', 'VE': 'VEN', 'VG': 'VGB', 'VI': 'VIR',
    'VN': 'VNM', 'VU': 'VUT', 'WF': 'WLF', 'WS': 'WSM', 'XK': 'XKX', 'YE': 'YEM', 'YT': 'MYT', 'ZA': 'ZAF',
    'ZM': 'ZMB', 'ZW': 'ZWE',
}

# EU member states: no customs declaration needed between them.
EU_COUNTRIES = {
    'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU', 'IE',
    'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE',
}


def to_iso3(code):
    """ISO alpha-2 -> alpha-3, or None when the country is not in the supported list."""
    return ISO2_TO_ISO3.get((code or '').upper())


def extract_dhl_error(data, limit=600):
    """Collect every human-readable message from a DHL error / multi-status response.

    DHL nests the useful text: top-level ``status.title/detail``, per item
    ``items[].sstatus`` and ``items[].validationMessages[].property/validationMessage``.
    Reading only top-level keys hides the real reason (the old "Shipment creation failed").
    """
    parts = []

    def add(text):
        text = (text or '').strip() if isinstance(text, str) else ''
        if text and text not in parts:
            parts.append(text)

    if isinstance(data, dict):
        status = data.get('status')
        if isinstance(status, dict):
            add(status.get('title'))
            add(status.get('detail'))
        for key in ('title', 'detail', 'message', 'error', 'error_description'):
            add(data.get(key))
        for item in data.get('items') or []:
            if not isinstance(item, dict):
                continue
            sstatus = item.get('sstatus') or {}
            if isinstance(sstatus, dict) and (sstatus.get('status') or 200) >= 300:
                add(sstatus.get('title'))
                add(sstatus.get('detail'))
            for vm in item.get('validationMessages') or []:
                if isinstance(vm, dict):
                    prop, msg = vm.get('property'), vm.get('validationMessage')
                    add(f"{prop}: {msg}" if prop and msg else (msg or prop))
    return ' | '.join(parts)[:limit]