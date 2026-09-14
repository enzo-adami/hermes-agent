"""The built-in ``deepseek`` alias must resolve to a live DeepSeek model.

DeepSeek retired ``deepseek-chat`` / ``deepseek-reasoner`` on 2026-07-24.
The alias table used to point at the ``deepseek-chat`` family, so ``/model
deepseek`` found nothing on the native provider (its catalog only carries
``deepseek-v4-*`` IDs) and, on OpenRouter, tripped the ambiguity guard
between three V3-era ``deepseek/deepseek-chat*`` slugs.  The alias now names
the flagship tier explicitly.
"""

from unittest.mock import patch

import pytest

import hermes_cli.model_switch as model_switch
from hermes_cli.models import _PROVIDER_MODELS, _PROVIDER_RETIRED_ALIASES

_NATIVE_CATALOG = ["deepseek-v4-flash-vision-exp", "deepseek-v4-flash", "deepseek-v4-pro"]
_OPENROUTER_CATALOG = [
    "deepseek/deepseek-chat",
    "deepseek/deepseek-chat-v3.1",
    "deepseek/deepseek-chat-v3-0324",
    "deepseek/deepseek-r1",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
]


def _resolve(current_provider: str, catalog: list[str]):
    # Attribute access at call time: other suites ``importlib.reload`` this
    # module, which would leave names imported here pointing at stale objects.
    with (
        patch("hermes_cli.model_switch.list_provider_models", return_value=list(catalog)),
        patch("hermes_cli.model_switch._ensure_direct_aliases"),
    ):
        return model_switch.resolve_alias("deepseek", current_provider)


class TestDeepSeekAlias:
    def test_alias_family_is_not_a_retired_id(self):
        family = model_switch.MODEL_ALIASES["deepseek"].family
        assert family not in _PROVIDER_RETIRED_ALIASES["deepseek"]
        assert family in _PROVIDER_MODELS["deepseek"]

    def test_native_provider_resolves_to_v4_pro(self):
        assert _resolve("deepseek", _NATIVE_CATALOG) == ("deepseek", "deepseek-v4-pro", "deepseek")

    def test_native_provider_static_catalog_fallback(self):
        """Offline (no live /models), the static catalog alone must resolve."""
        assert _resolve("deepseek", []) == ("deepseek", "deepseek-v4-pro", "deepseek")

    def test_openrouter_resolves_to_v4_pro_not_v3_chat(self):
        assert _resolve("openrouter", _OPENROUTER_CATALOG) == (
            "openrouter", "deepseek/deepseek-v4-pro", "deepseek",
        )

    def test_openrouter_dated_snapshot_still_asks_the_user(self):
        """A dated Pro snapshot alongside the canonical slug is a real choice."""
        with pytest.raises(model_switch.AmbiguousAliasError):
            _resolve("openrouter", _OPENROUTER_CATALOG + ["deepseek/deepseek-v4-pro-0813"])
