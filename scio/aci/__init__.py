"""The agent-computer interface: sandboxed tools with terse, truncated, hint-bearing output.

Built before any agent (SWE-agent finding): ``repo_search``, ``view_file``, ``edit_file``,
``run_tests``, ``git_checkpoint``. Tool output is untrusted data, never instructions.
"""

from scio.aci.capabilities import TOOL_CAPABILITIES, UnsafeToolsetError, check_toolset
from scio.aci.sandbox.base import ExecResult, Limits, PathEscapeError, Sandbox
from scio.aci.toolbox import ToolBox
from scio.aci.tools import ToolError
from scio.aci.truncate import truncate

__all__ = [
    "TOOL_CAPABILITIES",
    "ExecResult",
    "Limits",
    "PathEscapeError",
    "Sandbox",
    "ToolBox",
    "ToolError",
    "UnsafeToolsetError",
    "check_toolset",
    "truncate",
]
