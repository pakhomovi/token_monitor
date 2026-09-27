from token_monitor.config import load_params, load_settings
from token_monitor.models import Network


def test_param_overrides_are_typed():
    p = load_params({"PARAM_MAX_TAX": "0.03", "PARAM_STRICT_UNKNOWN": "false"})
    assert p.max_tax == 0.03
    assert p.strict_unknown is False
    assert p.min_liquidity == 10_000            # не переопределённое остаётся дефолтом


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("NETWORKS", "bsc, base")
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.1.10:11434/")
    s = load_settings(env_file=None)
    assert s.networks == (Network.BSC, Network.BASE)
    assert s.ollama_url == "http://192.168.1.10:11434"
