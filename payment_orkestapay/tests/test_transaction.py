# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged
from odoo.tools import float_compare, mute_logger

from odoo.addons.payment_orkestapay import const
from odoo.addons.payment_orkestapay.tests.common import OrkestaPayCommon

TX_MODULE = 'odoo.addons.payment_orkestapay.models.payment_transaction'
TX_LOGGER = TX_MODULE


@tagged('post_install', '-at_install')
class TestCheckoutPayload(OrkestaPayCommon):

    def test_single_line_payload_without_sales_order(self):
        tx = self._create_transaction(flow='redirect', amount=10.55)
        payload = tx._orkestapay_prepare_checkout_payload()
        order = payload['order']
        self.assertEqual(order['merchant_order_id'], self.reference)
        self.assertEqual(order['currency'], 'MXN')
        self.assertEqual(order['total_amount'], 10.55)
        self.assertEqual(order['subtotal_amount'], 10.55)
        self.assertEqual(order['products'], [{
            'product_id': self.reference,
            'name': f'Order {self.reference}',
            'quantity': 1,
            'unit_price': 10.55,
        }])
        for key in ('taxes', 'discounts', 'shipping_details', 'expires_at'):
            self.assertNotIn(key, order)
        self.assertNotIn('expires_at', payload)
        self.assertFalse(payload['allow_save_payment_methods'])
        self.assertEqual(order['customer']['email'], self.partner.email)
        self.assertEqual(order['customer']['merchant_customer_id'], str(self.partner.id))
        self.assertEqual(order['metadata']['odoo_tx_id'], str(tx.id))

    def test_amount_is_rounded_to_the_currency_decimals(self):
        tx = self._create_transaction(flow='redirect', amount=10.554999)
        self.assertEqual(tx._orkestapay_prepare_checkout_payload()['order']['total_amount'], 10.55)

    def test_return_urls_carry_the_reference_and_an_access_token(self):
        tx = self._create_transaction(flow='redirect')
        payload = tx._orkestapay_prepare_checkout_payload()
        base_url = self.provider.get_base_url()
        for key, route in (
            ('completed_redirect_url', '/payment/orkestapay/return'),
            ('canceled_redirect_url', '/payment/orkestapay/cancel'),
        ):
            url = urlsplit(payload[key])
            self.assertEqual(f'{url.scheme}://{url.netloc}{url.path}', f'{base_url}{route}')
            query = parse_qs(url.query)
            self.assertEqual(query['reference'], [self.reference])
            self.assertEqual(
                query['access_token'], [self._generate_test_access_token(self.reference)]
            )

    def test_locale_without_request_depends_on_the_buyer_language(self):
        self.env['res.lang']._activate_lang('es_MX')
        self.env['res.lang']._activate_lang('fr_FR')
        for lang, locale in (('es_MX', 'ES_LATAM'), ('en_US', 'EN_US'), ('fr_FR', 'EN_US')):
            with self.subTest(lang=lang):
                partner = self.env['res.partner'].create({
                    'name': "Buyer", 'email': 'buyer@example.com', 'lang': lang,
                })
                tx = self._create_transaction(
                    flow='redirect', reference=f'{self.reference}-{lang}', partner_id=partner.id
                )
                self.assertEqual(tx._orkestapay_prepare_checkout_payload()['locale'], locale)

    def test_locale_follows_the_language_the_buyer_is_browsing_in(self):
        """A returning buyer's contact may keep another language than the one being used."""
        self.env['res.lang']._activate_lang('es_MX')
        for browsing_lang, contact_lang, locale in (
            ('es_MX', 'en_US', 'ES_LATAM'),
            ('en_US', 'es_MX', 'EN_US'),
        ):
            with self.subTest(browsing_lang=browsing_lang):
                partner = self.env['res.partner'].create({
                    'name': "Buyer", 'email': 'buyer@example.com', 'lang': contact_lang,
                })
                tx = self._create_transaction(
                    flow='redirect', reference=f'{self.reference}-{browsing_lang}',
                    partner_id=partner.id,
                )
                fake_request = Mock(cookies={'frontend_lang': browsing_lang})
                with patch(f'{TX_MODULE}.request', fake_request):
                    self.assertEqual(tx._orkestapay_get_locale(), locale)

    def test_locale_without_browsing_language_uses_the_buyer_language(self):
        """A request without the website language cookie (e.g., no `website` module)."""
        self.env['res.lang']._activate_lang('es_MX')
        partner = self.env['res.partner'].create({
            'name': "Buyer", 'email': 'buyer@example.com', 'lang': 'es_MX',
        })
        tx = self._create_transaction(flow='redirect', partner_id=partner.id)
        with patch(f'{TX_MODULE}.request', Mock(cookies={})):
            self.assertEqual(tx._orkestapay_get_locale(), 'ES_LATAM')

    def test_phone_is_split_into_country_code_and_number(self):
        for phone, expected in (
            ('+52 55 1234 5678', {'number': '5512345678', 'country_code': '52'}),
            ('55 1234 5678', {'number': '5512345678', 'country_code': '52'}),
            ('+1 212 555 0100', None),  # International number from another country.
            ('', None),
        ):
            with self.subTest(phone=phone):
                partner = self.env['res.partner'].create({
                    'name': "Buyer", 'phone': phone, 'country_id': self.env.ref('base.mx').id,
                })
                tx = self._create_transaction(
                    flow='redirect', reference=f'{self.reference}-{phone}', partner_id=partner.id
                )
                self.assertEqual(tx._orkestapay_format_phone(), expected)


@tagged('post_install', '-at_install')
class TestCheckoutBreakdown(OrkestaPayCommon):
    """Breakdown from the sales order. Requires the `sale` module."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.has_sale = 'sale_order_ids' in cls.env['payment.transaction']._fields
        if not cls.has_sale:
            return
        cls.tax_group_iva = cls._create_tax_group("IVA")
        cls.tax_16 = cls._create_tax("IVA 16%", 16.0)
        cls.product = cls.env['product.product'].create({
            'name': "Widget", 'default_code': 'WID-1', 'list_price': 100.0,
        })
        cls.discount_product = cls.env['product.product'].create({
            'name': "Discount", 'list_price': 0.0,
        })
        cls.pricelist_mxn = cls.env['product.pricelist'].create({
            'name': "MXN", 'currency_id': cls.currency_mxn.id,
        })

    def setUp(self):
        super().setUp()
        if not self.has_sale:
            self.skipTest("The `sale` module is not installed.")

    @classmethod
    def _create_tax_group(cls, name):
        return cls.env['account.tax.group'].create({
            'name': name, 'country_id': cls.env.ref('base.mx').id,
        })

    @classmethod
    def _create_tax(cls, name, amount, amount_type='percent', tax_group=None, **values):
        return cls.env['account.tax'].create({
            'name': name,
            'amount': amount,
            'amount_type': amount_type,
            'type_tax_use': 'sale',
            'tax_group_id': (tax_group or cls.tax_group_iva).id,
            'country_id': cls.env.ref('base.mx').id,
            **values,
        })

    def _create_order_and_tx(self, lines, tx_amount=None, reference=None):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'pricelist_id': self.pricelist_mxn.id,
            'order_line': [Command.create(line) for line in lines],
        })
        tx = self._create_transaction(
            flow='redirect',
            amount=order.amount_total if tx_amount is None else tx_amount,
            sale_order_ids=[Command.set(order.ids)],
            reference=reference or self.reference,
        )
        return order, tx

    def _product_line(self, quantity=2.0, price=100.0, taxes=None, product=None, **values):
        """Return the values of an order line, taxed with IVA 16% unless `taxes` is given."""
        taxes = self.tax_16 if taxes is None else taxes
        return {
            'product_id': (product or self.product).id,
            'product_uom_qty': quantity,
            'price_unit': price,
            'tax_ids': [Command.set(taxes.ids)],
            **values,
        }

    def _get_itemized_order_values(self, tx):
        """Return the checkout order values, asserting that the sales order is itemized."""
        order_values = tx._orkestapay_prepare_checkout_payload()['order']
        self.assertEqual(order_values['products'][0]['product_id'], str(self.product.id))
        return order_values

    def _get_single_line_order_values(self, tx):
        """Return the checkout order values, asserting that the single-line fallback is used."""
        order_values = tx._orkestapay_prepare_checkout_payload()['order']
        self.assertEqual(len(order_values['products']), 1)
        self.assertEqual(order_values['products'][0]['product_id'], tx.reference)
        self.assertEqual(order_values['products'][0]['unit_price'], tx.amount)
        self.assertNotIn('taxes', order_values)
        return order_values

    def _assert_breakdown_adds_up(self, order_values):
        """Assert that subtotal + taxes - discounts matches the total within the tolerance."""
        breakdown_total = (
            order_values['subtotal_amount']
            + sum(tax['amount'] for tax in order_values.get('taxes', []))
            - sum(discount['amount'] for discount in order_values.get('discounts', []))
        )
        difference = abs(breakdown_total - order_values['total_amount'])
        self.assertLessEqual(
            float_compare(difference, const.BREAKDOWN_TOLERANCE, precision_digits=2), 0
        )

    def test_consistent_breakdown_is_itemized(self):
        order, tx = self._create_order_and_tx([
            self._product_line(quantity=2, price=100.0),
            self._product_line(quantity=1, price=-20.0, product=self.discount_product),
        ])
        self.assertEqual(order.amount_total, 208.8)  # (200 - 20) * 1.16
        order_values = tx._orkestapay_prepare_checkout_payload()['order']
        self.assertEqual(order_values['products'], [{
            'product_id': str(self.product.id),
            'name': "Widget",  # Without the internal reference, sent as `sku`.
            'quantity': 2,
            'unit_price': 100.0,
            'sku': 'WID-1',
        }])
        self.assertEqual(order_values['subtotal_amount'], 200.0)
        self.assertEqual(order_values['discounts'][0]['amount'], 20.0)
        self.assertEqual(len(order_values['taxes']), 1)
        self.assertEqual(order_values['taxes'][0]['amount'], 28.8)
        self.assertEqual(order_values['taxes'][0]['taxable_amount'], 180.0)
        self.assertEqual(order_values['taxes'][0]['rate'], 0.16)  # Fraction, not percentage.
        self.assertEqual(order_values['total_amount'], 208.8)
        self.assertEqual(order_values['metadata']['odoo_sale_order'], order.name)

    def test_tax_rate_outside_zero_one_falls_back_to_a_single_line(self):
        """Fixed taxes and withholdings would give a rate the checkout page rejects."""
        for name, amount_type, amount in (
            ("Fixed 50", 'fixed', 50.0),
            ("Withholding -10%", 'percent', -10.0),
        ):
            with self.subTest(tax=name):
                tax = self._create_tax(name, amount, amount_type=amount_type)
                _order, tx = self._create_order_and_tx(
                    [self._product_line(quantity=1, price=10.0, taxes=tax)],
                    reference=f'{self.reference}-{amount_type}-{amount}',
                )
                self._get_single_line_order_values(tx)

    def test_fractional_quantity_falls_back_to_a_single_line(self):
        _order, tx = self._create_order_and_tx([self._product_line(quantity=1.5)])
        self._get_single_line_order_values(tx)

    def test_inconsistent_breakdown_falls_back_to_a_single_line(self):
        # E.g., a partial payment: the transaction amount differs from the order total.
        _order, tx = self._create_order_and_tx([self._product_line()], tx_amount=100.0)
        order_values = self._get_single_line_order_values(tx)
        self.assertEqual(order_values['total_amount'], 100.0)
        self.assertEqual(order_values['subtotal_amount'], 100.0)

    def test_one_cent_difference_is_tolerated(self):
        _order, tx = self._create_order_and_tx(
            [self._product_line(taxes=self.env['account.tax'])], tx_amount=200.01
        )
        self._get_itemized_order_values(tx)

    def test_price_included_tax_sends_the_untaxed_unit_price(self):
        tax = self._create_tax("IVA 16% incl.", 16.0, price_include_override='tax_included')
        order, tx = self._create_order_and_tx(
            [self._product_line(quantity=1, price=116.0, taxes=tax)]
        )
        self.assertEqual(order.amount_total, 116.0)
        order_values = self._get_itemized_order_values(tx)
        self.assertEqual(order_values['products'][0]['unit_price'], 100.0)
        self.assertEqual(order_values['subtotal_amount'], 100.0)
        self.assertEqual(order_values['taxes'], [{
            'type': "IVA", 'rate': 0.16, 'amount': 16.0, 'taxable_amount': 100.0,
        }])

    def test_price_included_tax_with_a_discount_line(self):
        tax = self._create_tax("IVA 16% incl.", 16.0, price_include_override='tax_included')
        order, tx = self._create_order_and_tx([
            self._product_line(quantity=2, price=116.0, taxes=tax),
            self._product_line(quantity=1, price=-23.2, taxes=tax, product=self.discount_product),
        ])
        self.assertEqual(order.amount_total, 208.8)
        order_values = self._get_itemized_order_values(tx)
        self.assertEqual(order_values['subtotal_amount'], 200.0)
        self.assertEqual(order_values['discounts'][0]['amount'], 20.0)
        self.assertEqual(order_values['taxes'], [{
            'type': "IVA", 'rate': 0.16, 'amount': 28.8, 'taxable_amount': 180.0,
        }])
        self._assert_breakdown_adds_up(order_values)

    def test_price_included_tax_rounding_stays_within_the_tolerance(self):
        """3 x 9.99 tax included: the per-unit untaxed price is rounded, and the cent it may
        lose is within the tolerance."""
        tax = self._create_tax("IVA 16% incl.", 16.0, price_include_override='tax_included')
        order, tx = self._create_order_and_tx(
            [self._product_line(quantity=3, price=9.99, taxes=tax)]
        )
        self.assertEqual(order.amount_total, 29.97)
        order_values = self._get_itemized_order_values(tx)
        self.assertEqual(order_values['total_amount'], 29.97)
        self._assert_breakdown_adds_up(order_values)

    def test_unit_price_rounding_beyond_the_tolerance_falls_back_to_a_single_line(self):
        """7 units with a 33.33 % discount: 4.67 / 7 = 0.667 is sent as 0.67, and 7 x 0.67 = 4.69
        is 2 cents above the order total."""
        order, tx = self._create_order_and_tx([self._product_line(
            quantity=7, price=1.0, taxes=self.env['account.tax'], discount=33.33,
        )])
        self.assertEqual(order.amount_total, 4.67)
        order_values = self._get_single_line_order_values(tx)
        self.assertEqual(order_values['total_amount'], 4.67)

    def test_several_taxes_of_a_group_are_sent_as_one_tax(self):
        """Odoo totals taxes by group: the rate sent is the effective one (amount / base)."""
        other_tax = self._create_tax("IVA extra 8%", 8.0)
        _order, tx = self._create_order_and_tx(
            [self._product_line(quantity=1, taxes=self.tax_16 | other_tax)]
        )
        order_values = self._get_itemized_order_values(tx)
        self.assertEqual(order_values['taxes'], [{
            'type': "IVA", 'rate': 0.24, 'amount': 24.0, 'taxable_amount': 100.0,
        }])
        self.assertEqual(order_values['total_amount'], 124.0)

    def test_mixed_rates_of_a_group_are_sent_with_the_effective_rate(self):
        """IVA 16% and IVA 0% lines usually share the IVA group: one tax with the effective rate
        is sent, and the amounts still add up."""
        exempt = self._create_tax("IVA 0%", 0.0)
        _order, tx = self._create_order_and_tx([
            self._product_line(quantity=1),
            self._product_line(quantity=1, taxes=exempt),
        ])
        order_values = self._get_itemized_order_values(tx)
        self.assertEqual(order_values['taxes'], [{
            'type': "IVA", 'rate': 0.08, 'amount': 16.0, 'taxable_amount': 200.0,
        }])
        self.assertEqual(order_values['total_amount'], 216.0)
        self._assert_breakdown_adds_up(order_values)

    def test_fixed_tax_in_a_percent_group_is_sent_with_the_effective_rate(self):
        fixed = self._create_tax("Fixed 5", 5.0, amount_type='fixed')
        _order, tx = self._create_order_and_tx(
            [self._product_line(quantity=1, taxes=self.tax_16 | fixed)]
        )
        order_values = self._get_itemized_order_values(tx)
        self.assertEqual(order_values['taxes'], [{
            'type': "IVA", 'rate': 0.21, 'amount': 21.0, 'taxable_amount': 100.0,
        }])
        self._assert_breakdown_adds_up(order_values)

    def test_taxes_of_different_groups_are_sent_separately(self):
        ieps = self._create_tax("IEPS 8%", 8.0, tax_group=self._create_tax_group("IEPS"))
        _order, tx = self._create_order_and_tx(
            [self._product_line(quantity=1, taxes=self.tax_16 | ieps)]
        )
        order_values = self._get_itemized_order_values(tx)
        self.assertCountEqual(order_values['taxes'], [
            {'type': "IVA", 'rate': 0.16, 'amount': 16.0, 'taxable_amount': 100.0},
            {'type': "IEPS", 'rate': 0.08, 'amount': 8.0, 'taxable_amount': 100.0},
        ])
        self.assertEqual(order_values['total_amount'], 124.0)

    def test_zero_rate_tax_is_omitted(self):
        """An exempt (0 %) tax adds nothing: the order is itemized without taxes."""
        exempt = self._create_tax("IVA 0%", 0.0)
        _order, tx = self._create_order_and_tx([self._product_line(quantity=1, taxes=exempt)])
        order_values = self._get_itemized_order_values(tx)
        self.assertNotIn('taxes', order_values)
        self.assertEqual(order_values['subtotal_amount'], 100.0)
        self.assertEqual(order_values['total_amount'], 100.0)

    def test_tax_on_a_zero_base_falls_back_to_a_single_line(self):
        """A fixed tax on a free product, alone in its group, has no rate (zero base)."""
        fixed = self._create_tax(
            "Fixed 5", 5.0, amount_type='fixed', tax_group=self._create_tax_group("Fixed")
        )
        order, tx = self._create_order_and_tx([
            self._product_line(quantity=1),
            self._product_line(quantity=1, price=0.0, taxes=fixed),
        ])
        self.assertEqual(order.amount_total, 121.0)
        self._get_single_line_order_values(tx)

    def test_fallback_omits_optional_buyer_data(self):
        self.partner.write({'country_id': self.env.ref('base.mx').id, 'phone': '+52 55 1234 5678'})
        _order, tx = self._create_order_and_tx([self._product_line()])
        self.assertIn('phone', tx._orkestapay_prepare_checkout_payload()['order']['customer'])
        fallback = tx._orkestapay_prepare_checkout_payload(single_line=True)['order']
        self.assertNotIn('phone', fallback['customer'])
        self.assertNotIn('shipping_address', fallback)
        self.assertNotIn('billing_address', fallback)


@tagged('post_install', '-at_install')
class TestCheckoutCreation(OrkestaPayCommon):

    def _checkout_response(self, redirect_url='https://checkout.sand.orkestapay.com/chk_1?h=abc'):
        return {
            'checkout_id': 'chk_00000000000000000000000000000004',
            'checkout_redirect_url': redirect_url,
            'order': {'order_id': self.order_id, 'status': 'CREATED'},
        }

    def test_rendering_values_create_the_checkout(self):
        tx = self._create_transaction(flow='redirect')
        with self._mock_api({'POST /v1/checkouts': self._checkout_response()}) as calls:
            processing_values = tx._get_processing_values()
        self.assertEqual(len(calls), 1)
        self.assertEqual(tx.provider_reference, self.order_id)
        self.assertEqual(tx.orkestapay_checkout_id, 'chk_00000000000000000000000000000004')
        self.assertEqual(tx.state, 'draft')  # Draft until verified against the API.
        form = self._extract_values_from_html_form(processing_values['redirect_form_html'])
        self.assertEqual(form['method'], 'get')
        self.assertEqual(form['action'], 'https://checkout.sand.orkestapay.com/chk_1?h=abc')
        self.assertEqual(form['inputs'], {'h': 'abc'})

    @mute_logger(TX_LOGGER)
    def test_breakdown_rejected_with_400_is_retried_with_a_single_line(self):
        tx = self._create_transaction(flow='redirect')
        itemized_payload = tx._orkestapay_prepare_checkout_payload()
        itemized_payload['order']['taxes'] = [{'type': 'IVA', 'rate': 16, 'amount': 1}]
        original_prepare = type(tx)._orkestapay_prepare_checkout_payload

        def prepare(tx_self, single_line=False):
            return original_prepare(tx_self, single_line=True) if single_line else itemized_payload

        with patch.object(
            type(tx), '_orkestapay_prepare_checkout_payload', autospec=True, side_effect=prepare
        ), self._mock_api({
            'POST /v1/checkouts': [self._api_error(400), self._checkout_response()],
        }) as calls:
            checkout = tx._orkestapay_create_checkout()
        self.assertTrue(checkout)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('taxes', calls[1][2]['json']['order'])
        self.assertEqual(calls[1][2]['json']['order']['products'][0]['product_id'], tx.reference)

    @mute_logger(TX_LOGGER)
    def test_single_line_rejected_with_400_is_not_retried(self):
        tx = self._create_transaction(flow='redirect')
        with self._mock_api({'POST /v1/checkouts': self._api_error(400)}) as calls:
            processing_values = tx._get_processing_values()
        self.assertEqual(len(calls), 1)
        self.assertEqual(tx.state, 'error')
        self.assertEqual(processing_values['state'], 'error')
        self.assertTrue(processing_values['state_message'])

    @mute_logger(TX_LOGGER)
    def test_checkout_timeout_sets_the_transaction_in_error(self):
        tx = self._create_transaction(flow='redirect')
        with self._mock_api({
            'POST /v1/checkouts': self._api_error(is_network_error=True),
        }) as calls:
            tx._get_processing_values()
        self.assertEqual(len(calls), 1)
        self.assertEqual(tx.state, 'error')
        self.assertFalse(tx.provider_reference)


@tagged('post_install', '-at_install')
class TestStateMapping(OrkestaPayCommon):

    def _process_with_api(self, tx, **routes_kwargs):
        with self._mock_api(self._verification_routes(**routes_kwargs)):
            tx._orkestapay_process_from_api('return')

    def test_order_status_mapping(self):
        for order_status, expected_state in (
            ('COMPLETED', 'done'),
            ('CREATED', 'pending'),
            ('PROCESSING_PAYMENT', 'pending'),
            ('PAYMENT_ACTION_REQUIRED', 'pending'),
            ('PARTIALLY_PAID', 'pending'),
            ('CANCELED', 'cancel'),
            ('EXPIRED', 'cancel'),
            ('FAILED', 'error'),
        ):
            with self.subTest(order_status=order_status), mute_logger(TX_LOGGER):
                tx = self._create_orkestapay_transaction(
                    reference=f'{self.reference}-{order_status}'
                )
                self._process_with_api(
                    tx, order_status=order_status, reference=tx.reference,
                    payments=[self._payment_response(
                        'REJECTED' if order_status == 'FAILED' else 'COMPLETED',
                        captured=self.amount, message='Insufficient funds',
                    )] if order_status in ('COMPLETED', 'FAILED') else [],
                )
                self.assertEqual(tx.state, expected_state)
                if order_status == 'FAILED':
                    self.assertEqual(tx.state_message, 'Insufficient funds')

    def test_completed_payment_saves_the_card_details(self):
        tx = self._create_orkestapay_transaction()
        self._process_with_api(tx, order_status='COMPLETED')
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.orkestapay_payment_id, self.payment_id)
        self.assertEqual(tx.orkestapay_card_brand, 'MASTERCARD')
        self.assertEqual(tx.orkestapay_card_last_four, '8785')

    def test_card_details_are_fetched_when_the_payment_list_omits_them(self):
        """The order's payment list has no `payment_source.card` (seen in sandbox)."""
        tx = self._create_orkestapay_transaction()
        routes = self._verification_routes(
            'COMPLETED', payments=[self._payment_response(captured=self.amount, with_card=False)],
        )
        routes[f'GET /v1/payments/{self.payment_id}'] = self._payment_response(
            captured=self.amount
        )
        with self._mock_api(routes) as calls:
            tx._orkestapay_process_from_api('return')
        self.assertEqual(len(calls), 3)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.orkestapay_card_brand, 'MASTERCARD')
        self.assertEqual(tx.orkestapay_card_last_four, '8785')

    @mute_logger(TX_LOGGER)
    def test_card_details_failure_does_not_block_the_payment(self):
        tx = self._create_orkestapay_transaction()
        routes = self._verification_routes(
            'COMPLETED', payments=[self._payment_response(captured=self.amount, with_card=False)],
        )
        routes[f'GET /v1/payments/{self.payment_id}'] = self._api_error(is_network_error=True)
        with self._mock_api(routes):
            tx._orkestapay_process_from_api('return')
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.orkestapay_payment_id, self.payment_id)
        self.assertFalse(tx.orkestapay_card_brand)

    @mute_logger(TX_LOGGER)
    def test_authorized_payment_sets_the_transaction_in_error(self):
        tx = self._create_orkestapay_transaction()
        self._process_with_api(
            tx, order_status='PROCESSING_PAYMENT',
            payments=[self._payment_response('AUTHORIZED')],
        )
        self.assertEqual(tx.state, 'error')
        self.assertIn('capture', tx.state_message)

    @mute_logger('odoo.addons.payment.models.payment_transaction')
    def test_captured_amount_mismatch_prevents_done(self):
        tx = self._create_orkestapay_transaction()
        self._process_with_api(
            tx, order_status='COMPLETED', payments=[self._payment_response(captured=900.0)],
        )
        self.assertNotEqual(tx.state, 'done')
        self.assertEqual(tx.state, 'error')

    @mute_logger('odoo.addons.payment.models.payment_transaction')
    def test_order_amount_mismatch_prevents_done(self):
        tx = self._create_orkestapay_transaction()
        self._process_with_api(tx, order_status='COMPLETED', amount=1.0, payments=[])
        self.assertEqual(tx.state, 'error')

    @mute_logger('odoo.addons.payment.models.payment_transaction')
    def test_currency_mismatch_prevents_done(self):
        tx = self._create_orkestapay_transaction()
        self._process_with_api(tx, order_status='COMPLETED', currency='USD')
        self.assertEqual(tx.state, 'error')

    @mute_logger(TX_LOGGER)
    def test_order_of_another_transaction_is_rejected(self):
        tx = self._create_orkestapay_transaction()
        with self._mock_api(self._verification_routes(reference='S99999-1')), \
                self.assertRaises(ValidationError):
            tx._orkestapay_process_from_api('return')
        self.assertEqual(tx.state, 'draft')

    def test_processing_twice_is_idempotent(self):
        tx = self._create_orkestapay_transaction()
        self._process_with_api(tx, order_status='COMPLETED')
        last_state_change = tx.last_state_change
        self._process_with_api(tx, order_status='COMPLETED')
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.last_state_change, last_state_change)


@tagged('post_install', '-at_install')
class TestBuyerCancellation(OrkestaPayCommon):
    """The buyer cancels the checkout."""

    def _cancel_route(self):
        return f'POST /v1/orders/{self.order_id}/cancel'

    def test_cancel_cancels_the_order_in_the_api(self):
        tx = self._create_orkestapay_transaction()
        routes = self._verification_routes('CREATED')
        routes[f'GET /v1/orders/{self.order_id}'] = [
            self._order_response('CREATED'), self._order_response('CANCELED'),
        ]
        routes[self._cancel_route()] = {'order_id': self.order_id, 'status': 'CANCELED'}
        with self._mock_api(routes) as calls:
            tx._orkestapay_cancel_from_buyer()
        self.assertEqual(tx.state, 'cancel')
        cancel_calls = [c for c in calls if c[1].endswith('/cancel')]
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0][2]['idempotency_key'], f'{tx.reference}-cancel')

    def test_cancel_of_a_completed_order_marks_it_paid(self):
        tx = self._create_orkestapay_transaction()
        with self._mock_api(self._verification_routes('COMPLETED')) as calls:
            tx._orkestapay_cancel_from_buyer()
        self.assertEqual(tx.state, 'done')
        self.assertFalse([c for c in calls if c[1].endswith('/cancel')])

    @mute_logger(TX_LOGGER)
    def test_cancel_refused_with_400_because_paid_marks_it_paid(self):
        tx = self._create_orkestapay_transaction()
        routes = self._verification_routes('CREATED')
        routes[f'GET /v1/orders/{self.order_id}'] = [
            self._order_response('CREATED'), self._order_response('COMPLETED'),
        ]
        routes[f'GET /v1/orders/{self.order_id}/payments'] = [
            {'content': []}, {'content': [self._payment_response(captured=self.amount)]},
        ]
        routes[self._cancel_route()] = self._api_error(400)
        with self._mock_api(routes):
            tx._orkestapay_cancel_from_buyer()
        self.assertEqual(tx.state, 'done')

    @mute_logger(TX_LOGGER)
    def test_cancel_timeout_leaves_the_transaction_pending(self):
        tx = self._create_orkestapay_transaction()
        routes = self._verification_routes('CREATED')
        routes[self._cancel_route()] = self._api_error(is_network_error=True)
        with self._mock_api(routes) as calls:
            tx._orkestapay_cancel_from_buyer()
        self.assertEqual(tx.state, 'pending')
        self.assertEqual(len([c for c in calls if c[1] == f'/v1/orders/{self.order_id}']), 1)

    @mute_logger(TX_LOGGER)
    def test_cancel_5xx_leaves_the_transaction_pending(self):
        tx = self._create_orkestapay_transaction()
        routes = self._verification_routes('CREATED')
        routes[self._cancel_route()] = self._api_error(503)
        with self._mock_api(routes):
            tx._orkestapay_cancel_from_buyer()
        self.assertEqual(tx.state, 'pending')

    def test_cancel_of_an_expired_order_does_not_call_cancel(self):
        tx = self._create_orkestapay_transaction()
        with self._mock_api(self._verification_routes('EXPIRED')) as calls:
            tx._orkestapay_cancel_from_buyer()
        self.assertEqual(tx.state, 'cancel')
        self.assertFalse([c for c in calls if c[1].endswith('/cancel')])
