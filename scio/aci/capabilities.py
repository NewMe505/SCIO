"""Tool capability tags and the compile-time "lethal trifecta" check.

An agent that can read private data, ingest untrusted input and send data out can be
turned into an exfiltration channel by a prompt injection in that input. SCIO refuses to
assemble such a toolset unless a human gate stands between the agent and the outside
world (the merge interrupt, for example).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

Capability = Literal["private_data", "untrusted_input", "exfiltration", "mutation"]

TOOL_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "repo_search": frozenset({"private_data"}),
    "view_file": frozenset({"private_data"}),
    "edit_file": frozenset({"private_data", "mutation"}),
    "run_tests": frozenset({"private_data", "untrusted_input"}),
    "git_checkpoint": frozenset({"mutation"}),
    "git_push": frozenset({"mutation", "exfiltration"}),
    "github_write": frozenset({"exfiltration"}),
    "web_fetch": frozenset({"untrusted_input", "exfiltration"}),
}

TRIFECTA: frozenset[Capability] = frozenset({"private_data", "untrusted_input", "exfiltration"})


class UnsafeToolsetError(ValueError):
    pass


def capabilities_of(
    tool_names: Iterable[str], extra: Mapping[str, Iterable[Capability]] | None = None
) -> set[Capability]:
    table: dict[str, frozenset[Capability]] = dict(TOOL_CAPABILITIES)
    for name, caps in (extra or {}).items():
        table[name] = frozenset(caps)
    found: set[Capability] = set()
    for name in tool_names:
        if name not in table:
            raise UnsafeToolsetError(f"tool {name!r} has no capability tags; tag it before use")
        found |= table[name]
    return found


def check_toolset(
    tool_names: Iterable[str],
    *,
    human_gate: bool,
    extra: Mapping[str, Iterable[Capability]] | None = None,
) -> set[Capability]:
    """Return the toolset's capabilities; raise if it is the trifecta without a human gate."""
    names = list(tool_names)
    caps = capabilities_of(names, extra)
    if caps >= TRIFECTA and not human_gate:
        raise UnsafeToolsetError(
            "refusing to compile an agent holding private-data + untrusted-input + exfiltration "
            f"tools ({sorted(names)}) without a human gate"
        )
    return caps
