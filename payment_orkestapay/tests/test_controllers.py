# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import LockError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.addons.payment_orkestapay import const
from odoo.addons.payment_orkestapay.tests.common import TEST_WEBHOOK_SECRET, OrkestaPayCommon

CONTROLLER_LOGGER = 'odoo.addons.payment_orkestapay.controllers.main'
TX_LOGGER = 'odoo.addons.payment_orkestapay.models.payment_transaction'


@tagged('post_install', '-at_install')
class TestOrkestaPayControllers(OrkestaPayCommon, PaymentHttpCommon):

    # === Helpers === #

    def _return_params(self, reference=None):
        reference = reference or self.reference
        return {
            'reference': reference,
            'access_token': self._generate_test_access_token(reference),
        }

    def _webhook_body(self, event_type='payment.purchase', reference=None, order_id=None):
        return json.dumps({
            'event_type': event_type,
            'type': 'payment',
            'version': 1,
            'data': {'payment': {
                'merchant_order_id': reference or self.reference,
                'order_id': order_id or self.order_id,
                'payment_id': self.payment_id,
                'status': 'COMPLETED',
            }},
        }).encode()

    @staticmethod
    def _signed_headers(body, secret=TEST_WEBHOOK_SECRET, timestamp=None):
        msg_id = 'msg_test_1'
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        key = base64.b64decode(secret.removeprefix('whsec_'))
        signature = base64.b64encode(
            hmac.new(key, f'{msg_id}.{timestamp}.'.encode() + body, hashlib.sha256).digest()
        ).decode()
        return {
            'Content-Type': 'application/json',
            'svix-id': msg_id,
            'svix-timestamp': timestamp,
            'svix-signature': f'v1,{signature}',
        }

    def _post_webhook(self, body, headers):
        return self.url_open(
            self._build_url(const.WEBHOOK_ROUTE), data=body, headers=headers, method='POST'
        )

    # === Buyer return === #

    def test_return_verifies_the_payment_and_redirects_to_the_status_page(self):
        tx = self._create_orkestapay_transaction()
        with self._mock_api(self._verification_routes('COMPLETED')) as calls:
            response = self._make_http_get_request(
                self._build_url(const.PAYMENT_RETURN_ROUTE), params=self._return_params()
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(tx.state, 'done')
        self.assertTrue(response.url.endswith('/payment/status'))

    def test_tampered_return_does_not_mark_done(self):
        """Opening the return URL while the API reports CREATED keeps the tx pending."""
        tx = self._create_orkestapay_transaction()
        with self._mock_api(self._verification_routes('CREATED')):
            self._make_http_get_request(
                self._build_url(const.PAYMENT_RETURN_ROUTE), params=self._return_params()
            )
        self.assertEqual(tx.state, 'pending')

    @mute_logger(CONTROLLER_LOGGER)
    def test_return_with_an_invalid_access_token_is_ignored(self):
        tx = self._create_orkestapay_transaction()
        with self._mock_api({}) as calls:
            response = self._make_http_get_request(
                self._build_url(const.PAYMENT_RETURN_ROUTE),
                params={'reference': self.reference, 'access_token': 'forged'},
            )
        self.assertFalse(calls)
        self.assertEqual(tx.state, 'draft')
        self.assertTrue(response.url.endswith('/payment/status'))

    def test_return_with_an_unknown_reference_is_ignored(self):
        with self._mock_api({}) as calls:
            response = self._make_http_get_request(
                self._build_url(const.PAYMENT_RETURN_ROUTE),
                params=self._return_params('UNKNOWN-1'),
            )
        self.assertFalse(calls)
        self.assertTrue(response.url.endswith('/payment/status'))

    @mute_logger(CONTROLLER_LOGGER)
    def test_return_while_the_transaction_is_locked_redirects_to_the_status_page(self):
        tx = self._create_orkestapay_transaction()
        with patch.object(
            type(tx), 'lock_for_update', side_effect=LockError("locked")
        ), self._mock_api({}) as calls:
            response = self._make_http_get_request(
                self._build_url(const.PAYMENT_RETURN_ROUTE), params=self._return_params()
            )
        self.assertFalse(calls)
        self.assertEqual(tx.state, 'draft')
        self.assertTrue(response.url.endswith('/payment/status'))

    # === Buyer cancellation === #

    def test_cancel_cancels_the_order_and_the_transaction(self):
        tx = self._create_orkestapay_transaction()
        routes = self._verification_routes('CREATED')
        routes[f'GET /v1/orders/{self.order_id}'] = [
            self._order_response('CREATED'), self._order_response('CANCELED'),
        ]
        routes[f'POST /v1/orders/{self.order_id}/cancel'] = {'status': 'CANCELED'}
        with self._mock_api(routes) as calls:
            response = self._make_http_get_request(
                self._build_url(const.PAYMENT_CANCEL_ROUTE), params=self._return_params()
            )
        self.assertEqual(tx.state, 'cancel')
        self.assertEqual(len([c for c in calls if c[1].endswith('/cancel')]), 1)
        self.assertTrue(response.url.endswith('/payment/status'))

    @mute_logger(CONTROLLER_LOGGER)
    def test_cancel_with_an_invalid_access_token_does_not_cancel(self):
        self._create_orkestapay_transaction()
        with self._mock_api({}) as calls:
            self._make_http_get_request(
                self._build_url(const.PAYMENT_CANCEL_ROUTE),
                params={'reference': self.reference, 'access_token': 'forged'},
            )
        self.assertFalse(calls)

    # === Webhook === #

    @mute_logger(CONTROLLER_LOGGER)
    def test_webhook_without_signature_is_rejected(self):
        tx = self._create_orkestapay_transaction()
        with self._mock_api({}) as calls:
            response = self._post_webhook(
                self._webhook_body(), {'Content-Type': 'application/json'}
            )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(calls)
        self.assertEqual(tx.state, 'draft')

    @mute_logger(CONTROLLER_LOGGER)
    def test_webhook_with_an_invalid_signature_is_rejected(self):
        self._create_orkestapay_transaction()
        body = self._webhook_body()
        other_secret = 'whsec_' + base64.b64encode(b'x' * 24).decode()
        headers = self._signed_headers(body, secret=other_secret)
        with self._mock_api({}) as calls:
            response = self._post_webhook(body, headers)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(calls)

    @mute_logger(CONTROLLER_LOGGER)
    def test_webhook_with_an_expired_signature_is_rejected(self):
        self._create_orkestapay_transaction()
        body = self._webhook_body()
        headers = self._signed_headers(body, timestamp=int(time.time()) - 600)
        with self._mock_api({}):
            response = self._post_webhook(body, headers)
        self.assertEqual(response.status_code, 401)

    def test_webhook_with_a_valid_signature_verifies_and_processes(self):
        tx = self._create_orkestapay_transaction()
        body = self._webhook_body()
        with self._mock_api(self._verification_routes('COMPLETED')) as calls:
            response = self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(tx.state, 'done')

    def test_webhook_schedules_the_post_processing_cron_with_a_delay(self):
        tx = self._create_orkestapay_transaction()
        cron = self.env.ref('payment.cron_post_process_payment_tx')
        body = self._webhook_body()
        before = fields.Datetime.now()
        with self._mock_api(self._verification_routes('COMPLETED')):
            self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(tx.state, 'done')
        triggers = self.env['ir.cron.trigger'].search([
            ('cron_id', '=', cron.id), ('call_at', '>', before),
        ])
        self.assertTrue(triggers)
        self.assertGreaterEqual(
            min(triggers.mapped('call_at')),
            before + timedelta(seconds=const.POST_PROCESSING_DELAY - 5),
        )
        # A repeated webhook does not schedule the cron again.
        with self._mock_api(self._verification_routes('COMPLETED')):
            tx.is_post_processed = False
            self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(
            self.env['ir.cron.trigger'].search_count([
                ('cron_id', '=', cron.id), ('call_at', '>', before),
            ]),
            len(triggers),
        )

    def test_signed_webhook_does_not_mark_done_if_the_api_disagrees(self):
        tx = self._create_orkestapay_transaction()
        body = self._webhook_body()
        with self._mock_api(self._verification_routes('CREATED')):
            response = self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tx.state, 'pending')

    def test_duplicate_webhook_is_idempotent(self):
        tx = self._create_orkestapay_transaction()
        body = self._webhook_body()
        with self._mock_api(self._verification_routes('COMPLETED')):
            self._post_webhook(body, self._signed_headers(body))
            last_state_change = tx.last_state_change
            for _i in range(2):
                response = self._post_webhook(body, self._signed_headers(body))
                self.assertEqual(response.status_code, 200)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.last_state_change, last_state_change)

    def test_webhook_with_an_unknown_reference_is_acknowledged(self):
        body = self._webhook_body(reference='OTHER-CHANNEL-1')
        with self._mock_api({}) as calls:
            response = self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(calls)

    @mute_logger(CONTROLLER_LOGGER)
    def test_webhook_with_a_mismatching_order_is_not_processed(self):
        tx = self._create_orkestapay_transaction()
        body = self._webhook_body(order_id='ord_other')
        with self._mock_api({}) as calls:
            response = self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(calls)
        self.assertEqual(tx.state, 'draft')

    def test_webhook_of_an_unhandled_event_is_acknowledged(self):
        self._create_orkestapay_transaction()
        body = self._webhook_body(event_type='customer.created')
        with self._mock_api({}) as calls:
            response = self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(calls)

    @mute_logger(CONTROLLER_LOGGER)
    def test_webhook_returns_500_when_the_api_times_out(self):
        tx = self._create_orkestapay_transaction()
        body = self._webhook_body()
        with self._mock_api({
            f'GET /v1/orders/{self.order_id}': self._api_error(is_network_error=True),
        }):
            response = self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(tx.state, 'draft')

    @mute_logger(CONTROLLER_LOGGER)
    def test_webhook_returns_500_when_the_transaction_is_locked(self):
        tx = self._create_orkestapay_transaction()
        body = self._webhook_body()
        with patch.object(
            type(tx), 'lock_for_update', side_effect=LockError("locked")
        ), self._mock_api({}) as calls:
            response = self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(response.status_code, 500)
        self.assertFalse(calls)

    def test_refund_webhook_is_handled_as_a_refund(self):
        tx = self._create_orkestapay_transaction(state='done')
        body = self._webhook_body(event_type='payment.refund')
        refund_handler = (
            'odoo.addons.payment_orkestapay.models.payment_transaction.PaymentTransaction'
            '._orkestapay_process_refund_event'
        )
        with patch(refund_handler) as refund_mock, self._mock_api({}) as calls:
            response = self._post_webhook(body, self._signed_headers(body))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(refund_mock.call_count, 1)
        self.assertFalse(calls)  # The payment itself is not re-processed.
        self.assertEqual(tx.state, 'done')

    def test_webhook_accepts_white_label_headers(self):
        tx = self._create_orkestapay_transaction()
        body = self._webhook_body()
        headers = {
            key.replace('svix-', 'webhook-'): value
            for key, value in self._signed_headers(body).items()
        }
        with self._mock_api(self._verification_routes('COMPLETED')):
            response = self._post_webhook(body, headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tx.state, 'done')
