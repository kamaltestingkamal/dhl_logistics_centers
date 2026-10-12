# DHL + Amazon Logistics Control Center (Odoo 19)

Standalone custom logistics module for Odoo 19. It depends on `base`, `web`, `mail`, and `bus`; it does not require Odoo Sales or Inventory as the source of truth.

## Important verification status

This archive has received Python syntax, XML parsing, JavaScript syntax, manifest-path, and selected pure-helper checks. **Odoo install/upgrade tests and Odoo ORM tests were not run in the build environment because Odoo 19 is not installed there.** DHL sandbox and Amazon SP-API requests were not run from this environment. Do not treat static checks as live integration verification.

## Upgrade / install in a test database first

```bash
odoo-bin -d <test_database> -u dhl_logistics_center --stop-after-init
odoo-bin -d <test_database> -u dhl_logistics_center --test-tags /dhl_logistics_center --stop-after-init
```

For a fresh install, install the module from Apps or use the appropriate `-i dhl_logistics_center` command. Back up the database and existing addon directory before replacing the module. After upgrade, hard-refresh the browser (Ctrl+F5).

## Implemented foundations

- Custom products, customers/addresses, orders/order lines, shipments/packages, tracking events, returns, API/sync/automation logs, notifications, dashboard, agent health checks, and security groups.
- DHL Parcel DE Shipping v2 request construction, local validation, dry-run validation, shipment booking, label storage, and error classification. These API calls still need sandbox verification with the account's credentials and correct environment-specific billing configuration.
- DHL Parcel DE Tracking XML request/response handling with environment-specific endpoints, the `DHL-API-Key` header, Basic API authentication, sandbox public tracking credentials, and configurable production tracking-user credentials.
- Safe tracking refresh that does not treat DHL HTTP-200 XML authentication errors as successful calls.
- Unknown DHL booking-result protection: after a timeout/network/ambiguous response during the actual booking request, the record is blocked from retrying or creating a second shipment until a manager checks the DHL portal and explicitly confirms no booking exists.
- Amazon LWA token handling and SP-API connection/order/listing-sync foundation, with explicit Sandbox/Production endpoint selection (Sandbox default), idempotent order/product upserts, marketplace-aware listing pagination, customer/address reuse, sync history, bounded safe retry queue, and CSV exports. Cross-marketplace SKU/ASIN conflicts are stopped for review because the current product model stores one primary marketplace mapping.
- Role groups for User, Operator, Read-only, and Manager; API payload logging applies redaction helpers.

## Known limitations / items that must not be mistaken for live features

- **DHL shipment cancellation:** not implemented as a verified DHL API operation. Existing bookings are blocked from local-only cancellation.
- **DHL return labels / manifest generation / confirmed DHL rate quotes:** not implemented as verified API workflows. The Rates menu remains a clearly labelled placeholder; do not use it as a real quote.
- **Amazon returns import:** the module has an idempotent import boundary for normalized return data, but no live return source/endpoint adapter is enabled. Manual return management is available.
- **Amazon shipment confirmation/status write-back:** not implemented. Do not claim Amazon accepted a shipment confirmation.
- **Amazon product images, weight, dimensions, customs fields, and price:** not automatically filled unless a verified API response and permission provide them. Current listing sync focuses on available SKU/title/ASIN metadata; logistics fields are not guessed.
- **Amazon restricted buyer/recipient data:** import depends on the seller's app authorization and SP-API roles. Missing/redacted address data is reported rather than invented.
- **Live production verification:** not performed. Production tracking credentials/API approval and production shipping access must be verified by the operator.

## DHL sandbox setup checklist

1. Create/choose the DHL Parcel DE Shipping sandbox connection and enter the sandbox API key/secret, supported credentials, and a valid sandbox billing number.
2. Authenticate/test the shipping connection; verify the connection state reflects the real response.
3. If tracking uses separate developer-portal credentials, enter them under **Parcel DE Tracking Credentials**. The tracking key can fall back to the main API key/secret if the separate fields are empty.
4. Create a test order with a real product weight and complete address. Confirm local validation catches missing fields.
5. Run DHL dry-run validation and shipment creation only with sandbox configuration. Check the returned DHL shipment number and label data.
6. Test tracking with DHL's documented sandbox sample numbers; a generated sandbox label may not have tracking events. “No data” is not automatically a parser failure.
7. Simulate timeouts and HTTP errors using mocked tests. Confirm uncertain booking outcomes cannot be blindly retried.

## Amazon test checklist

1. Leave Amazon Environment set to Sandbox (the default) and configure LWA client ID, client secret, refresh token, seller ID, region, and marketplace IDs in a test database.
2. Confirm the connection test uses the seller/marketplace endpoint with the account's granted roles.
3. Test order sync with permitted data; verify duplicates update existing records and restricted/missing address fields do not create fabricated data.
4. Test listing sync one marketplace per request, including pagination/resume and multiple marketplace IDs.
5. Verify throttling, expired tokens, unauthorized roles, partial failures, and retry exhaustion. Amazon static sandbox responses are pattern-matched mocks, not real seller orders; use them to validate request/response plumbing, then use an authorized non-production workflow where available.

## Upgrade validation required before production

- Install and upgrade in a disposable Odoo 19 database using the exact target build.
- Run all Odoo tests and inspect server logs; fix view/model/ACL/cron issues discovered at runtime.
- Verify user/operator/manager/read-only access, including direct RPC write attempts and multi-company rules.
- Run DHL sandbox tests and Amazon SP-API authorized tests. Keep separate evidence for sandbox, production approval, and production verification.
- Verify backup/restore before moving the module to production.

The module is **not labelled 100% production-ready** until these runtime and external integration tests pass.
