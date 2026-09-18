"""The model layer.

`LLMProvider` is the only surface the agent runtime talks to. Four adapters implement it:

| Mode        | Used by                                    | Network | Cost |
|-------------|--------------------------------------------|---------|------|
| `replay`    | `make ci`, `make eval`, `make repro`, CI   | no      | none |
| `stub`      | unit tests                                 | no      | none |
| `anthropic` | a live run                                 | yes     | yes  |
| `record`    | `make record`                              | yes     | yes  |

`replay` is the default, which is what makes CI offline, free and byte-identical.
"""

from tda.agents.provider.base import (
    KEY_SCHEMA_VERSION,
    CassetteMissError,
    Effort,
    LLMProvider,
    Message,
    ModelRequest,
    ModelResponse,
    OutputValidationError,
    ProviderError,
    ProviderMode,
    TokenUsage,
)
from tda.agents.provider.replay import (
    DEFAULT_CASSETTE_DIR,
    ReplayProvider,
    cassette_path,
    write_cassette,
)
from tda.agents.provider.stub import StubProvider

__all__ = [
    "DEFAULT_CASSETTE_DIR",
    "KEY_SCHEMA_VERSION",
    "CassetteMissError",
    "Effort",
    "LLMProvider",
    "Message",
    "ModelRequest",
    "ModelResponse",
    "OutputValidationError",
    "ProviderError",
    "ProviderMode",
    "ReplayProvider",
    "StubProvider",
    "TokenUsage",
    "cassette_path",
    "write_cassette",
]


def anthropic_provider(**kwargs: object) -> LLMProvider:
    """Construct the live adapter.

    A function rather than a re-export, so importing this package never imports the anthropic SDK.
    CI installs the extra but runs in replay; a module-level import would make the whole provider
    package unimportable wherever the extra is absent, taking the replay path down with it.
    """
    from tda.agents.provider.anthropic_client import AnthropicProvider

    return AnthropicProvider(**kwargs)  # type: ignore[arg-type]
