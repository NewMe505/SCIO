"""Black-box acceptance against the running product (M9, ADR 0013).

A product keeps its acceptance tests under ``tests/acceptance/``; they talk to the product
from the outside (a subprocess for a CLI, HTTP for a server) and never import its internals
beyond the version. ``run_acceptance`` starts an ``http`` product on a free port with the
profile's app command, waits for its health path, runs the acceptance tests with ``APP_URL``
set, stops the server, and reports. A ``cli`` product's tests run as they are. The result
is one more line of the integration text that the milestone gate reads and any failure
overrides.

Where the app runs follows the sandbox (``scio.aci.apps``, shared with the ``run_app`` worker
tool): on a local sandbox it is a host subprocess; on any other (Docker) it is started inside
the sandbox with the profile's shell-less commands, its health probed from inside, and
stopped by pid, so the acceptance tests and the app share one network namespace and no port
leaves the container.
"""

from __future__ import annotations

from scio.aci.apps import (
    HEALTH_TIMEOUT_S,
    RunningApp,
    free_port,
    start_app,
    wait_healthy,
)
from scio.graph.run_context import RunContext
from scio.state.models import ProductSpec
from scio.toolchain import Toolchain

# re-exported: the eval solver and the tests import the app helpers from here (M9)
__all__ = [
    "HEALTH_TIMEOUT_S",
    "RunningApp",
    "app_argv",
    "free_port",
    "run_acceptance",
    "start_app",
    "wait_healthy",
]


def app_argv(spec: ProductSpec, tc: Toolchain, port: int | None = None) -> list[str]:
    """The command that starts the product the way a user does: the profile's ``app_argv``
    and target, then the ``## Runtime`` arguments with ``{port}`` filled in (none for a CLI,
    whose arguments are the test's to choose)."""
    target = [*tc.runtime().app_argv, tc.app_target(spec.name, spec.app.module)]
    if port is None:
        return target
    return [*target, *[a.replace("{port}", str(port)) for a in spec.app.args]]


def run_acceptance(ctx: RunContext, spec: ProductSpec) -> tuple[str, bool]:
    sb, tc = ctx.sandbox, ctx.toolchain
    acc_dir = f"{sb.root}/{tc.acceptance_dir}"
    if not sb.exists(acc_dir):
        return f"[acceptance] skipped: no {tc.acceptance_dir}", True
    env: dict[str, str] = {}
    app: RunningApp | None = None
    label = "cli"
    if spec.app.kind == "http":
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        argv = app_argv(spec, tc, port)
        app = start_app(sb, argv, url, tc)
        if not wait_healthy(app, spec.app.health_path):
            output = app.stop()
            return (
                f"[acceptance] FAILED: the app did not answer {spec.app.health_path} on {url} "
                f"within {HEALTH_TIMEOUT_S}s ({' '.join(argv[1:])})\n{output[-1500:]}",
                False,
            )
        env["APP_URL"] = url
        label = f"http {url}"
    try:
        r = sb.exec(tc.test_argv(filter=None, path=acc_dir), timeout_s=sb.limits.timeout_s, env=env)
    finally:
        app_output = app.stop() if app is not None else ""
    outcome: str = tc.test_outcome(r.exit_code, r.stdout)
    if outcome == "passed" and tc.skipped_only(r.stdout):
        outcome = "skipped only"  # every acceptance test skipped proves nothing (I-097)
    ok = outcome == "passed"
    tail = r.output.strip().splitlines()[-1][:200] if r.output.strip() else ""
    text = f"[acceptance] {outcome} ({label}): {tail}"
    if not ok:
        text += f"\n{r.output[-1500:]}"
        if app_output.strip():
            text += f"\n[app output]\n{app_output[-800:]}"
    return text, ok
