"""providers.py: which provider is asked for what, and never a key on the wire."""
import providers


def test_tiers_offer_fmp_only_with_a_key_and_only_for_us_listings(monkeypatch):
    monkeypatch.setattr(providers, "has_key", lambda name: False)
    assert [t for t, _ in providers.tiers("history", "AAPL")] == ["yfinance", "default"]
    monkeypatch.setattr(providers, "has_key", lambda name: name == "fmp_api_key")
    assert [t for t, _ in providers.tiers("history", "AAPL")] == ["yfinance", "fmp", "default"]
    assert [t for t, _ in providers.tiers("history", "^GSPC")] == ["yfinance", "fmp", "default"]
    assert [t for t, _ in providers.tiers("history", "0700.HK")] == ["yfinance", "default"]
    assert [t for t, _ in providers.tiers("fx", "GBPUSD")] == ["yfinance", "default"]


def test_credentials_reads_only_known_keys(tmp_path, monkeypatch):
    path = tmp_path / "config.local.json"
    path.write_text('{"flex_token": "secret", "providers": {"fmp_api_key": "k1", "bogus": "x", "tiingo_token": ""}}')
    monkeypatch.setattr(providers, "CONFIG_PATH", path)
    assert providers.credentials() == {"fmp_api_key": "k1"}
    monkeypatch.setattr(providers, "CONFIG_PATH", tmp_path / "missing.json")
    assert providers.credentials() == {}


def test_fx_ecb_crosses_through_the_euro(monkeypatch):
    class Result:
        def model_dump(self):
            return {"date": "2026-09-01", "GBP": 0.85655, "USD": 1.159, "HKD": 9.0877, "KRW": 1593.17}

    class Obb:
        class currency:
            @staticmethod
            def reference_rates(provider):
                return type("R", (), {"results": Result()})()

    monkeypatch.setattr(providers, "obb", lambda: Obb)
    out = providers.fx_ecb(["USD", "HKD", "KRW", "XXX"])
    assert out["EUR"] == 0.85655
    assert abs(out["USD"] - 0.85655 / 1.159) < 1e-9          # £ per $
    assert abs(out["KRW"] - 0.85655 / 1593.17) < 1e-12
    assert "XXX" not in out
