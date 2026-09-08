"""The provider layer: one openbb, credentials from config, a second tier.

Five modules each imported openbb for themselves and each hard-coded
`provider="yfinance"`. This is the one place that imports it, applies any
keys the operator put in config.local.json, and says which providers are
worth asking for what:

    "providers": {"fmp_api_key": "", "alpha_vantage_api_key": "", "tiingo_token": ""}

Keys already in ~/.openbb_platform/user_settings.json keep working; the
config block is for keys that should live with the rest of this project's
configuration. /api/intent's allowlist never serves either.

Tiers are honest about coverage. FMP's free plan answers for US listings and
the `^` indices and refuses a venue-suffixed symbol, so it is offered only
for those. The ECB's reference rates are keyless and daily and cover every
currency in the book, which makes them the natural second source for FX —
openbb's own "default" tier is the third, and IBKR's account rates the last.
Every call still goes through the shared breaker.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("providers")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.local.json"
KEY_NAMES = ("fmp_api_key", "alpha_vantage_api_key", "tiingo_token", "polygon_api_key")

_applied = False


def credentials() -> dict[str, str]:
    """Non-empty provider keys from config.local.json. Never raises."""
    try:
        doc = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}
    block = doc.get("providers") or {}
    return {k: str(v) for k, v in block.items() if k in KEY_NAMES and v}


def obb():
    """The openbb entry point, imported once, dataframes on, keys applied.

    Still deferred: the import is seconds of CPU and every caller sits behind
    serve.py's feed gate for exactly that reason. This only centralises it.
    """
    global _applied
    from openbb import obb as _obb
    _obb.user.preferences.output_type = "dataframe"
    if not _applied:
        for name, value in credentials().items():
            try:
                setattr(_obb.user.credentials, name, value)
            except Exception as exc:  # an unknown key name in a future openbb
                log.warning("providers: could not apply %s: %s", name, exc)
        _applied = True
    return _obb


def has_key(name: str) -> bool:
    if name in credentials():
        return True
    try:
        value = getattr(obb().user.credentials, name, None)
        return bool(value and str(value) != "None")
    except Exception:
        return False


def us_listed(symbol: str) -> bool:
    """What FMP's free tier answers for: no venue suffix, or an index."""
    return symbol.startswith("^") or "." not in symbol


def tiers(kind: str, symbol: str = "") -> list[tuple[str, dict]]:
    """Ordered (tag, kwargs) pairs for a price call on `symbol`."""
    out = [("yfinance", {"provider": "yfinance"})]
    if kind in ("quote", "history") and has_key("fmp_api_key") and (not symbol or us_listed(symbol)):
        out.append(("fmp", {"provider": "fmp"}))
    out.append(("default", {}))
    return out


def fx_ecb(currencies) -> dict[str, float]:
    """{currency: rate into GBP} from the ECB's daily reference rates.

    The ECB quotes everything per euro; a rate into sterling is the euro
    rate of GBP over the euro rate of the currency. Keyless, one call.
    """
    result = obb().currency.reference_rates(provider="ecb").results
    doc = result.model_dump() if hasattr(result, "model_dump") else (
        result[0].model_dump() if isinstance(result, list) and result else dict(result or {}))
    gbp_per_eur = doc.get("GBP")
    if not gbp_per_eur:
        return {}
    out = {"EUR": float(gbp_per_eur)}
    for ccy in currencies:
        per_eur = doc.get(ccy)
        if per_eur:
            out[ccy] = float(gbp_per_eur) / float(per_eur)
    return out
