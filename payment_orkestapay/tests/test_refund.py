# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

import time
from unittest.mock import patch

from odoo.exceptions import LockError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment_orkestapay.tests.common import OrkestaPayCommon

TX_LOGGER = 'odoo.addons.payment_orkestapay.models.payment_transaction'


@tagged('post_install', '-at_install')
class TestRefund(OrkestaPayCommon):
    """Refunds requested from Odoo."""

    def setUp(self):
        super().setUp()
        self.source_tx = self._create_orkestapay_transaction(
            state='done', orkestapay_payment_id=self.payment_id
        )
        self.refund_route = f'POST /v1/payments/{self.payment_id}/refund'

    def _trx(self, status='SUCCESS', amount=None, message='Refund approved'):
        return {
            'type': 'REFUND',
            'transaction_id': 'ref_0001',
            'status': status,
            'amount': self.amount if amount is None else amount,
            'message': message,
        }

    def test_full_refund(self):
        with self._mock_api({self.refund_route: self._trx()}) as calls:
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(refund_tx.provider_reference, 'ref_0001')
        self.assertEqual(refund_tx.amount, -self.amount)
        self.assertEqual(len(calls), 1)
        kwargs = calls[0][2]
        self.assertEqual(kwargs['idempotency_key'], refund_tx.reference)
        self.assertEqual(kwargs['json']['amount'], self.amount)
        self.assertIn(refund_tx.reference, kwargs['json']['description'])

    def test_partial_refund_sends_the_partial_amount(self):
        with self._mock_api({self.refund_route: self._trx(amount=300.0)}) as calls:
            refund_tx = self.source_tx._refund(amount_to_refund=300.0)
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(calls[0][2]['json']['amount'], 300.0)

    @mute_logger(TX_LOGGER)
    def test_declined_refund_sets_the_refund_in_error(self):
        with self._mock_api({
            self.refund_route: self._trx(status='DECLINED', message='Insufficient balance'),
        }):
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'error')
        self.assertEqual(refund_tx.state_message, 'Insufficient balance')

    @mute_logger(TX_LOGGER)
    def test_timeout_is_retried_once_with_the_same_idempotency_key(self):
        with self._mock_api({
            self.refund_route: [self._api_error(is_network_error=True), self._trx()],
        }) as calls:
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {call[2]['idempotency_key'] for call in calls}, {refund_tx.reference}
        )

    def _payments_route(self, *refunds):
        """Return the payments route with the given `(id, amount[, created_at_ms])` refunds."""
        now_ms = int(time.time() * 1000)
        payment = self._payment_response('PARTIALLY_REFUNDED', captured=self.amount)
        payment['payment_id'] = self.payment_id
        payment['transactions'] += [
            {
                'type': 'REFUND', 'status': 'SUCCESS', 'transaction_id': refund[0],
                'amount': refund[1], 'created_at': str(refund[2] if len(refund) > 2 else now_ms),
            }
            for refund in refunds
        ]
        return {f'GET /v1/orders/{self.order_id}/payments': {'content': [payment]}}

    def _unknown_outcome_routes(self, *refunds):
        """Routes where both refund attempts end with an unknown outcome."""
        routes = self._payments_route(*refunds)
        routes[self.refund_route] = [
            self._api_error(is_network_error=True), self._api_error(500),
        ]
        return routes

    @staticmethod
    def _count(calls, method):
        return len([call for call in calls if call[0] == method])

    @mute_logger(TX_LOGGER)
    def test_second_timeout_asks_to_check_the_portal(self):
        routes = self._payments_route()
        routes[self.refund_route] = self._api_error(is_network_error=True)
        with self._mock_api(routes) as calls:
            refund_tx = self.source_tx._refund()
        self.assertEqual(self._count(calls, 'POST'), 2)
        self.assertEqual(refund_tx.state, 'error')
        self.assertIn('portal', refund_tx.state_message)

    @mute_logger(TX_LOGGER)
    def test_replayed_key_answered_with_500_adopts_the_refund_made(self):
        """Seen in sandbox: the 1st attempt refunds but its answer is lost, and the retry with
        the same key gets a 500 instead of the original response."""
        with self._mock_api(self._unknown_outcome_routes(('ref_made_1', self.amount))) as calls, \
                patch.object(type(self.source_tx), '_log_message_on_linked_documents') as log_mock:
            refund_tx = self.source_tx._refund()
        self.assertEqual(self._count(calls, 'POST'), 2)
        self.assertEqual(self._count(calls, 'GET'), 1)
        self.assertEqual(
            {c[2]['idempotency_key'] for c in calls if c[0] == 'POST'}, {refund_tx.reference}
        )
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(refund_tx.provider_reference, 'ref_made_1')
        self.assertTrue(
            any('ref_made_1' in call.args[0] for call in log_mock.call_args_list)
        )

    @mute_logger(TX_LOGGER)
    def test_4xx_on_the_retry_is_still_an_unknown_outcome(self):
        routes = self._payments_route(('ref_made_2', self.amount))
        routes[self.refund_route] = [
            self._api_error(is_network_error=True), self._api_error(409),
        ]
        with self._mock_api(routes):
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(refund_tx.provider_reference, 'ref_made_2')

    @mute_logger(TX_LOGGER)
    def test_unknown_outcome_with_ambiguous_refunds_asks_to_check_the_portal(self):
        with self._mock_api(
            self._unknown_outcome_routes(('ref_a', self.amount), ('ref_b', self.amount))
        ):
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'error')
        self.assertIn('portal', refund_tx.state_message)
        self.assertFalse(refund_tx.provider_reference)

    @mute_logger(TX_LOGGER)
    def test_unknown_outcome_ignores_linked_older_and_other_amount_refunds(self):
        self.source_tx.orkestapay_external_refund_ids = 'ref_external'
        an_hour_ago_ms = int(time.time() * 1000) - 3600 * 1000
        with self._mock_api(self._unknown_outcome_routes(
            ('ref_external', self.amount),  # Already noted as made outside Odoo.
            ('ref_older', self.amount, an_hour_ago_ms),  # Created before the request.
            ('ref_other_amount', 1.0),
            ('ref_bad_amount', 'n/a'),  # Malformed data is skipped, not raised.
        )):
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'error')
        self.assertIn('portal', refund_tx.state_message)

    @mute_logger(TX_LOGGER)
    def test_unknown_outcome_when_payments_cannot_be_listed(self):
        routes = self._unknown_outcome_routes()
        routes[f'GET /v1/orders/{self.order_id}/payments'] = self._api_error(503)
        with self._mock_api(routes):
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'error')
        self.assertIn('portal', refund_tx.state_message)

    @mute_logger(TX_LOGGER)
    def test_5xx_is_retried_with_the_same_key(self):
        with self._mock_api({
            self.refund_route: [self._api_error(503), self._trx()],
        }) as calls:
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(len(calls), 2)
        self.assertEqual({call[2]['idempotency_key'] for call in calls}, {refund_tx.reference})

    def test_rejected_request_is_not_retried(self):
        with self._mock_api({self.refund_route: self._api_error(400)}) as calls:
            refund_tx = self.source_tx._refund()
        self.assertEqual(len(calls), 1)
        self.assertEqual(refund_tx.state, 'error')

    @mute_logger(TX_LOGGER, 'odoo.addons.payment.models.payment_transaction')
    def test_amount_mismatch_keeps_the_refund_id(self):
        """The refund exists in OrkestaPay: its id must be kept for the webhook."""
        with self._mock_api({self.refund_route: self._trx(amount=1.0)}):
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'error')
        self.assertEqual(refund_tx.provider_reference, 'ref_0001')

    @mute_logger(TX_LOGGER)
    def test_unknown_refund_status_sets_the_refund_in_error(self):
        with self._mock_api({self.refund_route: self._trx(status='PENDING')}):
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'error')
        self.assertIn('portal', refund_tx.state_message)

    def test_locked_payment_refuses_the_refund_without_calling_the_api(self):
        with patch.object(
            type(self.source_tx), 'lock_for_update', side_effect=LockError("locked")
        ), self._mock_api({}) as calls:
            refund_tx = self.source_tx._refund()
        self.assertFalse(calls)
        self.assertEqual(refund_tx.state, 'error')

    @mute_logger(TX_LOGGER)
    def test_post_processing_failure_keeps_the_refund_done(self):
        with patch.object(
            type(self.source_tx), '_post_process', side_effect=Exception("accounting error")
        ), self._mock_api({self.refund_route: self._trx()}):
            refund_tx = self.source_tx._refund()
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(refund_tx.provider_reference, 'ref_0001')

    def test_refund_without_payment_id_does_not_call_the_api(self):
        self.source_tx.orkestapay_payment_id = False
        with self._mock_api({}) as calls:
            refund_tx = self.source_tx._refund()
        self.assertFalse(calls)
        self.assertEqual(refund_tx.state, 'error')


@tagged('post_install', '-at_install')
class TestRefundWebhook(OrkestaPayCommon):
    """`payment.refund` webhook events."""

    def setUp(self):
        super().setUp()
        self.source_tx = self._create_orkestapay_transaction(
            state='done', orkestapay_payment_id=self.payment_id
        )

    def _patch_log(self):
        return patch.object(type(self.source_tx), '_log_message_on_linked_documents')

    def _payments_with_refunds(self, *refunds):
        payment = self._payment_response('PARTIALLY_REFUNDED', captured=self.amount)
        payment['transactions'] += [
            {'type': 'REFUND', 'status': status, 'transaction_id': refund_id, 'amount': amount}
            for refund_id, amount, status in refunds
        ]
        return {f'GET /v1/orders/{self.order_id}/payments': {'content': [payment]}}

    def test_external_refund_is_noted_once(self):
        routes = self._payments_with_refunds(('ref_ext_1', 100.0, 'SUCCESS'))
        with self._mock_api(routes), self._patch_log() as log_mock:
            self.source_tx._orkestapay_process_refund_event()
            self.source_tx._orkestapay_process_refund_event()  # Duplicate webhook.
        self.assertEqual(log_mock.call_count, 1)
        self.assertIn('ref_ext_1', log_mock.call_args.args[0])
        self.assertEqual(self.source_tx.orkestapay_external_refund_ids, 'ref_ext_1')
        self.assertFalse(self.source_tx.child_transaction_ids)  # No refund tx, no payment.

    def test_refund_made_from_odoo_is_ignored(self):
        self._create_transaction(
            flow='redirect', reference=f'R-{self.reference}', operation='refund',
            amount=-100.0, provider_reference='ref_odoo_1', state='done',
            source_transaction_id=self.source_tx.id,
        )
        routes = self._payments_with_refunds(('ref_odoo_1', 100.0, 'SUCCESS'))
        with self._mock_api(routes), self._patch_log() as log_mock:
            self.source_tx._orkestapay_process_refund_event()
        self.assertFalse(log_mock.called)

    def test_failed_refund_is_not_noted(self):
        routes = self._payments_with_refunds(('ref_ext_2', 100.0, 'DECLINED'))
        with self._mock_api(routes), self._patch_log() as log_mock:
            self.source_tx._orkestapay_process_refund_event()
        self.assertFalse(log_mock.called)

    def test_new_external_refund_is_added_to_the_noted_ones(self):
        self.source_tx.orkestapay_external_refund_ids = 'ref_ext_1'
        routes = self._payments_with_refunds(
            ('ref_ext_1', 100.0, 'SUCCESS'), ('ref_ext_2', 50.0, 'SUCCESS'),
        )
        with self._mock_api(routes), self._patch_log() as log_mock:
            self.source_tx._orkestapay_process_refund_event()
        self.assertEqual(log_mock.call_count, 1)
        self.assertEqual(
            self.source_tx.orkestapay_external_refund_ids.split(), ['ref_ext_1', 'ref_ext_2']
        )
