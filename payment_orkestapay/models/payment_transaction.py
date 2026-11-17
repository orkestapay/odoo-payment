# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

import re
import time
from datetime import timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from odoo import api, fields, models
from odoo.exceptions import LockError, ValidationError
from odoo.http import request
from odoo.modules.module import get_manifest
from odoo.tools import float_compare, float_is_zero, float_round
from odoo.tools.urls import urljoin

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.logging import get_payment_logger
from odoo.addons.payment_orkestapay import const
from odoo.addons.payment_orkestapay.utils import OrkestaPayApiError


_logger = get_payment_logger(__name__, sensitive_keys=const.SENSITIVE_KEYS)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    orkestapay_checkout_id = fields.Char(
        string="OrkestaPay Checkout ID", readonly=True, copy=False
    )
    orkestapay_payment_id = fields.Char(string="OrkestaPay Payment ID", readonly=True, copy=False)
    orkestapay_card_brand = fields.Char(string="Card Brand", readonly=True, copy=False)
    orkestapay_card_last_four = fields.Char(
        string="Card Last 4 Digits", readonly=True, copy=False
    )
    # Ids of the refunds made outside Odoo (e.g., in the OrkestaPay portal) that were already noted
    # on the linked documents, one per line, so that webhook retries don't repeat the note.
    orkestapay_external_refund_ids = fields.Text(
        string="OrkestaPay External Refund IDs", readonly=True, copy=False
    )
    # Last time the reconciliation cron queried the transaction; the cron serves the least
    # recently queried ones first so that unpaid checkouts don't monopolize its batches.
    orkestapay_last_reconcile_date = fields.Datetime(
        string="OrkestaPay Last Reconciliation", readonly=True, copy=False
    )

    # === BUSINESS METHODS - PAYMENT FLOW === #

    def _get_specific_rendering_values(self, processing_values):
        """Override of `payment` to create the checkout and return the redirect form values.

        Note: `self.ensure_one()` from `_get_processing_values`.

        :param dict processing_values: The generic and specific processing values of the
                                       transaction.
        :return: The dict of provider-specific rendering values.
        :rtype: dict
        """
        if self.provider_code != 'orkestapay':
            return super()._get_specific_rendering_values(processing_values)

        checkout = self._orkestapay_create_checkout()
        if not checkout:  # The transaction is in error; the payment form displays the message.
            return {'api_url': '', 'url_params': {}}

        api_url = checkout['checkout_redirect_url']
        # A GET form replaces the query string of its action: send the params as inputs.
        url_params = dict(parse_qsl(urlsplit(api_url).query, keep_blank_values=True))
        return {'api_url': api_url, 'url_params': url_params}

    def _orkestapay_create_checkout(self):
        """Create the OrkestaPay checkout of the transaction.

        If the API rejects the itemized breakdown with a 400, the checkout is requested once more
        with the single-line fallback. On success, the order and checkout ids are saved on the
        transaction, which stays in `draft` until the payment is verified against the API.

        Note: `self.ensure_one()`

        :return: The checkout data, or None if the checkout could not be created.
        :rtype: dict|None
        """
        self.ensure_one()
        payload = self._orkestapay_prepare_checkout_payload()
        try:
            checkout = self._send_api_request('POST', const.CHECKOUTS_ENDPOINT, json=payload)
        except OrkestaPayApiError as error:
            checkout = None
            fallback_payload = self._orkestapay_prepare_checkout_payload(single_line=True)
            if error.status_code == 400 and fallback_payload != payload:
                _logger.warning(
                    "OrkestaPay rejected the checkout breakdown of transaction %s; retrying with a"
                    " single line.", self.reference,
                )
                try:
                    checkout = self._send_api_request(
                        'POST', const.CHECKOUTS_ENDPOINT, json=fallback_payload
                    )
                except OrkestaPayApiError:
                    checkout = None

        order_id = (checkout or {}).get('order', {}).get('order_id')
        if not (checkout and checkout.get('checkout_redirect_url') and order_id):
            _logger.error(
                "Could not create the OrkestaPay checkout of transaction %s.", self.reference
            )
            self._set_error(self.env._(
                "We could not start the payment with OrkestaPay. Please try again or choose"
                " another payment method."
            ))
            return None

        self.write({
            'provider_reference': order_id,
            'orkestapay_checkout_id': checkout.get('checkout_id'),
        })
        return checkout

    def _orkestapay_prepare_checkout_payload(self, single_line=False):
        """Build the `POST /v1/checkouts` payload.

        The order is itemized from the linked sales order when its breakdown adds up to the
        transaction amount; otherwise, or with `single_line`, a single product line for the total
        is sent, without taxes, discounts, shipping, phone or addresses, so that it always passes
        the API validation.

        Note: `self.ensure_one()`

        :param bool single_line: Whether to force the single-line fallback.
        :return: The checkout payload.
        :rtype: dict
        """
        self.ensure_one()
        total_amount = self._orkestapay_format_amount(self.amount)
        breakdown = None if single_line else self._orkestapay_get_order_breakdown()

        order_values = {
            'merchant_order_id': self.reference,
            'currency': self.currency_id.name,
            'country_code': self._orkestapay_get_country_code(),
            'total_amount': total_amount,
        }
        if breakdown:
            order_values.update(breakdown)
        else:
            order_values.update({
                'subtotal_amount': total_amount,
                'products': [{
                    'product_id': self.reference,
                    'name': self.env._("Order %s", self.reference),
                    'quantity': 1,
                    'unit_price': total_amount,
                }],
            })
        order_values['customer'] = self._orkestapay_prepare_customer(with_phone=bool(breakdown))
        if breakdown:
            order_values.update(self._orkestapay_prepare_addresses())
        order_values['metadata'] = self._orkestapay_prepare_metadata()

        base_url = self.provider_id.get_base_url()
        return_query = urlencode({
            'reference': self.reference,
            'access_token': payment_utils.generate_access_token(self.reference, env=self.env),
        })
        return {
            'completed_redirect_url': (
                f'{urljoin(base_url, const.PAYMENT_RETURN_ROUTE)}?{return_query}'
            ),
            'canceled_redirect_url': (
                f'{urljoin(base_url, const.PAYMENT_CANCEL_ROUTE)}?{return_query}'
            ),
            'allow_save_payment_methods': False,
            'locale': self._orkestapay_get_locale(),
            'order': order_values,
        }

    def _orkestapay_format_amount(self, amount, currency=None):
        """Format an amount for the OrkestaPay API: major units rounded to the currency decimals
        (e.g., `990.5`), not cents. Every amount sent to the API must go through this method.

        :param float amount: The amount to format.
        :param res.currency currency: The currency; defaults to the transaction currency.
        :return: The formatted amount.
        :rtype: float
        """
        currency = currency or self.currency_id
        return currency.round(amount)

    def _orkestapay_get_order_breakdown(self):
        """Return the itemized breakdown of the linked sales order, if it is consistent.

        The breakdown is used only if the transaction pays exactly one sales order in the same
        currency, all quantities are integers, and `subtotal + taxes + shipping - discounts` equals
        the transaction amount within `BREAKDOWN_TOLERANCE`.

        Note: `self.ensure_one()`

        :return: The `products`, `subtotal_amount`, `taxes`, `discounts` and `shipping_details`
                 order values, or None to use the single-line fallback.
        :rtype: dict|None
        """
        self.ensure_one()
        if 'sale_order_ids' not in self._fields or len(self.sale_order_ids) != 1:
            return None
        order = self.sale_order_ids
        currency = self.currency_id
        if order.currency_id != currency:
            return None

        products, discounts = [], []
        shipping_amount = 0.0
        for line in order.order_line:
            if line.display_type:
                continue  # Section or note.
            if getattr(line, 'is_downpayment', False):
                return None
            line_subtotal = line.price_subtotal
            if getattr(line, 'is_delivery', False):
                shipping_amount += line_subtotal
                continue
            if currency.compare_amounts(line_subtotal, 0) < 0:  # Discount or reward line.
                discount = {'amount': self._orkestapay_format_amount(-line_subtotal)}
                if line.name:
                    discount['description'] = line.name.splitlines()[0][:255]
                discounts.append(discount)
                continue
            quantity = line.product_uom_qty
            if float_is_zero(quantity, precision_digits=6):
                continue
            if not float_is_zero(quantity - round(quantity), precision_digits=6):
                return None  # The API only accepts integer quantities.
            quantity = int(round(quantity))
            product = line.product_id
            product_values = {
                'product_id': str(product.id),
                # Without the internal reference, already sent as `sku`.
                'name': (
                    product.with_context(display_default_code=False).display_name
                    or line.name or ''
                )[:255],
                'quantity': quantity,
                'unit_price': self._orkestapay_format_amount(line_subtotal / quantity),
            }
            if product.default_code:
                product_values['sku'] = product.default_code
            products.append(product_values)

        if not products:
            return None

        subtotal_amount = self._orkestapay_format_amount(
            sum(p['quantity'] * p['unit_price'] for p in products)
        )
        taxes = self._orkestapay_get_order_taxes(order)
        if any(tax['rate'] is None or not 0 <= tax['rate'] <= 1 for tax in taxes):
            # The checkout page rejects such rates although the API accepts them.
            _logger.info(
                "Transaction %s has a tax rate outside [0, 1]; using a single line.",
                self.reference,
            )
            return None
        shipping_amount = self._orkestapay_format_amount(shipping_amount)
        breakdown_total = (
            subtotal_amount
            + sum(tax['amount'] for tax in taxes)
            + shipping_amount
            - sum(discount['amount'] for discount in discounts)
        )
        difference = abs(breakdown_total - self._orkestapay_format_amount(self.amount))
        if float_compare(
            difference, const.BREAKDOWN_TOLERANCE, precision_digits=currency.decimal_places
        ) > 0:
            _logger.info(
                "The breakdown of transaction %s does not add up to its amount (difference: %s);"
                " using a single line.", self.reference, difference,
            )
            return None

        breakdown = {
            'subtotal_amount': subtotal_amount,
            'products': products,
        }
        if taxes:
            breakdown['taxes'] = taxes
        if discounts:
            breakdown['discounts'] = discounts
        if not currency.is_zero(shipping_amount):
            breakdown['shipping_details'] = {'amount': shipping_amount}
        return breakdown

    def _orkestapay_get_order_taxes(self, order):
        """Aggregate the taxes of a sales order by tax group.

        :param sale.order order: The sales order.
        :return: The list of `{type, rate, amount, taxable_amount}` values.
        :rtype: list[dict]
        """
        taxes = []
        for subtotal in (order.tax_totals or {}).get('subtotals', []):
            for tax_group in subtotal.get('tax_groups', []):
                amount = self._orkestapay_format_amount(
                    tax_group.get('tax_amount_currency') or 0.0
                )
                base = self._orkestapay_format_amount(
                    tax_group.get('base_amount_currency') or 0.0
                )
                if self.currency_id.is_zero(amount):
                    continue
                involved_taxes = self.env['account.tax'].browse(
                    tax_group.get('involved_tax_ids') or []
                ).exists()
                if len(involved_taxes) == 1 and involved_taxes.amount_type == 'percent':
                    rate = involved_taxes.amount / 100
                else:
                    rate = amount / base if base else None
                # The checkout requires a fraction between 0 and 1 (e.g., 0.16 for 16 %); other
                # values (fixed taxes, withholdings, zero base) are left to the caller to reject.
                rate = float_round(rate, precision_digits=6) if rate is not None else None
                taxes.append({
                    'type': tax_group.get('group_name') or self.env._("Tax"),
                    'rate': rate,
                    'amount': amount,
                    'taxable_amount': base,
                })
        return taxes

    def _orkestapay_prepare_customer(self, with_phone=True):
        """Return the `order.customer` values of the checkout payload.

        :param bool with_phone: Whether to include the phone number.
        :rtype: dict
        """
        first_name, last_name = payment_utils.split_partner_name(self.partner_name or '')
        customer = {
            'first_name': first_name,
            'last_name': last_name,
            'email': self.partner_email,
            'merchant_customer_id': str(self.partner_id.id),
        }
        if with_phone and (phone := self._orkestapay_format_phone()):
            customer['phone'] = phone
        return {key: value for key, value in customer.items() if value}

    def _orkestapay_format_phone(self):
        """Return the buyer's phone as `{number, country_code}`, or None if it cannot be split.

        :rtype: dict|None
        """
        raw_phone = (self.partner_phone or '').strip()
        digits = re.sub(r'\D', '', raw_phone)
        country_code = str(self.partner_country_id.phone_code or '')
        if not digits or not country_code:
            return None
        if raw_phone.startswith(('+', '00')):  # International format: strip the country code.
            if raw_phone.startswith('00'):
                digits = digits[2:]
            if not digits.startswith(country_code):
                return None  # International number from another country than the partner's.
            digits = digits[len(country_code):]
        return {'number': digits, 'country_code': country_code} if digits else None

    def _orkestapay_prepare_addresses(self):
        """Return the `shipping_address` and `billing_address` values, when complete.

        :rtype: dict
        """
        if 'sale_order_ids' in self._fields and len(self.sale_order_ids) == 1:
            shipping_partner = self.sale_order_ids.partner_shipping_id
            billing_partner = self.sale_order_ids.partner_invoice_id
        else:
            shipping_partner = self.env['res.partner']
            billing_partner = self.partner_id
        addresses = {}
        if shipping_address := self._orkestapay_prepare_address(shipping_partner):
            addresses['shipping_address'] = shipping_address
        if billing_address := self._orkestapay_prepare_address(billing_partner):
            addresses['billing_address'] = billing_address
        return addresses

    @api.model
    def _orkestapay_prepare_address(self, partner):
        """Return the address values of a partner, or None if a required field is missing.

        :param res.partner partner: The partner.
        :rtype: dict|None
        """
        if not partner:
            return None
        address = {
            'line_1': partner.street,
            'line_2': partner.street2,
            'city': partner.city,
            'state': partner.state_id.name,
            'country': partner.country_id.code,
            'zip_code': partner.zip,
        }
        if not all(address[key] for key in ('line_1', 'city', 'state', 'country', 'zip_code')):
            return None
        return {key: value for key, value in address.items() if value}

    def _orkestapay_prepare_metadata(self):
        """Return the `order.metadata` values of the checkout payload.

        :rtype: dict
        """
        metadata = {
            'odoo_tx_id': str(self.id),
            'odoo_db_uuid': self.env['ir.config_parameter'].sudo().get_param('database.uuid'),
            'module_version': get_manifest('payment_orkestapay').get('version', ''),
        }
        if 'sale_order_ids' in self._fields and self.sale_order_ids:
            metadata['odoo_sale_order'] = ', '.join(self.sale_order_ids.mapped('name'))
        return {key: value for key, value in metadata.items() if value}

    def _orkestapay_get_country_code(self):
        """Return the ISO country code of the order: the buyer's, or the company's."""
        return (
            (self.partner_country_id or self.company_id.country_id).code
            or const.DEFAULT_COUNTRY_CODE
        )

    def _orkestapay_get_locale(self):
        """Return the checkout locale: Spanish for Spanish-speaking buyers, English otherwise.

        The language the buyer is browsing the website in comes first: a returning buyer's
        contact may keep another language. It is read from the `frontend_lang` cookie that the
        website sets, because the payment link and portal transaction routes are not website
        routes and their request language may be the public user's. Without that cookie (no
        request, or no `website` module), the language of the buyer's contact is used. The cookie
        is set by the visitor, but it only chooses between the Spanish and English checkout.
        """
        browsing_lang = request and request.cookies.get('frontend_lang')
        lang = browsing_lang or self.partner_lang or self.partner_id.lang or ''
        return const.LOCALE_SPANISH if lang.startswith('es') else const.LOCALE_DEFAULT

    # === BUSINESS METHODS - VERIFICATION === #

    def _orkestapay_process_from_api(self, source):
        """Lock the transaction, fetch its state from the API and process it.

        This is the single processing path of the buyer return, the webhook and the cron: the
        state never comes from the webhook body or the URL parameters, and the row lock
        serializes a return and a webhook that arrive at the same time.

        Note: `self.ensure_one()`

        :param str source: `return`, `webhook` or `cron`.
        :return: None
        :raise LockError: If another process holds the transaction.
        :raise ValidationError: If the API cannot be queried or returns another order.
        """
        self.ensure_one()
        self.lock_for_update()
        self._process('orkestapay', self._orkestapay_fetch_payment_data(source))

    def _orkestapay_cancel_from_buyer(self):
        """Handle the buyer's return through the cancel URL.

        The order is canceled in OrkestaPay before the transaction is canceled in Odoo, so that the
        checkout can no longer be paid once the transaction is `cancel`.

        Note: `self.ensure_one()`

        :return: None
        :raise LockError: If another process holds the transaction.
        :raise ValidationError: If the first API query fails.
        """
        self.ensure_one()
        self.lock_for_update()
        payment_data = self._orkestapay_fetch_payment_data('cancel')
        self._process('orkestapay', payment_data)  # A non-final order moves the tx to `pending`.
        if payment_data.get('order_status') in const.ORDER_FINAL_STATUSES:
            return  # Already paid, canceled, expired or failed: the mapping was applied.
        if self.state != 'pending':
            return  # E.g., an unexpected AUTHORIZED payment moved the transaction to `error`.

        order_id = quote(self.provider_reference, safe='')
        try:
            self._send_api_request(
                'POST',
                const.ORDER_CANCEL_ENDPOINT.format(order_id=order_id),
                json={'description': self.env._("Canceled by the buyer in Odoo")},
                idempotency_key=f'{self.reference}-cancel',
            )
        except OrkestaPayApiError as error:
            if error.status_code != 400:  # Timeout, 5xx or unexpected error: outcome unknown.
                _logger.warning(
                    "Could not cancel the OrkestaPay order of transaction %s; it stays pending"
                    " until the reconciliation resolves it.", self.reference,
                )
                return
            # 400: the order may have been paid in the meantime; the new query tells.
            _logger.info(
                "OrkestaPay refused to cancel the order of transaction %s; checking its status.",
                self.reference,
            )

        try:
            payment_data = self._orkestapay_fetch_payment_data('cancel')
        except ValidationError:
            _logger.warning(
                "Could not verify the OrkestaPay order of transaction %s after canceling it.",
                self.reference,
            )
            return
        self._process('orkestapay', payment_data)

    def _orkestapay_fetch_payment_data(self, source):
        """Query the order and its payments and return the normalized payment data.

        Note: `self.ensure_one()`

        :param str source: `return`, `cancel`, `webhook` or `cron`.
        :return: The normalized payment data.
        :rtype: dict
        :raise ValidationError: If the API cannot be queried or returns another order.
        """
        self.ensure_one()
        if not self.provider_reference:
            raise ValidationError(
                self.env._("The transaction %s has no OrkestaPay order.", self.reference)
            )
        order_id = quote(self.provider_reference, safe='')
        order = self._send_api_request('GET', const.ORDER_ENDPOINT.format(order_id=order_id))
        if order.get('merchant_order_id') != self.reference:
            _logger.error(
                "The OrkestaPay order %s does not belong to transaction %s.",
                self.provider_reference, self.reference,
            )
            raise ValidationError(
                self.env._("The OrkestaPay order does not match the transaction.")
            )
        payments_page = self._send_api_request(
            'GET',
            const.ORDER_PAYMENTS_ENDPOINT.format(order_id=order_id),
            params={'page': 0, 'size': 50},
        )
        payments = (payments_page or {}).get('content') or []
        if (
            order.get('status') in const.ORDER_STATUS_MAPPING['done']
            and not self.orkestapay_card_brand  # Already fetched by a previous notification.
        ):
            payments = self._orkestapay_add_card_details(payments)
        return self._orkestapay_normalize_payment_data(order, payments, source)

    def _orkestapay_add_card_details(self, payments):
        """Complete the selected payment with its card data, which the order's payment list omits.

        `GET /v1/orders/{order_id}/payments` does not return `payment_source.card`; the payment
        detail does. The card brand and last 4 digits are informative only, so a failure to fetch
        them is logged and does not prevent processing the payment.

        Note: `self.ensure_one()`

        :param list payments: The `PaymentResponse` list of the order.
        :return: The payments, with the selected one replaced by its detail when available.
        :rtype: list
        """
        self.ensure_one()
        payment = self._orkestapay_select_payment(payments)
        if not payment or not payment.get('payment_id'):
            return payments
        if (payment.get('payment_source') or {}).get('card'):
            return payments
        try:
            payment_detail = self._send_api_request(
                'GET',
                const.PAYMENT_ENDPOINT.format(
                    payment_id=quote(str(payment['payment_id']), safe='')
                ),
            )
        except ValidationError:
            _logger.warning(
                "Could not fetch the card details of transaction %s.", self.reference
            )
            return payments
        if not ((payment_detail or {}).get('payment_source') or {}).get('card'):
            return payments
        # `_orkestapay_select_payment` returns an element of `payments` (not a copy).
        enriched_payment = dict(payment, payment_source=payment_detail['payment_source'])
        return [enriched_payment if p is payment else p for p in payments]

    @api.model
    def _orkestapay_normalize_payment_data(self, order, payments, source):
        """Build the normalized payment data from the API order and payments.

        :param dict order: The `OrderDetail` returned by the API.
        :param list payments: The `PaymentResponse` list of the order.
        :param str source: `return`, `cancel`, `webhook` or `cron`.
        :rtype: dict
        """
        order_status = order.get('status')
        amount = order.get('total_amount')
        if order_status in const.ORDER_STATUS_MAPPING['done']:
            # Validate the amount actually captured when the API reports it.
            captured_amounts = [
                payment['amount']['captured'] for payment in payments
                if payment.get('status') in const.PAYMENT_STATUSES_CAPTURED
                and isinstance(payment.get('amount'), dict)
                and payment['amount'].get('captured') is not None
            ]
            if captured_amounts:
                amount = sum(float(captured) for captured in captured_amounts)
        payment = self._orkestapay_select_payment(payments)
        return {
            'reference': order.get('merchant_order_id'),
            'order_id': order.get('order_id'),
            'order_status': order_status,
            'amount': amount,
            'currency': order.get('currency'),
            'payment': self._orkestapay_normalize_payment(payment) if payment else None,
            'source': source,
        }

    @api.model
    def _orkestapay_select_payment(self, payments):
        """Return the most relevant payment: completed, else authorized, else the latest.

        :param list payments: The `PaymentResponse` list of the order.
        :rtype: dict|None
        """
        def created_at(payment):
            return int(payment.get('created_at') or 0)

        for status in (const.PAYMENT_STATUS_COMPLETED, const.PAYMENT_STATUS_AUTHORIZED):
            matching = [p for p in payments if p.get('status') == status]
            if matching:
                return max(matching, key=created_at)
        return max(payments, key=created_at) if payments else None

    @api.model
    def _orkestapay_normalize_payment(self, payment):
        """Return the normalized values of a `PaymentResponse`.

        Only the card brand and last 4 digits are kept; no other card data is stored.

        :param dict payment: The payment.
        :rtype: dict
        """
        card = (payment.get('payment_source') or {}).get('card') or {}
        transactions = sorted(
            payment.get('transactions') or [], key=lambda trx: int(trx.get('created_at') or 0)
        )
        return {
            'payment_id': payment.get('payment_id'),
            'status': payment.get('status'),
            'card_brand': card.get('brand'),
            'last_four': card.get('last_four'),
            'message': transactions[-1].get('message') if transactions else None,
        }

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        """Override of `payment` to extract the reference from the payment data."""
        if provider_code != 'orkestapay':
            return super()._extract_reference(provider_code, payment_data)
        return payment_data.get('reference')

    def _extract_amount_data(self, payment_data):
        """Override of `payment` to extract the amount and currency from the payment data."""
        if self.provider_code != 'orkestapay':
            return super()._extract_amount_data(payment_data)
        if self.operation == 'refund':
            refund = payment_data.get('refund') or {}
            if refund.get('amount') is None:
                # Don't turn a refund the API reports as made into an error for a missing field.
                return None
            return {
                'amount': float(refund['amount']),
                'currency_code': self.currency_id.name,
            }
        amount = payment_data.get('amount')
        return {
            'amount': float(amount) if amount is not None else None,
            'currency_code': payment_data.get('currency'),
        }

    def _apply_updates(self, payment_data):
        """Override of `payment` to update the transaction based on the payment data."""
        if self.provider_code != 'orkestapay':
            return super()._apply_updates(payment_data)

        if self.operation == 'refund':
            self._orkestapay_apply_refund_updates(payment_data.get('refund') or {})
            return

        if order_id := payment_data.get('order_id'):
            self.provider_reference = order_id
        payment = payment_data.get('payment') or {}
        self._orkestapay_save_payment_details(payment)

        order_status = payment_data.get('order_status')
        if (
            order_status not in const.ORDER_STATUS_MAPPING['done']
            and payment.get('status') == const.PAYMENT_STATUS_AUTHORIZED
        ):
            _logger.warning(
                "The OrkestaPay payment %s of transaction %s was only authorized; check the"
                " capture settings of the checkout.", payment.get('payment_id'), self.reference,
            )
            self._set_error(self.env._(
                "OrkestaPay only authorized this payment instead of charging it. Review the"
                " capture settings of the checkout in the OrkestaPay portal, then capture or"
                " cancel the payment there."
            ))
        elif order_status in const.ORDER_STATUS_MAPPING['done']:
            self._set_done()
        elif order_status in const.ORDER_STATUS_MAPPING['pending']:
            self._set_pending()
        elif order_status in const.ORDER_STATUS_MAPPING['cancel']:
            self._set_canceled(state_message=self.env._(
                "The OrkestaPay order is %(status)s.", status=order_status
            ))
        elif order_status in const.ORDER_STATUS_MAPPING['error']:
            _logger.warning(
                "The OrkestaPay order of transaction %s failed: %s",
                self.reference, payment.get('message'),
            )
            self._set_error(payment.get('message') or self.env._("The payment was declined."))
        else:
            _logger.warning(
                "Received an unknown OrkestaPay order status for transaction %s: %s",
                self.reference, order_status,
            )
            self._set_error(
                self.env._("Received data with invalid order status: %s", order_status)
            )

    def _orkestapay_save_payment_details(self, payment):
        """Save the payment id, card brand and last 4 digits, when the API returns them.

        :param dict payment: The normalized payment.
        :return: None
        """
        values = {
            'orkestapay_payment_id': payment.get('payment_id'),
            'orkestapay_card_brand': payment.get('card_brand'),
            'orkestapay_card_last_four': payment.get('last_four'),
        }
        values = {field: value for field, value in values.items() if value}
        if values:
            self.write(values)

    # === BUSINESS METHODS - RECONCILIATION === #

    @api.model
    def _cron_orkestapay_reconcile_pending(self):
        """Reconcile the OrkestaPay transactions left in `draft` or `pending`.

        Covers buyers who never come back and lost webhooks: transactions aged between
        `RECONCILE_MIN_AGE` and `RECONCILE_MAX_AGE`, by batches of `RECONCILE_BATCH_SIZE`, go
        through the same fetch-to-verify path as the return and the webhook. Each one
        is processed in its own savepoint and committed, so an error does not stop the others.

        Only transactions of the environment of the provider's current credentials are queried:
        sandbox ones in Test Mode, production ones when Enabled. With a disabled provider the
        environment of the credentials is unknown, so nothing is reconciled. The least recently
        queried transactions come first, so that unpaid checkouts don't monopolize the batches,
        and a run stops when the cron runs out of time.

        :return: None
        """
        now = fields.Datetime.now()
        txs = self.search([
            ('provider_code', '=', 'orkestapay'),
            ('operation', '!=', 'refund'),
            ('state', 'in', ('draft', 'pending')),
            ('provider_reference', '!=', False),
            # Only the environment of the current credentials: sandbox credentials can't query
            # production orders and vice versa.
            '|',
            '&', ('provider_id.state', '=', 'enabled'), ('is_live', '=', True),
            '&', ('provider_id.state', '=', 'test'), ('is_live', '=', False),
            ('create_date', '<=', now - timedelta(seconds=const.RECONCILE_MIN_AGE)),
            ('create_date', '>=', now - timedelta(seconds=const.RECONCILE_MAX_AGE)),
        ], order='orkestapay_last_reconcile_date asc nulls first, create_date asc',
            limit=const.RECONCILE_BATCH_SIZE)
        in_cron = bool(self.env.context.get('cron_id'))
        processed = failed = skipped = 0
        for index, tx in enumerate(txs):
            locked = False
            try:
                with self.env.cr.savepoint():
                    tx._orkestapay_process_from_api('cron')
                processed += 1
            except LockError:
                # Another request holds the row: writing to it would wait for that request, and
                # leaving it unmarked keeps it first in line for the next run.
                locked = True
                skipped += 1
                _logger.info("Skipped the reconciliation of %s: being processed.", tx.reference)
            except ValidationError as error:
                failed += 1
                _logger.warning(
                    "Could not reconcile the transaction %s (code %s, request_id %s).",
                    tx.reference, getattr(error, 'error_code', None),
                    getattr(error, 'request_id', None),
                )
            # One transaction must not stop the others.
            except Exception:  # pylint: disable=broad-exception-caught
                failed += 1
                _logger.exception("Unexpected error while reconciling %s.", tx.reference)
            if not locked:
                # Recorded whatever the outcome, so that the next runs serve the other ones.
                tx.orkestapay_last_reconcile_date = fields.Datetime.now()
            if in_cron:
                remaining_time = self.env['ir.cron']._commit_progress(
                    1, remaining=len(txs) - index - 1
                )
                if remaining_time <= 0:
                    _logger.info("Stopped the reconciliation: out of time for this run.")
                    break
        if txs:
            _logger.info(
                "Reconciled %s OrkestaPay transactions (%s failed, %s skipped).",
                processed, failed, skipped,
            )
        if processed:
            # Post-process the transactions whose state may have changed (e.g., confirm the
            # sales orders of the paid ones).
            post_processing_cron = self.env.ref(
                'payment.cron_post_process_payment_tx', raise_if_not_found=False
            )
            if post_processing_cron:
                post_processing_cron.sudo()._trigger()

    # === BUSINESS METHODS - REFUNDS === #

    def _send_refund_request(self):
        """Override of `payment` to request a refund to OrkestaPay.

        The request carries `Idempotency-Key = <refund reference>`. On a timeout or connection
        error, it is retried once with the same key; if the outcome is still unknown, the refund
        transaction is set in error with a message asking to check the portal before retrying,
        because a manual retry creates another refund transaction with another key.

        Note: `self.ensure_one()` from `_refund`.

        :return: None
        :raise ValidationError: If the refund cannot be requested or its outcome is unknown.
        """
        if self.provider_code != 'orkestapay':
            return super()._send_refund_request()

        source_tx = self.source_transaction_id
        if not source_tx.orkestapay_payment_id:
            raise ValidationError(self.env._(
                "The OrkestaPay payment of transaction %s is unknown; refund it from the"
                " OrkestaPay portal.", source_tx.reference,
            ))
        # Serialize with the processing of `payment.refund` webhooks: until this refund is saved,
        # the webhook must not mistake it for a refund made outside Odoo.
        try:
            source_tx.lock_for_update()
        except LockError:
            raise ValidationError(self.env._(
                "The payment is being updated by OrkestaPay; try the refund again in a moment."
            )) from None

        endpoint = const.PAYMENT_REFUND_ENDPOINT.format(
            payment_id=quote(str(source_tx.orkestapay_payment_id), safe='')
        )
        payload = {
            # Refund transactions have a negative amount in Odoo.
            'amount': self._orkestapay_format_amount(-self.amount),
            'description': self.env._(
                "Refund %(refund)s of %(payment)s",
                refund=self.reference, payment=source_tx.reference,
            ),
        }
        outcome_unknown = False
        requested_at_ms = int(time.time() * 1000)
        for attempt in (1, 2):
            try:
                response = self._send_api_request(
                    'POST', endpoint, json=payload, idempotency_key=self.reference
                )
                break
            except OrkestaPayApiError as error:
                # After an unanswered attempt, OrkestaPay answers a retry with the same key with a
                # 500 instead of the original response (seen in sandbox): still unknown. Any error
                # of the retry, a 4xx included, is treated as unknown for the same reason.
                if not (error.is_retryable or outcome_unknown):
                    raise  # Rejected (4xx): nothing was refunded.
                outcome_unknown = True
                if attempt == 1:
                    _logger.warning(
                        "Unknown outcome of the refund request %s; retrying with the same"
                        " idempotency key.", self.reference,
                    )
                    continue
                response = self._orkestapay_find_unlinked_refund(-self.amount, requested_at_ms)
                if not response:
                    raise ValidationError(self.env._(
                        "OrkestaPay did not confirm the refund. Check in the OrkestaPay portal"
                        " whether the refund was made before trying again."
                    )) from None
                _logger.warning(
                    "Matched the unconfirmed refund request %s with the OrkestaPay refund %s by"
                    " amount.", self.reference, response.get('transaction_id'),
                )
                self._log_message_on_linked_documents(self.env._(
                    "OrkestaPay did not confirm the refund %(refund)s; it was matched with the"
                    " OrkestaPay refund %(refund_id)s of the same amount. Check it in the"
                    " OrkestaPay portal.",
                    refund=self.reference, refund_id=response.get('transaction_id'),
                ))

        refund = self._orkestapay_normalize_trx(response)
        if refund.get('transaction_id'):
            # Save it before any validation: the refund exists in OrkestaPay and the
            # `payment.refund` webhook must recognize it as made from Odoo.
            self.provider_reference = refund['transaction_id']
        self._process('orkestapay', {'reference': self.reference, 'refund': refund})
        if self.state == 'done':
            # The refundable amount of the payment only decreases once the refund is
            # post-processed (its accounting payment created): don't wait for the cron. A failure
            # must not roll back the refund, which OrkestaPay already made; the cron retries.
            try:
                with self.env.cr.savepoint():
                    self._post_process()
            # Any failure is retried by the cron.
            except Exception:  # pylint: disable=broad-exception-caught
                _logger.exception(
                    "Could not post-process the refund %s; the cron will retry.", self.reference
                )

    def _orkestapay_find_unlinked_refund(self, amount, requested_at_ms):
        """Find the refund of the source payment that an unanswered refund request made.

        The idempotency key prevents a second refund but OrkestaPay does not replay the original
        response, so the refund is looked for among the payment's successful refunds of the same
        amount that are linked neither to a refund transaction of Odoo nor to an external refund,
        and that were not created before the request (e.g., an earlier refund from the portal
        whose webhook has not been processed yet). The match is a heuristic.

        Note: `self.ensure_one()`

        :param float amount: The (positive) refunded amount.
        :param int requested_at_ms: When the refund was first requested, in Unix milliseconds.
        :return: The refund operation, if exactly one matches; None otherwise.
        :rtype: dict|None
        """
        self.ensure_one()
        source_tx = self.source_transaction_id
        try:
            payments_page = self._send_api_request(
                'GET',
                const.ORDER_PAYMENTS_ENDPOINT.format(
                    order_id=quote(source_tx.provider_reference or '', safe='')
                ),
                params={'page': 0, 'size': 50},
            )
        except ValidationError as error:
            _logger.warning(
                "Could not list the payments to confirm the refund %s (request_id %s).",
                self.reference, getattr(error, 'request_id', None),
            )
            return None
        linked_ids = set(
            source_tx.child_transaction_ids.filtered(lambda tx: tx.operation == 'refund')
            .mapped('provider_reference')
        ) | set((source_tx.orkestapay_external_refund_ids or '').split())
        earliest_ms = requested_at_ms - const.REFUND_MATCH_CLOCK_SKEW * 1000

        def is_candidate(trx):
            try:
                trx_amount = float(trx.get('amount'))
                created_at = int(trx.get('created_at') or 0)
            except (TypeError, ValueError):
                return False
            return (
                trx.get('type') == const.TRX_TYPE_REFUND
                and trx.get('status') == const.TRX_STATUS_SUCCESS
                and trx.get('transaction_id') not in linked_ids
                and (not created_at or created_at >= earliest_ms)
                and self.currency_id.compare_amounts(trx_amount, amount) == 0
            )

        candidates = [
            trx
            for payment in (payments_page or {}).get('content') or []
            if payment.get('payment_id') == source_tx.orkestapay_payment_id
            for trx in payment.get('transactions') or []
            if is_candidate(trx)
        ]
        return candidates[0] if len(candidates) == 1 else None

    @api.model
    def _orkestapay_normalize_trx(self, trx):
        """Return the normalized values of a `TrxResponse` (an operation on a payment).

        :param dict trx: The operation returned by the API.
        :rtype: dict
        """
        trx = trx or {}
        return {
            'transaction_id': trx.get('transaction_id'),
            'type': trx.get('type'),
            'status': trx.get('status'),
            'amount': trx.get('amount'),
            'message': trx.get('message'),
        }

    def _orkestapay_apply_refund_updates(self, refund):
        """Update a refund transaction from the normalized refund operation.

        :param dict refund: The normalized `TrxResponse`.
        :return: None
        """
        if refund.get('transaction_id'):
            self.provider_reference = refund['transaction_id']
        status = refund.get('status')
        if status == const.TRX_STATUS_SUCCESS:
            self._set_done()
        elif status in const.TRX_STATUSES_FAILED:
            _logger.warning(
                "OrkestaPay refused the refund %s (%s): %s",
                self.reference, status, refund.get('message'),
            )
            self._set_error(refund.get('message') or self.env._("OrkestaPay refused the refund."))
        else:
            _logger.warning(
                "Received an unknown OrkestaPay refund status for %s: %s", self.reference, status
            )
            self._set_error(self.env._(
                "Received an unknown refund status: %s. Check the refund in the OrkestaPay"
                " portal.", status,
            ))

    def _orkestapay_process_refund_event(self):
        """Handle a `payment.refund` webhook for this source transaction.

        The refunds are read from the API, not from the webhook body. Refunds already linked to a
        refund transaction of Odoo are ignored; each refund made outside Odoo gets a single note
        on the linked documents, and no accounting entry.

        Note: `self.ensure_one()`

        :return: None
        :raise LockError: If another process holds the transaction (e.g., a refund in progress).
        :raise ValidationError: If the API cannot be queried.
        """
        self.ensure_one()
        if not self.provider_reference:
            return
        self.lock_for_update()
        order_id = quote(self.provider_reference, safe='')
        payments_page = self._send_api_request(
            'GET',
            const.ORDER_PAYMENTS_ENDPOINT.format(order_id=order_id),
            params={'page': 0, 'size': 50},  # An order has a handful of payments at most.
        )
        refunds = [
            self._orkestapay_normalize_trx(trx)
            for payment in (payments_page or {}).get('content') or []
            for trx in payment.get('transactions') or []
            if trx.get('type') == const.TRX_TYPE_REFUND
            and trx.get('status') == const.TRX_STATUS_SUCCESS
            and trx.get('transaction_id')
        ]
        known_ids = set(
            self.child_transaction_ids.filtered(lambda tx: tx.operation == 'refund')
            .mapped('provider_reference')
        )
        noted_ids = set((self.orkestapay_external_refund_ids or '').split())
        previously_noted_ids = set(noted_ids)
        for refund in refunds:
            refund_id = refund['transaction_id']
            if refund_id in known_ids or refund_id in noted_ids:
                continue
            amount = self.currency_id.format(float(refund.get('amount') or 0.0))
            self._log_message_on_linked_documents(self.env._(
                "A refund of %(amount)s was made outside Odoo, e.g., in the OrkestaPay portal"
                " (transaction %(refund_id)s). No accounting entry was created; record it"
                " manually if needed.",
                amount=amount, refund_id=refund_id,
            ))
            _logger.info(
                "Noted the external OrkestaPay refund %s of transaction %s.",
                refund_id, self.reference,
            )
            noted_ids.add(refund_id)
        if noted_ids != previously_noted_ids:
            self.orkestapay_external_refund_ids = '\n'.join(sorted(noted_ids))
