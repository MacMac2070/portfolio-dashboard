"""The aggregator seam: one interface, a stub per candidate provider.

The shape exists now so the OAuth endpoints and the pull can be written and
wired; the four methods get real bodies once the provider account exists and
its API docs are in hand (build plan §3 — a human step: register with Enable
Banking or Yapily, confirm Lloyds and HSBC appear in their sandbox, bring the
credentials back). Until then every method raises NotImplementedError naming
exactly what it is waiting for, which the API layer surfaces as a clear 501.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config

BANKS = ("lloyds", "hsbc")


@dataclass
class Account:
    account_id: str
    name: str
    currency: str


@dataclass
class Transaction:
    source_transaction_id: str
    account_id: str
    date: str            # ISO yyyy-mm-dd
    amount: float        # signed; spend negative
    currency: str
    raw_description: str


class AggregatorClient:
    """What every provider must answer. Implementations own all HTTP detail."""

    def get_auth_url(self, bank: str, state: str) -> str:
        """The provider's consent URL for `bank`, carrying `state` for the callback."""
        raise NotImplementedError

    def exchange_token(self, code: str) -> dict:
        """Auth code -> {access_token, refresh_token|None, consent_expires_at ISO}."""
        raise NotImplementedError

    def get_accounts(self, access_token: str) -> list[Account]:
        raise NotImplementedError

    def get_transactions(self, access_token: str, account_id: str,
                         since: str | None) -> list[Transaction]:
        """Transactions since the ISO timestamp (None = provider's full window)."""
        raise NotImplementedError


class EnableBankingClient(AggregatorClient):
    _WAITING = ("Enable Banking integration not implemented yet — needs the "
                "registered app's credentials and the API docs for {what} "
                "(build plan §3, a manual signup step)")

    def get_auth_url(self, bank: str, state: str) -> str:
        raise NotImplementedError(self._WAITING.format(what="the authorisation URL format"))

    def exchange_token(self, code: str) -> dict:
        raise NotImplementedError(self._WAITING.format(what="the token exchange endpoint"))

    def get_accounts(self, access_token: str) -> list[Account]:
        raise NotImplementedError(self._WAITING.format(what="the accounts endpoint"))

    def get_transactions(self, access_token: str, account_id: str,
                         since: str | None) -> list[Transaction]:
        raise NotImplementedError(self._WAITING.format(what="the transactions endpoint"))


class YapilyClient(AggregatorClient):
    _WAITING = ("Yapily integration not implemented yet — needs the registered "
                "app's credentials and the API docs for {what} "
                "(build plan §3, a manual signup step)")

    def get_auth_url(self, bank: str, state: str) -> str:
        raise NotImplementedError(self._WAITING.format(what="the authorisation URL format"))

    def exchange_token(self, code: str) -> dict:
        raise NotImplementedError(self._WAITING.format(what="the token exchange endpoint"))

    def get_accounts(self, access_token: str) -> list[Account]:
        raise NotImplementedError(self._WAITING.format(what="the accounts endpoint"))

    def get_transactions(self, access_token: str, account_id: str,
                         since: str | None) -> list[Transaction]:
        raise NotImplementedError(self._WAITING.format(what="the transactions endpoint"))


def client() -> AggregatorClient:
    """The configured provider, from AGGREGATOR_PROVIDER."""
    provider = (config.get("AGGREGATOR_PROVIDER") or "").strip().lower()
    if provider == "enable_banking":
        return EnableBankingClient()
    if provider == "yapily":
        return YapilyClient()
    raise RuntimeError(
        'AGGREGATOR_PROVIDER must be "enable_banking" or "yapily" — '
        "set it in .env.expense-tracker")
