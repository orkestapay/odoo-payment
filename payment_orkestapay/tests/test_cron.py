# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

from unittest.mock import patch

from odoo.exceptions import LockError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment_orkestapay import const
from odoo.addons.payment_orkestapay.tests.common import OrkestaPayCommon

TX_LOGGER = 'odoo.addons.payment_orkestapay.models.payment_transaction'


@tagged('post_install', '-at_install')
class TestReconciliationCron(OrkestaPayCommon):
    """Reconciliation of pending transactions."""

    def _create_aged_tx(self, suffix, minutes_old=20, state='pending', **values):
        """Create a transaction with its own OrkestaPay order, created `minutes_old` ago."""
        values.setdefault('provider_reference', f'ord_cron_{suffix}')
        tx = self._create_transaction(
            flow='redirect', reference=f'{self.reference}-{suffix}', state=state, **values
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE payment_transaction SET create_date = now() at time zone 'utc'"
            " - make_interval(mins => %s) WHERE id = %s",
            (minutes_old, tx.id),
        )
        tx.invalidate_recordset(['create_date'])
        return tx

    def _routes_for(self, tx, order_status='COMPLETED', error=None):
        """Return the fetch-to-verify routes of a transaction's order."""
        order_id = tx.provider_reference
        order = {
            'order_id': order_id,
            'status': order_status,
            'merchant_order_id': tx.reference,
            'currency': tx.currency_id.name,
            'total_amount': tx.amount,
        }
        payments = [self._payment_response(captured=tx.amount)] \
            if order_status == 'COMPLETED' else []
        return {
            f'GET /v1/orders/{order_id}': error or order,
            f'GET /v1/orders/{order_id}/payments': {'content': payments},
        }

    @mute_logger(TX_LOGGER)
    def test_an_error_does_not_stop_the_other_transactions(self):
        txs = [self._create_aged_tx(n) for n in range(1, 6)]
        routes = {}
        for tx in txs:
            routes.update(self._routes_for(
                tx, error=self._api_error(is_network_error=True) if tx == txs[2] else None,
            ))
        with self._mock_api(routes), self.assertLogs(TX_LOGGER, level='WARNING') as logs:
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertEqual(
            [tx.state for tx in txs], ['done', 'done', 'pending', 'done', 'done']
        )
        self.assertTrue(any(txs[2].reference in line for line in logs.output))

    def test_abandoned_checkout_is_canceled_once_expired(self):
        tx = self._create_aged_tx('abandoned', minutes_old=60 * 25, state='draft')
        with self._mock_api(self._routes_for(tx, order_status='EXPIRED')):
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertEqual(tx.state, 'cancel')

    def test_unpaid_checkout_stays_pending(self):
        tx = self._create_aged_tx('waiting', state='draft')
        with self._mock_api(self._routes_for(tx, order_status='CREATED')):
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertEqual(tx.state, 'pending')

    def test_only_transactions_in_the_age_window_are_reconciled(self):
        too_recent = self._create_aged_tx('recent', minutes_old=5)
        too_old = self._create_aged_tx('old', minutes_old=60 * 49)
        with self._mock_api({}) as calls:
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertFalse(calls)
        self.assertEqual((too_recent | too_old).mapped('state'), ['pending', 'pending'])

    def test_transactions_without_order_or_final_are_skipped(self):
        self._create_aged_tx('no-order', provider_reference=False, state='draft')
        self._create_aged_tx('done', state='done')
        self._create_aged_tx('refund', operation='refund', amount=-10.0)
        with self._mock_api({}) as calls:
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertFalse(calls)

    def test_transactions_of_another_environment_are_skipped(self):
        """Production transactions can't be queried with the Test Mode credentials."""
        live_tx = self._create_aged_tx('live')
        live_tx.is_live = True
        with self._mock_api({}) as calls:
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertFalse(calls)

    def test_disabled_provider_is_not_reconciled(self):
        self._create_aged_tx('disabled')
        self.provider.state = 'disabled'
        with self._mock_api({}) as calls:
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertFalse(calls)

    def test_batch_size_is_limited(self):
        txs = [self._create_aged_tx(f'batch-{n}', state='draft') for n in range(3)]
        routes = {}
        for tx in txs:
            routes.update(self._routes_for(tx, order_status='CREATED'))
        with self._mock_api(routes) as calls, patch.object(const, 'RECONCILE_BATCH_SIZE', 2):
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertEqual(len(calls), 4)  # 2 transactions x (order + payments).
        self.assertEqual(sorted(tx.state for tx in txs), ['draft', 'pending', 'pending'])

    def test_batches_rotate_so_unpaid_checkouts_do_not_monopolize_them(self):
        oldest, middle, newest = (
            self._create_aged_tx(name, minutes_old=age, state='draft')
            for name, age in (('oldest', 300), ('middle', 200), ('newest', 100))
        )
        routes = {}
        for tx in (oldest, middle, newest):
            routes.update(self._routes_for(tx, order_status='CREATED'))
        with self._mock_api(routes), patch.object(const, 'RECONCILE_BATCH_SIZE', 2):
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
            self.assertEqual(newest.state, 'draft')  # Not served by the first run.
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertEqual(newest.state, 'pending')  # Served first by the second run.

    def test_other_environment_does_not_use_up_the_batch(self):
        live_txs = [self._create_aged_tx(f'live-{n}', minutes_old=300) for n in range(2)]
        for tx in live_txs:
            tx.is_live = True
        test_tx = self._create_aged_tx('test-env', state='draft')
        with self._mock_api(self._routes_for(test_tx, order_status='CREATED')), \
                patch.object(const, 'RECONCILE_BATCH_SIZE', 1):
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertEqual(test_tx.state, 'pending')

    @mute_logger(TX_LOGGER)
    def test_locked_and_crashing_transactions_do_not_stop_the_others(self):
        locked, crashing, fine = (
            self._create_aged_tx(name, state='draft') for name in ('locked', 'crash', 'fine')
        )
        original = type(fine)._orkestapay_process_from_api

        def process(tx_self, source):
            if tx_self == locked:
                raise LockError("locked")
            if tx_self == crashing:
                raise KeyError("unexpected")
            return original(tx_self, source)

        with patch.object(
            type(fine), '_orkestapay_process_from_api', autospec=True, side_effect=process
        ), self._mock_api(self._routes_for(fine, order_status='COMPLETED')):
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertEqual((locked.state, crashing.state, fine.state), ('draft', 'draft', 'done'))
        self.assertTrue(crashing.orkestapay_last_reconcile_date)
        self.assertFalse(locked.orkestapay_last_reconcile_date)  # First in line next time.

    def test_cron_commits_each_transaction_and_stops_when_out_of_time(self):
        txs = [self._create_aged_tx(f'time-{n}', state='draft') for n in range(3)]
        routes = {}
        for tx in txs:
            routes.update(self._routes_for(tx, order_status='CREATED'))
        with self._mock_api(routes), patch.object(
            type(self.env['ir.cron']), '_commit_progress', return_value=0.0
        ) as commit_mock:
            self.env['payment.transaction'].with_context(
                cron_id=1
            )._cron_orkestapay_reconcile_pending()
        self.assertEqual(commit_mock.call_count, 1)  # No time left after the first one.
        self.assertEqual(sorted(tx.state for tx in txs), ['draft', 'draft', 'pending'])

    def test_post_processing_is_triggered_after_reconciling(self):
        tx = self._create_aged_tx('post', state='draft')
        cron = self.env.ref('payment.cron_post_process_payment_tx')
        with self._mock_api(self._routes_for(tx)), \
                patch.object(type(cron), '_trigger') as trigger_mock:
            self.env['payment.transaction']._cron_orkestapay_reconcile_pending()
        self.assertEqual(tx.state, 'done')
        trigger_mock.assert_called_once()
