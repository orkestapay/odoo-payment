# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment_orkestapay import const
from odoo.addons.payment_orkestapay.tests.common import REQUESTS_PATH, OrkestaPayCommon
from odoo.addons.payment_orkestapay.utils import OrkestaPayApiError

PROVIDER_LOGGERS = (
    'odoo.addons.payment.models.payment_provider',
    'odoo.addons.payment_orkestapay.models.payment_provider',
)


def _response(status_code=200, payload=None, url='https://api.sand.orkestapay.com/'):
    response = requests.Response()
    response.status_code = status_code
    response.reason = 'OK' if status_code < 300 else 'Redirect' if status_code < 400 else 'Error'
    response.url = url
    response._content = json.dumps(payload).encode() if payload is not None else b''
    return response


def _token_response(token='token-1', expires_in=1800):
    return _response(
        200, {'token_type': 'Bearer', 'expires_in': expires_in, 'access_token': token}
    )


@tagged('post_install', '-at_install')
class TestOrkestaPayProvider(OrkestaPayCommon):

    def setUp(self):
        super().setUp()
        self.provider._orkestapay_clear_access_token()

    def _mock_http(self, *responses):
        """Patch `requests.request` to return the given responses (or raise them) in order."""
        responses = list(responses)

        def request(*_args, **_kwargs):
            outcome = responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return patch(REQUESTS_PATH, side_effect=request)

    # === Configuration === #

    def test_supported_currencies_are_mxn_and_usd(self):
        self.assertEqual(set(self.provider.available_currency_ids.mapped('name')), {'MXN', 'USD'})

    def test_card_is_the_default_payment_method(self):
        self.assertIn('card', self.provider._get_default_payment_method_codes())
        self.assertTrue(self.env.ref('payment.payment_method_card').active)

    def test_refunds_are_partial(self):
        self.assertEqual(self.provider.support_refund, 'partial')

    def test_every_credential_is_required_to_activate(self):
        for field_name in (
            'orkestapay_client_id', 'orkestapay_client_secret', 'orkestapay_webhook_secret'
        ):
            with self.subTest(field=field_name), self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    self.provider.write({field_name: False})

    def test_credentials_are_restricted_to_system_administrators(self):
        for field_name in (
            'orkestapay_client_id', 'orkestapay_client_secret', 'orkestapay_webhook_secret',
            'orkestapay_access_token', 'orkestapay_token_expiry',
        ):
            self.assertEqual(self.provider._fields[field_name].groups, 'base.group_system')

    def test_webhook_url_uses_the_base_url(self):
        self.assertEqual(
            self.provider.orkestapay_webhook_url,
            f'{self.provider.get_base_url()}/payment/orkestapay/webhook',
        )

    def test_environment_depends_on_the_state(self):
        self.assertEqual(
            self.provider._build_request_url('/v1/checkouts'),
            'https://api.sand.orkestapay.com/v1/checkouts',
        )
        self.provider.state = 'enabled'
        self.assertEqual(
            self.provider._build_request_url('/v1/checkouts'),
            'https://api.orkestapay.com/v1/checkouts',
        )

    # === Access token === #

    def test_token_is_reused_while_valid(self):
        with self._mock_http(
            _token_response(), _response(200, {'status': 'CREATED'}), _response(200, {}),
        ) as request_mock:
            self.provider._send_api_request('GET', '/v1/orders/ord_1')
            self.provider._send_api_request('GET', '/v1/orders/ord_1/payments')
        token_calls = [
            c for c in request_mock.call_args_list if c.args[1].endswith('/oauth/tokens')
        ]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(request_mock.call_count, 3)
        self.assertEqual(
            request_mock.call_args.kwargs['headers']['Authorization'], 'Bearer token-1'
        )

    def test_cached_token_is_read_from_the_provider(self):
        self.provider.sudo().write({
            'orkestapay_access_token': 'cached-token',
            'orkestapay_token_expiry': fields.Datetime.now() + timedelta(minutes=10),
        })
        self.assertEqual(self.provider._orkestapay_get_access_token(), 'cached-token')

    def test_token_is_renewed_shortly_before_expiry(self):
        self.provider.sudo().write({
            'orkestapay_access_token': 'old-token',
            'orkestapay_token_expiry': fields.Datetime.now() + timedelta(seconds=30),
        })
        with self._mock_http(_token_response('new-token')):
            self.assertEqual(self.provider._orkestapay_get_access_token(), 'new-token')

    @mute_logger(*PROVIDER_LOGGERS)
    def test_401_retries_once_with_a_new_token(self):
        with self._mock_http(
            _token_response('token-1'),
            _response(401, {'code': 'INVALID_TOKEN', 'message': 'Invalid token'}),
            _token_response('token-2'),
            _response(200, {'status': 'CREATED'}),
        ) as request_mock:
            result = self.provider._send_api_request('GET', '/v1/orders/ord_1')
        self.assertEqual(result, {'status': 'CREATED'})
        self.assertEqual(request_mock.call_count, 4)
        self.assertEqual(
            request_mock.call_args.kwargs['headers']['Authorization'], 'Bearer token-2'
        )

    @mute_logger(*PROVIDER_LOGGERS)
    def test_401_is_retried_exactly_once(self):
        unauthorized = _response(401, {'code': 'INVALID_TOKEN', 'message': 'Invalid token'})
        with self._mock_http(
            _token_response('token-1'), unauthorized, _token_response('token-2'), unauthorized,
        ) as request_mock, self.assertRaises(OrkestaPayApiError) as error:
            self.provider._send_api_request('GET', '/v1/orders/ord_1')
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(request_mock.call_count, 4)

    def test_token_is_invalidated_when_credentials_or_state_change(self):
        for values in (
            {'orkestapay_client_id': 'other-id'},
            {'orkestapay_client_secret': 'other-secret'},
            {'state': 'enabled'},
        ):
            self.provider.sudo().write({
                'orkestapay_access_token': 'cached-token',
                'orkestapay_token_expiry': fields.Datetime.now() + timedelta(minutes=10),
            })
            with self.subTest(values=values):
                self.provider.write(values)
                self.assertFalse(self.provider.sudo().orkestapay_access_token)

    def test_idempotency_key_header(self):
        with self._mock_http(_token_response(), _response(200, {})) as request_mock:
            self.provider._send_api_request(
                'POST', '/v1/orders/ord_1/cancel', json={}, idempotency_key='S00042-1-cancel'
            )
        self.assertEqual(
            request_mock.call_args.kwargs['headers']['Idempotency-Key'], 'S00042-1-cancel'
        )
        self.assertEqual(request_mock.call_args.kwargs['timeout'], const.REQUEST_TIMEOUT)

    @mute_logger(*PROVIDER_LOGGERS)
    def test_timeout_raises_a_network_error(self):
        with self._mock_http(_token_response(), requests.exceptions.Timeout()), \
                self.assertRaises(OrkestaPayApiError) as error:
            self.provider._send_api_request('GET', '/v1/orders/ord_1')
        self.assertTrue(error.exception.is_network_error)
        self.assertTrue(error.exception.is_retryable)

    def test_requests_do_not_follow_redirects(self):
        with self._mock_http(_token_response(), _response(200, {})) as request:
            self.provider._send_api_request('GET', '/v1/orders/ord_1')
        self.assertEqual(request.call_count, 2)  # Token, then the API call.
        for call in request.call_args_list:
            self.assertIs(call.kwargs.get('allow_redirects'), False)

    @staticmethod
    def _redirect_response(location):
        response = _response(307)
        response.headers['Location'] = location
        return response

    @mute_logger(*PROVIDER_LOGGERS)
    def test_api_redirect_is_an_error(self):
        redirect = self._redirect_response('https://example.com/v1/orders/ord_1')
        with self._mock_http(_token_response(), redirect) as request, \
                self.assertRaises(OrkestaPayApiError) as error:
            self.provider._send_api_request('GET', '/v1/orders/ord_1')
        self.assertEqual(error.exception.status_code, 307)
        self.assertEqual(request.call_count, 2)  # Token, then the API call; no retry.

    @mute_logger(*PROVIDER_LOGGERS)
    def test_token_redirect_is_an_error(self):
        redirect = self._redirect_response('https://example.com/v1/oauth/tokens')
        with self._mock_http(redirect) as request, \
                self.assertRaises(OrkestaPayApiError) as error:
            self.provider._send_api_request('GET', '/v1/orders/ord_1')
        self.assertEqual(error.exception.status_code, 307)
        self.assertEqual(request.call_count, 1)  # The client secret isn't sent anywhere else.

    def test_api_error_is_parsed_and_logged_with_request_id(self):
        error_body = {
            'request_id': '664bec7ff960e690abcdb507afa50d28',
            'category': 'ERROR',
            'message': 'The current resource status does not allow this operation.',
            'code': 'INVALID_STATUS',
        }
        with self._mock_http(_token_response(), _response(400, error_body)), \
                self.assertLogs('odoo.addons', level='ERROR') as logs, \
                self.assertRaises(OrkestaPayApiError) as error:
            self.provider._send_api_request('POST', '/v1/orders/ord_1/cancel', json={})
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(error.exception.error_code, 'INVALID_STATUS')
        self.assertFalse(error.exception.is_retryable)
        self.assertIn(error_body['message'], str(error.exception))
        self.assertTrue(any(error_body['request_id'] in line for line in logs.output))

    # === Test connection === #

    def test_test_connection_success(self):
        with self._mock_http(_token_response()):
            action = self.provider.action_orkestapay_test_connection()
        self.assertEqual(action['params']['type'], 'success')

    @mute_logger(*PROVIDER_LOGGERS)
    def test_test_connection_failure_shows_the_api_message(self):
        with self._mock_http(
            _response(400, {'error': 'invalid_client', 'message': 'Invalid client credentials'})
        ):
            action = self.provider.action_orkestapay_test_connection()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertIn('Invalid client credentials', action['params']['message'])

    # === Logging === #

    def test_payload_and_response_are_not_logged_at_info_level(self):
        payload = {
            'completed_redirect_url': 'https://shop.test/return?reference=R1&access_token=abc123',
            'order': {'customer': {'email': 'buyer@example.com'}},
        }
        response_body = {'checkout_redirect_url': 'https://checkout.test/chk_secret-path'}
        with self._mock_http(_token_response(), _response(201, response_body)), \
                self.assertLogs('odoo.addons', level='INFO') as logs:
            self.provider._send_api_request('POST', '/v1/checkouts', json=payload)
        output = '\n'.join(logs.output)
        self.assertNotIn('abc123', output)
        self.assertNotIn('buyer@example.com', output)
        self.assertNotIn('chk_secret-path', output)

    def test_return_url_access_token_is_masked_in_debug_logs(self):
        payload = {
            'completed_redirect_url': 'https://shop.test/return?reference=R1&access_token=abc123',
        }
        with self._mock_http(_token_response(), _response(201, {})), \
                self.assertLogs('odoo.addons', level='DEBUG') as logs:
            self.provider._send_api_request('POST', '/v1/checkouts', json=payload)
        output = '\n'.join(logs.output)
        self.assertNotIn('abc123', output)
        self.assertIn('access_token=[REDACTED]', output)

    def test_secrets_are_never_logged(self):
        with self._mock_http(
            _token_response('secret-token-value'), _response(200, {'status': 'CREATED'}),
        ), self.assertLogs('odoo.addons', level='DEBUG') as logs:
            self.provider._send_api_request('GET', '/v1/orders/ord_1')
        output = '\n'.join(logs.output)
        self.assertNotIn('test-client-secret', output)
        self.assertNotIn('secret-token-value', output)
        self.assertNotIn(self.provider.sudo().orkestapay_webhook_secret, output)
