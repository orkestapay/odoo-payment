# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

from datetime import timedelta
from pprint import pformat

import psycopg2
import requests

from odoo import api, fields, models
from odoo.exceptions import LockError
from odoo.tools.urls import urljoin

from odoo.addons.payment.const import SENSITIVE_KEYS as PAYMENT_SENSITIVE_KEYS
from odoo.addons.payment.logging import get_payment_logger
from odoo.addons.payment_orkestapay import const
from odoo.addons.payment_orkestapay.utils import OrkestaPayApiError, mask_sensitive_text

# Extend the keys masked by the generic request/response logs of the `payment` module, as intended
# by its logger (see `payment.models.payment_provider`).
PAYMENT_SENSITIVE_KEYS.update(const.SENSITIVE_KEYS)

_logger = get_payment_logger(__name__, sensitive_keys=const.SENSITIVE_KEYS)


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('orkestapay', "OrkestaPay")], ondelete={'orkestapay': 'set default'}
    )
    orkestapay_client_id = fields.Char(
        string="Client ID",
        help="The Client ID of your OrkestaPay API keys.",
        required_if_provider='orkestapay',
        copy=False,
        groups='base.group_system',
    )
    orkestapay_client_secret = fields.Char(
        string="Client Secret",
        help="The Client Secret of your OrkestaPay API keys.",
        required_if_provider='orkestapay',
        copy=False,
        groups='base.group_system',
    )
    orkestapay_webhook_secret = fields.Char(
        string="Webhook Signing Secret",
        help="The Signing Secret (whsec_...) of the webhook endpoint registered in the OrkestaPay"
             " portal.",
        required_if_provider='orkestapay',
        copy=False,
        groups='base.group_system',
    )
    orkestapay_webhook_url = fields.Char(
        string="Webhook URL",
        help="The URL to register as a webhook endpoint in the OrkestaPay portal.",
        compute='_compute_orkestapay_webhook_url',
    )

    # Technical fields caching the OAuth access token. Never displayed.
    orkestapay_access_token = fields.Char(
        string="OrkestaPay Access Token", copy=False, groups='base.group_system'
    )
    orkestapay_token_expiry = fields.Datetime(
        string="OrkestaPay Token Expiry", copy=False, groups='base.group_system'
    )

    # === COMPUTE METHODS === #

    def _compute_feature_support_fields(self):  # pylint: disable=missing-return
        """Override of `payment` to enable additional features."""
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == 'orkestapay').update({
            'support_refund': 'partial',
        })

    @api.depends('code')
    def _compute_orkestapay_webhook_url(self):
        for provider in self:
            if provider.code == 'orkestapay':
                provider.orkestapay_webhook_url = urljoin(
                    provider.get_base_url(), const.WEBHOOK_ROUTE
                )
            else:
                provider.orkestapay_webhook_url = False

    # === CRUD METHODS === #

    def write(self, vals):
        """Override of `payment` to invalidate the cached token when the credentials or the state
        (hence the environment) change."""
        invalidate_token = bool(
            {'orkestapay_client_id', 'orkestapay_client_secret', 'state'} & vals.keys()
        )
        result = super().write(vals)
        if invalidate_token:
            self.filtered(lambda p: p.code == 'orkestapay')._orkestapay_clear_access_token()
        return result

    def _get_default_payment_method_codes(self):
        """Override of `payment` to return the default payment method codes."""
        self.ensure_one()
        if self.code != 'orkestapay':
            return super()._get_default_payment_method_codes()
        return const.DEFAULT_PAYMENT_METHOD_CODES

    # === BUSINESS METHODS === #

    def _get_supported_currencies(self):
        """Override of `payment` to restrict the provider to the currencies OrkestaPay supports."""
        supported_currencies = super()._get_supported_currencies()
        if self.code == 'orkestapay':
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    # === ACTION METHODS === #

    def action_orkestapay_test_connection(self):
        """Request a fresh access token with the saved credentials and report the result.

        :return: A client action displaying a notification.
        :rtype: dict
        """
        self.ensure_one()
        self.check_access('write')  # Only users allowed to configure the provider can test it.

        if self.state == 'enabled':
            environment = self.env._("production")
        else:
            environment = self.env._("sandbox")
        try:
            self._orkestapay_get_access_token(force_refresh=True)
        except OrkestaPayApiError as error:
            notification = {
                'type': 'danger',
                'title': self.env._(
                    "Connection failed (%(environment)s)", environment=environment
                ),
                'message': str(error),
                'sticky': True,
            }
        else:
            notification = {
                'type': 'success',
                'title': self.env._(
                    "Connection successful (%(environment)s)", environment=environment
                ),
                'message': self.env._("The OrkestaPay credentials are valid."),
                'sticky': False,
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': notification,
        }

    # === TOKEN METHODS === #

    def _orkestapay_token_memo_key(self):
        """Return the key under which the token is memoized for the current DB transaction."""
        return ('payment_orkestapay.access_token', self.id)

    def _orkestapay_get_access_token(self, force_refresh=False):
        """Return a valid OAuth access token, requesting a new one if needed.

        The token is cached in technical fields of the provider and reused while it has more than
        `TOKEN_REFRESH_MARGIN` seconds left. The cache is written in a separate cursor, so the
        current transaction also memoizes the token to avoid reading its stale snapshot.

        Note: `self.ensure_one()`

        :param bool force_refresh: Whether to ignore the cached token (e.g., after a 401).
        :return: The access token.
        :rtype: str
        :raise OrkestaPayApiError: If the token cannot be obtained.
        """
        self.ensure_one()
        margin = timedelta(seconds=const.TOKEN_REFRESH_MARGIN)
        now = fields.Datetime.now()
        memo_key = self._orkestapay_token_memo_key()
        if not force_refresh:
            memoized = self.env.cr.cache.get(memo_key)
            if memoized and memoized[1] - now > margin:
                return memoized[0]
            provider_sudo = self.sudo()
            token = provider_sudo.orkestapay_access_token
            expiry = provider_sudo.orkestapay_token_expiry
            if token and expiry and expiry - now > margin:
                return token

        token, expiry = self._orkestapay_request_access_token()
        self.env.cr.cache[memo_key] = (token, expiry)
        self._orkestapay_store_access_token(token, expiry)
        return token

    def _orkestapay_request_access_token(self):
        """Request a new access token with the `client_credentials` grant.

        The request is sent directly rather than through `_send_api_request` so that neither the
        client secret nor the token goes through the generic request/response logs.

        Note: `self.ensure_one()`

        :return: The access token and its expiry datetime.
        :rtype: tuple[str, datetime]
        :raise OrkestaPayApiError: If the request fails.
        """
        self.ensure_one()
        provider_sudo = self.sudo()
        if not (provider_sudo.orkestapay_client_id and provider_sudo.orkestapay_client_secret):
            raise OrkestaPayApiError(
                self.env._("Enter the OrkestaPay Client ID and Client Secret first.")
            )

        url = self._build_request_url(const.TOKEN_ENDPOINT)
        _logger.info(
            "Requesting an OrkestaPay access token from %s for provider %s.", url, self.id
        )
        try:
            response = requests.request(
                'POST',
                url,
                json={
                    'client_id': provider_sudo.orkestapay_client_id,
                    'client_secret': provider_sudo.orkestapay_client_secret,
                    'grant_type': 'client_credentials',
                },
                headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
                timeout=const.REQUEST_TIMEOUT,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            _logger.warning(
                "Could not reach the OrkestaPay authentication endpoint for provider %s.", self.id
            )
            raise OrkestaPayApiError(
                self.env._("Could not establish the connection to OrkestaPay."),
                is_network_error=True,
            ) from None

        if not response.ok:
            raise self._orkestapay_build_api_error(response)

        try:
            response_content = response.json()
            token = response_content['access_token']
            expires_in = int(response_content.get('expires_in') or 0)
        except (ValueError, KeyError, TypeError):
            _logger.error(
                "Received an unexpected authentication response from OrkestaPay for provider %s.",
                self.id,
            )
            raise OrkestaPayApiError(
                self.env._("OrkestaPay returned an unexpected authentication response.")
            ) from None
        return token, fields.Datetime.now() + timedelta(seconds=expires_in)

    def _orkestapay_store_access_token(self, token, expiry):
        """Cache the token on the provider in a separate cursor to avoid serialization conflicts
        between concurrent workers.

        Caching is best effort: if the row is locked or the update conflicts, the token is only
        kept for the current transaction.

        Note: `self.ensure_one()`

        :param str token: The access token.
        :param datetime expiry: The expiry datetime of the token.
        :return: None
        """
        self.ensure_one()
        try:
            with self.env.registry.cursor() as cr:
                provider = self.with_env(self.env(cr=cr, su=True))
                provider.lock_for_update()
                provider.write({
                    'orkestapay_access_token': token,
                    'orkestapay_token_expiry': expiry,
                })
        except (LockError, psycopg2.Error) as error:
            _logger.info(
                "Could not cache the OrkestaPay access token of provider %s (%s); it will only be"
                " reused within the current transaction.", self.id, type(error).__name__,
            )

    def _orkestapay_clear_access_token(self):
        """Invalidate the cached access token of the providers.

        :return: None
        """
        for provider in self:
            self.env.cr.cache.pop(provider._orkestapay_token_memo_key(), None)
        self.sudo().write({'orkestapay_access_token': False, 'orkestapay_token_expiry': False})

    # === REQUEST HELPERS === #

    def _send_api_request(
        self, method, endpoint, *, params=None, data=None, json=None, reference=None, **kwargs
    ):
        """Override of `payment` to send OrkestaPay API requests.

        The generic implementation turns every failure into a plain `ValidationError` and cannot
        retry. OrkestaPay needs to (1) retry exactly once with a fresh token after a 401 and
        (2) tell a timeout or 5xx (unknown outcome) from a 4xx (rejected request). This override
        reuses the generic hooks and loggers, and raises `OrkestaPayApiError`, a subclass of
        `ValidationError`, so that callers expecting the generic behavior keep working.

        Note: `self.ensure_one()`

        :param str method: The HTTP method of the request.
        :param str endpoint: The endpoint of the API to reach with the request.
        :param dict params: The query string parameters of the request.
        :param dict|str data: The body of the request.
        :param dict json: The JSON-formatted body of the request.
        :param str reference: The reference of the transaction, if any.
        :param dict kwargs: Optional `idempotency_key`.
        :return: The JSON content of the response.
        :rtype: dict
        :raise OrkestaPayApiError: If the request fails.
        """
        if self.code != 'orkestapay':
            return super()._send_api_request(
                method, endpoint, params=params, data=data, json=json, reference=reference,
                **kwargs,
            )

        self.ensure_one()
        url = self._build_request_url(endpoint, **kwargs)
        payload = params or data or json
        for attempt in (1, 2):
            headers = self._build_request_headers(
                method, endpoint, payload, force_token_refresh=attempt > 1, **kwargs
            )
            self._orkestapay_log_request(method, url, payload, reference)
            try:
                response = requests.request(
                    method, url, params=params, data=data, json=json, headers=headers,
                    timeout=const.REQUEST_TIMEOUT,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                _logger.warning(
                    "No response from OrkestaPay for %s %s (reference %s).", method, url, reference
                )
                raise OrkestaPayApiError(
                    self.env._("Could not establish the connection to OrkestaPay."),
                    is_network_error=True,
                ) from None
            self._orkestapay_log_response(response, reference)

            if response.status_code == 401 and attempt == 1:
                _logger.info("OrkestaPay rejected the access token; retrying with a new one.")
                continue
            if not response.ok:
                raise self._orkestapay_build_api_error(response, reference=reference)
            return self._parse_response_content(response, **kwargs)
        return None  # Unreachable: the second attempt either returns or raises.

    def _orkestapay_log_request(self, method, url, payload, reference):
        """Log an API request without personal data at INFO level.

        The generic `_log_request` logs the whole payload at INFO level, which here includes the
        buyer's personal data and the access tokens of the return URLs. The payload is only logged
        at DEBUG level, with those tokens masked.
        """
        _logger.info(
            "Sending %(method)s request to %(url)s (provider %(provider)s, reference %(ref)s).",
            {'method': method, 'url': url, 'provider': self.id, 'ref': reference},
        )
        if payload:
            _logger.debug(
                "OrkestaPay request payload (reference %s):\n%s",
                reference, mask_sensitive_text(pformat(payload)),
            )

    def _orkestapay_log_response(self, response, reference):
        """Log an API response: status at INFO (ERROR if failed), body at DEBUG only."""
        log_values = {
            'status': response.status_code,
            'url': mask_sensitive_text(response.url),
            'provider': self.id,
            'ref': reference,
        }
        log_msg = (
            "Received HTTP %(status)s from %(url)s (provider %(provider)s,"
            " reference %(ref)s)."
        )
        if response.ok:
            _logger.info(log_msg, log_values)
        else:  # The details, with the request_id, are logged by `_orkestapay_build_api_error`.
            _logger.error(log_msg, log_values)
        _logger.debug(
            "OrkestaPay response body (reference %s):\n%s",
            reference, mask_sensitive_text(response.text),
        )

    def _build_request_url(self, endpoint, **kwargs):
        """Override of `payment` to build the request URL based on the provider state."""
        if self.code != 'orkestapay':
            return super()._build_request_url(endpoint, **kwargs)
        base_url = const.API_URLS.get(self.state, const.API_URL_DISABLED_FALLBACK)
        return urljoin(base_url, endpoint)

    def _build_request_headers(self, method, endpoint, payload, **kwargs):
        """Override of `payment` to build the request headers.

        Recognized kwargs: `idempotency_key` (str) and `force_token_refresh` (bool).
        """
        if self.code != 'orkestapay':
            return super()._build_request_headers(method, endpoint, payload, **kwargs)

        access_token = self._orkestapay_get_access_token(
            force_refresh=kwargs.get('force_token_refresh', False)
        )
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
        }
        if kwargs.get('idempotency_key'):
            headers['Idempotency-Key'] = kwargs['idempotency_key']
        return headers

    def _parse_response_content(self, response, **kwargs):
        """Override of `payment` to tolerate empty response bodies."""
        if self.code != 'orkestapay':
            return super()._parse_response_content(response, **kwargs)
        return response.json() if response.content else {}

    def _parse_response_error(self, response):
        """Override of `payment` to extract the error message from the OrkestaPay error object."""
        if self.code != 'orkestapay':
            return super()._parse_response_error(response)
        error_data = self._orkestapay_get_error_data(response)
        return error_data.get('message') or response.reason or str(response.status_code)

    @api.model
    def _orkestapay_get_error_data(self, response):
        """Return the OrkestaPay error object of a response, or an empty dict.

        :param requests.Response response: The response.
        :rtype: dict
        """
        try:
            error_data = response.json()
        except ValueError:
            return {}
        return error_data if isinstance(error_data, dict) else {}

    def _orkestapay_build_api_error(self, response, reference=None):
        """Log a failed response with its `request_id` and build the matching exception.

        :param requests.Response response: The failed response.
        :param str reference: The reference of the transaction, if any.
        :return: The exception to raise.
        :rtype: OrkestaPayApiError
        """
        error_data = self._orkestapay_get_error_data(response)
        message = self._parse_response_error(response)
        _logger.error(
            "OrkestaPay API error for provider %(provider)s (reference %(ref)s): HTTP %(status)s,"
            " code %(code)s, request_id %(request_id)s: %(message)s",
            {
                'provider': self.id,
                'ref': reference,
                'status': response.status_code,
                'code': error_data.get('code'),
                'request_id': error_data.get('request_id'),
                'message': message,
            },
        )
        return OrkestaPayApiError(
            self.env._("OrkestaPay rejected the request: %(message)s", message=message),
            status_code=response.status_code,
            error_code=error_data.get('code'),
            request_id=error_data.get('request_id'),
        )
