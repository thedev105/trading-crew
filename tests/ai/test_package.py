from importlib.util import find_spec

from polytrading.ai import AUTHORITY


def test_ai_package_is_research_only_and_has_no_provider_sdk() -> None:
    assert AUTHORITY == "research_only"
    assert find_spec("openai") is None
    assert find_spec("anthropic") is None
