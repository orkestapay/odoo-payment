# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

from contextlib import contextmanager
from unittest.mock import patch

from odoo.addons.payment.tests.common import PaymentCommon
from odoo.addons.payment_orkestapay.utils import OrkestaPayApiError


SEND_API_REQUEST_PATH = (
    'odoo.addons.payment_orkestapay.models.payment_provider.PaymentProvider._send_api_request'
)
GET_TOKEN_PATH = (
    'odoo.addons.payment_orkestapay.models.payment_provider.PaymentProvider'
    '._orkestapay_get_access_token'
)
REQUESTS_PATH = 'odoo.addons.payment_orkestapay.models.payment_provider.requests.request'

# Fake test-only secret (base64 of 24 zero bytes); never a real credential.
TEST_WEBHOOK_SECRET = 'whsec_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'


class OrkestaPayCommon(PaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.currency_mxn = cls._enable_currency('MXN')
        cls.currency = cls.currency_mxn
        cls.amount = 990.0
        cls.reference = 'S00042-1'
        cls.order_id = 'ord_00000000000000000000000000000001'
        cls.payment_id = 'pay_00000000000000000000000000000002'

        cls.provider = cls._prepare_provider('orkestapay', update_values={
            'orkestapay_client_id': 'test-client-id',
            'orkestapay_client_secret': 'test-client-secret',
            'orkestapay_webhook_secret': TEST_WEBHOOK_SECRET,
        })
        cls.payment_method = cls.env.ref('payment.payment_method_card')
        cls.payment_method_id = cls.payment_method.id

    # === Helpers === #

    def _create_orkestapay_transaction(self, **values):
        """Create a redirect transaction that already has an OrkestaPay order."""
        return self._create_transaction(
            flow='redirect', provider_reference=self.order_id, **values
        )

    def _order_response(self, status='COMPLETED', amount=None, currency='MXN', reference=None):
        """Return an `OrderDetail` as returned by `GET /v1/orders/{order_id}`."""
        return {
            'order_id': self.order_id,
            'status': status,
            'merchant_order_id': reference or self.reference,
            'currency': currency,
            'total_amount': self.amount if amount is None else amount,
        }

    def _payment_response(
        self, status='COMPLETED', captured=None, message='Approved', with_card=True,
    ):
        """Return a `PaymentResponse` as listed by `GET /v1/orders/{order_id}/payments`."""
        amount = {'currency': 'MXN'}
        if captured is not None:
            amount['captured'] = captured
        return {
            'payment_id': self.payment_id,
            'order_id': self.order_id,
            'status': status,
            'amount': amount,
            'payment_source': {
                'type': 'CARD',
                'payment_method_id': 'pym_00000000000000000000000000000003',
                **({'card': {'brand': 'MASTERCARD', 'last_four': '8785', 'bin': '518600'}}
                   if with_card else {}),
            },
            'transactions': [{
                'type': 'PURCHASE', 'status': 'SUCCESS', 'message': message,
                'created_at': '1713480514319',
            }],
            'created_at': '1713480514319',
        }

    @contextmanager
    def _mock_api(self, routes):
        """Mock `PaymentProvider._send_api_request` with responses routed by endpoint.

        :param dict routes: `'<METHOD> <endpoint>'` -> response dict, exception instance, or
                            list of those consumed in order (the last one is repeated).
        :return: The list of `(method, endpoint, kwargs)` calls, filled as the API is called.
        """
        calls = []

        def send_api_request(_provider_self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            key = f'{method} {endpoint}'
            if key not in routes:
                raise AssertionError(f"Unexpected OrkestaPay API call: {key}")
            outcome = routes[key]
            if isinstance(outcome, list):
                outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch(SEND_API_REQUEST_PATH, autospec=True, side_effect=send_api_request):
            yield calls

    def _verification_routes(self, order_status='COMPLETED', payments=None, **order_kwargs):
        """Return the routes answering the fetch-to-verify queries of the order."""
        if payments is None:
            payments = [self._payment_response(captured=self.amount)] \
                if order_status == 'COMPLETED' else []
        return {
            f'GET /v1/orders/{self.order_id}': self._order_response(order_status, **order_kwargs),
            f'GET /v1/orders/{self.order_id}/payments': {'content': payments},
        }

    @staticmethod
    def _api_error(status_code=None, is_network_error=False):
        """Return an `OrkestaPayApiError` like the ones raised by the HTTP client."""
        return OrkestaPayApiError(
            "API error", status_code=status_code, is_network_error=is_network_error
        )
