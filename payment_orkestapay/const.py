# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

# API base URLs, keyed by provider state. A disabled provider uses the sandbox so that credentials
# can be tested with "Test connection" before the provider is activated.
API_URLS = {
    'test': 'https://api.sand.orkestapay.com',
    'enabled': 'https://api.orkestapay.com',
}
API_URL_DISABLED_FALLBACK = API_URLS['test']

# HTTP timeout, in seconds, for every call to the OrkestaPay API.
REQUEST_TIMEOUT = 10

# Domain the hosted checkout must be served from (e.g. checkout.sand.orkestapay.com): the buyer is
# only redirected to an HTTPS URL of this domain or one of its subdomains.
CHECKOUT_DOMAIN = 'orkestapay.com'

# OAuth `client_credentials` token endpoint and the margin, in seconds, before expiry under which
# a cached token is renewed.
TOKEN_ENDPOINT = '/v1/oauth/tokens'
TOKEN_REFRESH_MARGIN = 60

# API endpoints. `{order_id}` is URL-quoted by the caller.
CHECKOUTS_ENDPOINT = '/v1/checkouts'
ORDER_ENDPOINT = '/v1/orders/{order_id}'
ORDER_PAYMENTS_ENDPOINT = '/v1/orders/{order_id}/payments'
ORDER_CANCEL_ENDPOINT = '/v1/orders/{order_id}/cancel'
PAYMENT_ENDPOINT = '/v1/payments/{payment_id}'
PAYMENT_REFUND_ENDPOINT = '/v1/payments/{payment_id}/refund'

# Country sent with the order when neither the buyer nor the company has one.
DEFAULT_COUNTRY_CODE = 'MX'

# Odoo routes.
PAYMENT_RETURN_ROUTE = '/payment/orkestapay/return'
PAYMENT_CANCEL_ROUTE = '/payment/orkestapay/cancel'
WEBHOOK_ROUTE = '/payment/orkestapay/webhook'

# Currencies accepted by the provider (ISO 4217 codes).
SUPPORTED_CURRENCIES = ('MXN', 'USD')

# Payment methods activated by default, including the card brands.
DEFAULT_PAYMENT_METHOD_CODES = {
    # Primary payment methods.
    'card',
    # Brand payment methods.
    'visa',
    'mastercard',
    'amex',
}

# Checkout locales.
LOCALE_SPANISH = 'ES_LATAM'
LOCALE_DEFAULT = 'EN_US'

# Mapping of OrkestaPay order statuses (`OrderDetail.status`) to Odoo transaction states.
# A paid order becomes (PARTIALLY_)REFUNDED after a refund: the payment did happen, and the
# refunds are reconciled on their own transactions.
ORDER_STATUS_MAPPING = {
    'pending': ('CREATED', 'PROCESSING_PAYMENT', 'PAYMENT_ACTION_REQUIRED', 'PARTIALLY_PAID'),
    'done': ('COMPLETED', 'REFUNDED', 'PARTIALLY_REFUNDED'),
    'cancel': ('CANCELED', 'EXPIRED'),
    'error': ('FAILED',),
}

# Order statuses after which the order can no longer be paid or canceled.
ORDER_FINAL_STATUSES = (
    'COMPLETED',
    'REFUNDED',
    'PARTIALLY_REFUNDED',
    'CANCELED',
    'EXPIRED',
    'FAILED',
)

# Payment statuses (`PaymentResponse.status`).
PAYMENT_STATUS_COMPLETED = 'COMPLETED'
PAYMENT_STATUS_AUTHORIZED = 'AUTHORIZED'
# Payment statuses whose captured amount counts towards the paid amount of the order.
PAYMENT_STATUSES_CAPTURED = ('COMPLETED', 'REFUNDED', 'PARTIALLY_REFUNDED')

# Statuses of an operation on a payment (`TrxResponse.status`), e.g., a refund.
TRX_STATUS_SUCCESS = 'SUCCESS'
TRX_STATUSES_FAILED = ('DECLINED', 'FAILED')
TRX_TYPE_REFUND = 'REFUND'
# Tolerance, in seconds, between the clocks of Odoo and OrkestaPay when matching an unconfirmed
# refund with the refunds created after it was requested.
REFUND_MATCH_CLOCK_SKEW = 5 * 60

# Webhook events processed by the module. Other events are acknowledged and ignored.
HANDLED_WEBHOOK_EVENTS = (
    'payment.purchase',
    'payment.capture',
    'payment.cancel',
    'payment.refund',
    'payment.authorize',
)

# Delay, in seconds, before the post-processing cron runs after a webhook. It leaves the buyer's
# status page time to post-process the transaction itself.
POST_PROCESSING_DELAY = 2 * 60

# Reconciliation cron: age window of the transactions to reconcile and batch size.
RECONCILE_MIN_AGE = 10 * 60  # Seconds: leave time to the buyer's return and the webhook.
RECONCILE_MAX_AGE = 48 * 60 * 60  # Seconds.
RECONCILE_BATCH_SIZE = 100

# Webhook event handled as a refund notification.
REFUND_WEBHOOK_EVENT = 'payment.refund'

# Maximum accepted difference, in seconds, between the Svix timestamp and the current time.
WEBHOOK_TIMESTAMP_TOLERANCE = 5 * 60

# Tolerance used to check that the checkout breakdown adds up to the total amount.
BREAKDOWN_TOLERANCE = 0.01

# Keys whose values must never appear in the logs (`bin`: card data of the payment detail).
SENSITIVE_KEYS = {'client_secret', 'access_token', 'bin'}
