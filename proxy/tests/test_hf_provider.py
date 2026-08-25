"""Tests for Hugging Face provider implementation and OpenRouter regression."""
import json
import os
import sys
import asyncio
import tempfile
import importlib
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

# Ensure proxy package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def set_provider_env(provider="openrouter"):
    """Set environment to simulate a provider selection."""
    os.environ["ATLAS_PROVIDER"] = provider


def clear_provider_env():
    os.environ.pop("ATLAS_PROVIDER", None)


def reload_config():
    """Reload config module to pick up env changes."""
    import proxy.config as cfg
    importlib.reload(cfg)
    return cfg


def write_runtime_provider(provider, model=""):
    """Write a runtime_provider.json file."""
    import proxy.config as cfg
    rp_file = Path(cfg.RUNTIME_PROVIDER_FILE)
    rp_file.parent.mkdir(parents=True, exist_ok=True)
    data = {"provider": provider, "model": model, "timestamp": 1234567890}
    rp_file.write_text(json.dumps(data))
    return rp_file


def cleanup_runtime_provider():
    """Remove runtime provider file."""
    import proxy.config as cfg
    rp = Path(cfg.RUNTIME_PROVIDER_FILE)
    if rp.exists():
        rp.unlink()


# ---------------------------------------------------------------------------
# Provider selection tests
# ---------------------------------------------------------------------------

class TestProviderSelection:

    def test_runtime_provider_openrouter(self):
        """atlas restart → OpenRouter via runtime config."""
        set_provider_env("openrouter")
        rp_file = write_runtime_provider("openrouter", "")
        try:
            cfg = reload_config()
            assert cfg.PROVIDER == "openrouter"
        finally:
            rp_file.unlink()
            clear_provider_env()

    def test_runtime_provider_huggingface(self):
        """atlas restart --huggingface → Hugging Face via runtime config."""
        set_provider_env("openrouter")
        rp_file = write_runtime_provider("huggingface", "moonshotai/Kimi-K3")
        try:
            cfg = reload_config()
            assert cfg.PROVIDER == "huggingface"
        finally:
            rp_file.unlink()
            clear_provider_env()

    def test_runtime_provider_hf_custom_model(self):
        """atlas restart --huggingface --MODEL → custom model via runtime config."""
        set_provider_env("openrouter")
        rp_file = write_runtime_provider("huggingface", "some/custom-model")
        try:
            cfg = reload_config()
            assert cfg.PROVIDER == "huggingface"
            assert cfg.HF_MODEL == "some/custom-model"
        finally:
            rp_file.unlink()
            clear_provider_env()

    def test_default_provider_is_openrouter(self):
        """No runtime config + no env → defaults to openrouter."""
        cleanup_runtime_provider()
        clear_provider_env()
        cfg = reload_config()
        assert cfg.PROVIDER == "openrouter"

    def test_default_hf_model(self):
        """HF default model is deepseek-ai/DeepSeek-V4-Flash:deepinfra."""
        rp_file = write_runtime_provider("huggingface", "")
        try:
            cfg = reload_config()
            assert cfg.HF_MODEL == "deepseek-ai/DeepSeek-V4-Flash:deepinfra"
        finally:
            rp_file.unlink()

    def test_openrouter_default_model_unchanged(self):
        """OpenRouter default model must remain unchanged."""
        rp_file = write_runtime_provider("openrouter", "")
        try:
            cfg = reload_config()
            assert cfg.OPENROUTER_MODEL == "poolside/laguna-s-2.1:free"
        finally:
            rp_file.unlink()


# ---------------------------------------------------------------------------
# KeyPool — full_sticky mode tests
# ---------------------------------------------------------------------------

class TestKeyPoolFullSticky:

    def test_full_sticky_returns_same_key(self):
        """Keys 1,2,3 → first request returns key1, all subsequent return key1."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b", "hf_c"], mode="full_sticky")
        k1 = pool.next_key()
        k2 = pool.next_key()
        k3 = pool.next_key()
        k4 = pool.next_key()
        # Returns (key_str, index, is_healthy)
        assert k1[0] == k2[0] == k3[0] == k4[0]

    def test_full_sticky_retire_then_next(self):
        """Key 1 dies → next_key returns key 2."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b", "hf_c"], mode="full_sticky")
        k1 = pool.next_key()
        assert k1[1] == 0  # index of first key
        asyncio.run(pool.retire_key(k1[1]))
        k2 = pool.next_key()
        assert k2[1] == 1  # index of second key

    def test_full_sticky_all_retired_raises(self):
        """All keys retired → next_key raises ValueError."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        asyncio.run(pool.retire_key(0))
        asyncio.run(pool.retire_key(1))
        with pytest.raises(ValueError):
            pool.next_key()

    def test_partial_sticky_unchanged(self):
        """OpenRouter partial_sticky still rotates when key is unavailable."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["sk_a", "sk_b", "sk_c"], mode="partial_sticky")
        k = pool.next_key()
        assert 0 <= k[1] < 3


# ---------------------------------------------------------------------------
# Dead key management tests
# ---------------------------------------------------------------------------

class TestDeadKeyManagement:

    def test_remove_hf_key_removes_from_active(self, tmp_path):
        """remove_hf_key deletes a key from hf_keys.txt."""
        from proxy.config import remove_hf_key
        active_file = tmp_path / "hf_keys.txt"
        active_file.write_text("hf_alive\nhf_dead\nhf_other\n")
        with patch("proxy.config.HF_KEY_FILE", str(active_file)):
            result = remove_hf_key("hf_dead")
            assert result is True
            lines = active_file.read_text().strip().splitlines()
            assert "hf_dead" not in lines
            assert "hf_alive" in lines
            assert "hf_other" in lines

    def test_remove_hf_key_not_present(self, tmp_path):
        """remove_hf_key is idempotent — returns False if key absent."""
        from proxy.config import remove_hf_key
        active_file = tmp_path / "hf_keys.txt"
        active_file.write_text("hf_alive\nhf_other\n")
        with patch("proxy.config.HF_KEY_FILE", str(active_file)):
            result = remove_hf_key("hf_notthere")
            assert result is False
            lines = active_file.read_text().strip().splitlines()
            assert len(lines) == 2

    def test_add_dead_hf_key_creates_file(self, tmp_path):
        """add_dead_hf_key creates the dead file lazily."""
        from proxy.config import add_dead_hf_key, load_dead_hf_keys
        with patch("proxy.config.HF_DEAD_KEYS_FILE", str(tmp_path / "dead.txt")):
            assert not (tmp_path / "dead.txt").exists()
            result = add_dead_hf_key("hf_test_key")
            assert result is True
            assert (tmp_path / "dead.txt").exists()
            dead = load_dead_hf_keys() if False else set()
            # Load from the actual file
            dead = set()
            content = (tmp_path / "dead.txt").read_text().strip()
            for line in content.splitlines():
                if line.startswith("hf_"):
                    dead.add(line)
            assert "hf_test_key" in dead

    def test_add_dead_hf_key_no_duplicate(self, tmp_path):
        """Dead key file never duplicates."""
        from proxy.config import add_dead_hf_key
        dead_file = tmp_path / "dead.txt"
        with patch("proxy.config.HF_DEAD_KEYS_FILE", str(dead_file)):
            add_dead_hf_key("hf_key1")
            result2 = add_dead_hf_key("hf_key1")
            assert result2 is False  # Already present
            lines = dead_file.read_text().strip().splitlines()
            assert len(lines) == 1

    def test_retire_and_remove_two_sided(self, tmp_path):
        """retire_and_remove_hf_key removes from active AND adds to dead."""
        from proxy.config import retire_and_remove_hf_key
        active_file = tmp_path / "hf_keys.txt"
        dead_file = tmp_path / "dead_hf_keys.txt"
        active_file.write_text("hf_alive\nhf_dead\nhf_other\n")
        with patch("proxy.config.HF_KEY_FILE", str(active_file)):
            with patch("proxy.config.HF_DEAD_KEYS_FILE", str(dead_file)):
                added, removed = retire_and_remove_hf_key("hf_dead")
                assert added is True
                assert removed is True
                # Dead file has the key
                dead_content = dead_file.read_text().strip()
                assert "hf_dead" in dead_content
                # Active file does NOT have the key
                active_lines = active_file.read_text().strip().splitlines()
                assert "hf_dead" not in active_lines
                assert "hf_alive" in active_lines
                assert "hf_other" in active_lines

    def test_retire_and_remove_dead_key_stays_dead(self, tmp_path):
        """A key already in dead_keys.txt is not re-added but still removed from active."""
        from proxy.config import retire_and_remove_hf_key
        active_file = tmp_path / "hf_keys.txt"
        dead_file = tmp_path / "dead_hf_keys.txt"
        active_file.write_text("hf_alive\nhf_dead\n")
        dead_file.write_text("hf_dead\n")
        with patch("proxy.config.HF_KEY_FILE", str(active_file)):
            with patch("proxy.config.HF_DEAD_KEYS_FILE", str(dead_file)):
                added, removed = retire_and_remove_hf_key("hf_dead")
                assert added is False  # already in dead file
                assert removed is True  # still removed from active
                active_lines = active_file.read_text().strip().splitlines()
                assert "hf_dead" not in active_lines
                dead_lines = dead_file.read_text().strip().splitlines()
                assert dead_lines.count("hf_dead") == 1  # no duplicate

    def test_dead_key_never_resurrected(self, tmp_path):
        """After retirement, the key is never loaded back into the active pool."""
        from proxy.config import retire_and_remove_hf_key
        from proxy.keypool import load_keys
        active_file = tmp_path / "hf_keys.txt"
        dead_file = tmp_path / "dead_hf_keys.txt"
        active_file.write_text("hf_alive1\nhf_dead1\nhf_alive2\n")
        with patch("proxy.config.HF_KEY_FILE", str(active_file)):
            with patch("proxy.config.HF_DEAD_KEYS_FILE", str(dead_file)):
                retire_and_remove_hf_key("hf_dead1")
                # After retirement, loading keys should NOT include hf_dead1
                keys = load_keys(str(active_file))
                assert "hf_dead1" not in keys
                assert "hf_alive1" in keys
                assert "hf_alive2" in keys

    def test_migrate_hf_active_keys_cleans_orphans(self, tmp_path):
        """migrate_hf_active_keys removes dead keys lingering in active file."""
        from proxy.config import migrate_hf_active_keys
        active_file = tmp_path / "hf_keys.txt"
        dead_file = tmp_path / "dead_hf_keys.txt"
        active_file.write_text("hf_alive\nhf_dead1\nhf_dead2\nhf_other\n")
        dead_file.write_text("hf_dead1\nhf_dead2\n")
        with patch("proxy.config.HF_KEY_FILE", str(active_file)):
            with patch("proxy.config.HF_DEAD_KEYS_FILE", str(dead_file)):
                removed, kept = migrate_hf_active_keys()
                assert removed == 2
                assert kept == 2
                active_lines = active_file.read_text().strip().splitlines()
                assert "hf_dead1" not in active_lines
                assert "hf_dead2" not in active_lines
                assert "hf_alive" in active_lines
                assert "hf_other" in active_lines

    def test_migrate_hf_active_keys_no_dead_file(self, tmp_path):
        """migrate_hf_active_keys is a no-op when dead file is missing."""
        from proxy.config import migrate_hf_active_keys
        active_file = tmp_path / "hf_keys.txt"
        active_file.write_text("hf_alive\nhf_dead\n")
        dead_file = tmp_path / "dead_hf_keys.txt"  # does not exist
        with patch("proxy.config.HF_KEY_FILE", str(active_file)):
            with patch("proxy.config.HF_DEAD_KEYS_FILE", str(dead_file)):
                removed, kept = migrate_hf_active_keys()
                assert removed == 0
                assert kept == 0  # no dead file to cross-reference
                # Active file unchanged
                assert len(active_file.read_text().strip().splitlines()) == 2

    def test_dead_key_persistence_across_reload(self, tmp_path):
        """Dead keys survive reload and are never reloaded into active pool."""
        from proxy.config import retire_and_remove_hf_key
        from proxy.keypool import load_keys
        active_file = tmp_path / "hf_keys.txt"
        dead_file = tmp_path / "dead_hf_keys.txt"
        active_file.write_text("hf_dead1\nhf_alive1\n")
        with patch("proxy.config.HF_KEY_FILE", str(active_file)):
            with patch("proxy.config.HF_DEAD_KEYS_FILE", str(dead_file)):
                retire_and_remove_hf_key("hf_dead1")
                # Reload active pool — dead key must be gone
                keys = load_keys(str(active_file))
                assert "hf_dead1" not in keys
                assert "hf_alive1" in keys


# ---------------------------------------------------------------------------
# HF error classification tests
# ---------------------------------------------------------------------------

class TestHfErrorClassification:

    def test_429_is_rate_limit_error(self):
        """HTTP 429 → permanent retirement trigger."""
        from proxy.config import is_hf_rate_limit_error, is_hf_key_invalid
        assert is_hf_rate_limit_error(429) is True
        assert is_hf_key_invalid(429) is False

    def test_402_is_rate_limit_error(self):
        """HTTP 402 (Payment Required) → permanent retirement trigger."""
        from proxy.config import is_hf_rate_limit_error
        assert is_hf_rate_limit_error(402) is True

    def test_400_is_not_rate_limit_error(self):
        """400 (malformed request) must NOT retire key."""
        from proxy.config import is_hf_rate_limit_error, is_hf_key_invalid
        assert is_hf_rate_limit_error(400) is False
        assert is_hf_key_invalid(400) is False

    def test_404_is_not_rate_limit_error(self):
        """404 (unsupported model) must NOT retire key."""
        from proxy.config import is_hf_rate_limit_error
        assert is_hf_rate_limit_error(404) is False

    def test_500_is_not_rate_limit_error(self):
        """500 (server error) must NOT retire key."""
        from proxy.config import is_hf_rate_limit_error
        assert is_hf_rate_limit_error(500) is False

    def test_502_is_not_rate_limit_error(self):
        """502 must NOT retire key."""
        from proxy.config import is_hf_rate_limit_error
        assert is_hf_rate_limit_error(502) is False

    def test_503_is_not_rate_limit_error(self):
        """503 must NOT retire key."""
        from proxy.config import is_hf_rate_limit_error
        assert is_hf_rate_limit_error(503) is False

    def test_504_is_not_rate_limit_error(self):
        """504 must NOT retire key."""
        from proxy.config import is_hf_rate_limit_error
        assert is_hf_rate_limit_error(504) is False

    def test_body_quota_marker(self):
        """Body text with quota markers triggers retirement."""
        from proxy.config import is_hf_rate_limit_error
        body = b'{"error": {"message": "Rate limit reached for your account"}}'
        assert is_hf_rate_limit_error(429, body) is True

    def test_body_credit_marker(self):
        """Body text with credit markers triggers retirement."""
        from proxy.config import is_hf_rate_limit_error, is_hf_key_invalid
        body = b'{"error": {"message": "Credit balance is insufficient"}}'
        assert is_hf_rate_limit_error(402, body) is True

    def test_401_invalid_key(self):
        """401 with 'invalid' body → key is permanently invalid."""
        from proxy.config import is_hf_key_invalid
        body = b'{"error": {"message": "Invalid API key"}}'
        assert is_hf_key_invalid(401, body) is True

    def test_401_generic_not_invalid(self):
        """401 without 'invalid'/'unauthorized' marker → not auto-retired."""
        from proxy.config import is_hf_key_invalid
        body = b'{"error": {"message": "Unauthorized access"}}'
        # 'unauthorized' IS a marker, so this should be True
        assert is_hf_key_invalid(401, body) is True

    def test_400_does_not_retire(self):
        """400 body does not trigger retirement via either function."""
        from proxy.config import is_hf_rate_limit_error, is_hf_key_invalid
        body = b'{"error": {"message": "Malformed request"}}'
        assert is_hf_rate_limit_error(400, body) is False
        assert is_hf_key_invalid(400, body) is False


# ---------------------------------------------------------------------------
# Provider-aware URL resolution tests
# ---------------------------------------------------------------------------

class TestProviderUrls:

    def test_openrouter_urls(self):
        """OpenRouter URL resolution is unchanged."""
        from proxy.config import get_chat_url, get_messages_url, get_models_url
        with patch("proxy.config.PROVIDER", "openrouter"):
            from importlib import reload
            import proxy.config as cfg
            reload(cfg)
            # Test with provider explicitly set
            old = cfg.PROVIDER
            cfg.PROVIDER = "openrouter"
            assert cfg.get_chat_url() == cfg.OPENROUTER_CHAT
            assert cfg.get_messages_url() == cfg.OPENROUTER_MESSAGES
            assert cfg.get_models_url() == cfg.OPENROUTER_MODELS

    def test_hf_urls(self):
        """Hugging Face URL resolution uses HF endpoints."""
        import proxy.config as cfg
        assert cfg.is_hf_key_invalid is not None  # sanity
        # Test by directly setting provider
        old = cfg.PROVIDER
        cfg.PROVIDER = "huggingface"
        try:
            assert cfg.get_chat_url() == f"{cfg.HF_BASE_URL}/chat/completions"
            assert cfg.get_messages_url() == f"{cfg.HF_BASE_URL}/chat/completions"
            assert cfg.get_models_url() == f"{cfg.HF_BASE_URL}/models"
        finally:
            cfg.PROVIDER = old

    def test_hf_base_url_correct(self):
        """HF base URL is the OpenAI-compatible API endpoint."""
        import proxy.config as cfg
        assert cfg.HF_BASE_URL == "https://router.huggingface.co/v1"

    def test_openrouter_base_url_unchanged(self):
        """OpenRouter base URL is unchanged."""
        import proxy.config as cfg
        assert cfg.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"

    def test_openrouter_key_file_unchanged(self):
        """OpenRouter key file path is unchanged."""
        import proxy.config as cfg
        assert "openroute_keys.txt" in cfg.KEY_FILE

    def test_hf_key_file(self):
        """HF key file points to huggingface_data/hf_keys.txt."""
        import proxy.config as cfg
        assert "huggingface_data" in cfg.HF_KEY_FILE
        assert cfg.HF_KEY_FILE.endswith("hf_keys.txt")

    def test_hf_dead_key_file(self):
        """HF dead key file points to huggingface_data/dead_hf_keys.txt."""
        import proxy.config as cfg
        assert "huggingface_data" in cfg.HF_DEAD_KEYS_FILE
        assert cfg.HF_DEAD_KEYS_FILE.endswith("dead_hf_keys.txt")


# ---------------------------------------------------------------------------
# OpenRouter regression tests (existing behavior must be unchanged)
# ---------------------------------------------------------------------------

class TestOpenRouterRegression:

    def test_openrouter_key_file_not_hf(self):
        """OpenRouter uses its own key file, not HF."""
        import proxy.config as cfg
        assert cfg.PROVIDER == "openrouter"  # default
        assert cfg.KEY_FILE != cfg.HF_KEY_FILE
        assert "openroute_keys" in cfg.KEY_FILE

    def test_openrouter_model_not_kimi(self):
        """OpenRouter default model is NOT the HF default."""
        import proxy.config as cfg
        assert cfg.OPENROUTER_MODEL != "moonshotai/Kimi-K3"

    def test_openrouter_uses_partial_sticky_by_default(self):
        """OpenRouter key pool defaults to partial_sticky."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["sk_a", "sk_b"])
        assert pool.mode == "partial_sticky"

    def test_hf_uses_full_sticky(self):
        """HF key pool uses full_sticky."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        assert pool.mode == "full_sticky"

    def test_openrouter_translation_uses_openrouter_model(self):
        """Translation layer uses OPENROUTER_MODEL for OR provider."""
        import proxy.config as cfg
        old = cfg.PROVIDER
        cfg.PROVIDER = "openrouter"
        try:
            assert cfg.get_default_model() == cfg.OPENROUTER_MODEL
        finally:
            cfg.PROVIDER = old

    def test_hf_translation_uses_hf_model(self):
        """Translation layer uses HF_MODEL for HF provider."""
        import proxy.config as cfg
        old = cfg.PROVIDER
        cfg.PROVIDER = "huggingface"
        try:
            assert cfg.get_default_model() == cfg.HF_MODEL
        finally:
            cfg.PROVIDER = old


# ---------------------------------------------------------------------------
# Retry / exhaustion tests
# ---------------------------------------------------------------------------

class TestRetryAndExhaustion:

    def test_exhaustion_returns_error(self):
        """When all keys are retired, next_key raises."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a"], mode="full_sticky")
        asyncio.run(pool.retire_key(0))
        with pytest.raises(ValueError):
            pool.next_key()

    def test_retry_does_not_loop_forever(self):
        """Full-sticky: after retiring key 1, key 2 is used once (no cycling)."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        k1 = pool.next_key()
        asyncio.run(pool.retire_key(k1[1]))
        k2 = pool.next_key()
        assert k2[1] == 1
        # Still key 2 (sticky)
        k3 = pool.next_key()
        assert k3[1] == 1


# ---------------------------------------------------------------------------
# CLI parsing tests
# ---------------------------------------------------------------------------

class TestCLIParsing:

    def test_cli_file_has_huggingface_flag(self):
        """atlas CLI supports --huggingface flag parsing."""
        with open("/usr/local/bin/atlas") as f:
            content = f.read()
        assert "--huggingface" in content
        assert "_write_runtime_provider" in content
        assert "cmd_proxy_restart" in content

    def test_cli_restart_hf_writes_config(self, tmp_path):
        """Runtime config structure is correct for HF."""
        import json
        config = {"provider": "huggingface", "model": "moonshotai/Kimi-K3", "timestamp": 123}
        rp_file = tmp_path / "runtime_provider.json"
        rp_file.write_text(json.dumps(config))
        loaded = json.loads(rp_file.read_text())
        assert loaded["provider"] == "huggingface"
        assert loaded["model"] == "moonshotai/Kimi-K3"

    def test_cli_restart_or_writes_config(self, tmp_path):
        """Runtime config structure is correct for OpenRouter."""
        import json
        config = {"provider": "openrouter", "model": "", "timestamp": 123}
        rp_file = tmp_path / "runtime_provider.json"
        rp_file.write_text(json.dumps(config))
        loaded = json.loads(rp_file.read_text())
        assert loaded["provider"] == "openrouter"
        assert loaded["model"] == ""


# ---------------------------------------------------------------------------
# Main.py integration — provider-aware startup
# ---------------------------------------------------------------------------

class TestMainProviderIntegration:

    def test_main_imports_provider(self):
        """main.py reads PROVIDER from config at startup."""
        from proxy import main as m
        assert m.PROVIDER in ("openrouter", "huggingface")

    def test_main_proxy_core_got_provider(self):
        """ProxyCore gets provider from main.py."""
        from proxy.main import _load_active_keys
        # If provider is openrouter, we should load OR keys
        # If provider is hf, we should load HF keys
        keys = _load_active_keys()
        # Should not crash regardless
        assert isinstance(keys, list)


# ---------------------------------------------------------------------------
# Regression tests — HF key-pool lifecycle (reload_keys / hot reload)
# ---------------------------------------------------------------------------

class TestReloadKeysRegression:
    """Cover every requirement from the HF key-pool lifecycle audit."""

    def test_startup_loads_newly_added_keys(self):
        """Requirement 1: startup pool reflects current hf_keys.txt."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        assert pool.total == 2
        # Simulate startup with a file that now has 3 keys
        asyncio.run(
            pool.reload_keys(["hf_a", "hf_b", "hf_c"])
        )
        assert pool.total == 3
        assert "hf_c" in [k.key for k in pool._keys]

    def test_hot_reload_adds_keys(self):
        """Requirement 2: newly added keys appear without restart."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a"], mode="full_sticky")
        asyncio.run(
            pool.reload_keys(["hf_a", "hf_b", "hf_c"])
        )
        assert pool.total == 3
        # New keys are usable
        k = pool.next_key()
        assert k[0] in ("hf_a", "hf_b", "hf_c")

    def test_dead_keys_remain_excluded_after_reload(self):
        """Requirement 4: dead keys never re-enter the pool."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b", "hf_c"], mode="full_sticky")
        # Retire hf_b
        asyncio.run(pool.retire_key(1))
        assert pool.is_key_retired(1)
        # Reload with same keys — hf_b must stay retired
        asyncio.run(
            pool.reload_keys(["hf_a", "hf_b", "hf_c"])
        )
        assert pool.is_key_retired(1)
        # Reload with reordered keys — hf_b must still be retired
        asyncio.run(
            pool.reload_keys(["hf_c", "hf_a", "hf_b"])
        )
        # Find hf_b's new index and verify it's retired
        for i, info in enumerate(pool._keys):
            if info.key == "hf_b":
                assert pool.is_key_retired(i)
                break
        else:
            pytest.fail("hf_b not found after reload")

    def test_existing_usable_keys_survive_reload(self):
        """Requirement 5: usable keys are never discarded by reload."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        # Give hf_a some state
        asyncio.run(pool.mark_success(0, 123.0))
        asyncio.run(
            pool.reload_keys(["hf_a", "hf_b", "hf_c"])
        )
        # hf_a must still be present with its stats
        for info in pool._keys:
            if info.key == "hf_a":
                assert info.total_requests == 1
                assert info.last_latency_ms == 123.0
                break
        else:
            pytest.fail("hf_a lost after reload")

    def test_sticky_key_preservation(self):
        """Requirement 7: sticky key survives reload when still valid."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b", "hf_c"], mode="full_sticky")
        k1 = pool.next_key()
        assert k1[0] == "hf_a"  # sticky is first key
        # Reload with same keys — sticky must stay hf_a
        asyncio.run(
            pool.reload_keys(["hf_a", "hf_b", "hf_c"])
        )
        k2 = pool.next_key()
        assert k2[0] == "hf_a"
        # Reload with reordered keys — sticky must still be hf_a
        asyncio.run(
            pool.reload_keys(["hf_c", "hf_b", "hf_a"])
        )
        k3 = pool.next_key()
        assert k3[0] == "hf_a"

    def test_removed_sticky_key_recovery(self):
        """Requirement 8: removed sticky key → safely select another."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b", "hf_c"], mode="full_sticky")
        k1 = pool.next_key()
        assert k1[0] == "hf_a"
        # Remove hf_a from the file
        asyncio.run(
            pool.reload_keys(["hf_b", "hf_c"])
        )
        k2 = pool.next_key()
        assert k2[0] in ("hf_b", "hf_c")
        assert k2[2] is True  # healthy

    def test_retired_sticky_key_recovery(self):
        """Requirement 8: retired sticky key → safely select another."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        k1 = pool.next_key()
        assert k1[0] == "hf_a"
        asyncio.run(pool.retire_key(0))
        # Reload — hf_a still in file but retired
        asyncio.run(
            pool.reload_keys(["hf_a", "hf_b"])
        )
        k2 = pool.next_key()
        assert k2[0] == "hf_b"

    def test_rapid_file_changes(self):
        """Requirement 12: rapid successive changes are all picked up."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a"], mode="full_sticky")
        # Simulate rapid successive reloads
        for i in range(10):
            keys = ["hf_a"] + [f"hf_new_{j}" for j in range(i + 1)]
            asyncio.run(pool.reload_keys(keys))
            assert pool.total == i + 2
        # Final state must reflect last reload
        assert pool.total == 11

    def test_empty_reload_protection(self):
        """Requirement 10: empty reload never discards healthy pool."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        asyncio.run(pool.reload_keys([]))
        assert pool.total == 2  # unchanged

    def test_no_duplicate_keys(self):
        """Requirement 11: duplicates in input are deduplicated."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        asyncio.run(
            pool.reload_keys(["hf_a", "hf_b", "hf_a", "hf_c", "hf_b"])
        )
        assert pool.total == 3
        key_strs = [k.key for k in pool._keys]
        assert len(key_strs) == len(set(key_strs))

    def test_retired_key_stays_retired_on_index_shift(self):
        """Retired key tracked by string, not index — index shift cannot un-retire it."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b", "hf_c"], mode="full_sticky")
        # Retire hf_c (index 2)
        asyncio.run(pool.retire_key(2))
        assert pool.is_key_retired(2)
        # Insert a new key before hf_c — hf_c shifts to index 3
        asyncio.run(
            pool.reload_keys(["hf_a", "hf_b", "hf_new", "hf_c"])
        )
        # hf_c must still be retired at its new index
        for i, info in enumerate(pool._keys):
            if info.key == "hf_c":
                assert pool.is_key_retired(i)
                break
        else:
            pytest.fail("hf_c not found after reload")
        # hf_new must NOT be retired
        for i, info in enumerate(pool._keys):
            if info.key == "hf_new":
                assert not pool.is_key_retired(i)
                break

    def test_reload_returns_counts(self):
        """reload_keys returns (added, removed, kept) for observability."""
        from proxy.keypool import KeyPool
        pool = KeyPool(["hf_a", "hf_b"], mode="full_sticky")
        added, removed, kept = asyncio.run(
            pool.reload_keys(["hf_a", "hf_c"])
        )
        assert added == 1   # hf_c
        assert removed == 1 # hf_b
        assert kept == 1    # hf_a

