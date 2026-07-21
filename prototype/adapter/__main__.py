"""Command-line entry point for the PyRust DAP proxy."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from prototype.adapter.mixed_stack import MixedStackHooks
    from prototype.adapter.proxy import DapProxy, ProxyHooks
else:
    from .mixed_stack import MixedStackHooks
    from .proxy import DapProxy, ProxyHooks


def _load_hooks(specification: str | None) -> ProxyHooks:
    if not specification:
        return ProxyHooks()
    module_name, separator, attribute_name = specification.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("--hooks must use the form module:attribute")

    module = importlib.import_module(module_name)
    candidate: Any = getattr(module, attribute_name)
    if isinstance(candidate, ProxyHooks):
        hooks = candidate
    elif isinstance(candidate, type):
        hooks = candidate()
    elif callable(candidate):
        hooks = candidate()
    else:
        hooks = candidate
    if not isinstance(hooks, ProxyHooks):
        raise TypeError(f"{specification!r} did not produce ProxyHooks")
    return hooks


def _default_codelldb_command(
    adapter_path: str | None = None,
    liblldb_path: str | None = None,
) -> list[str]:
    configured_adapter = adapter_path or os.environ.get("PYRUST_CODELLDB")
    configured_liblldb = liblldb_path or os.environ.get("PYRUST_LIBLLDB")
    if bool(configured_adapter) != bool(configured_liblldb):
        raise ValueError(
            "CodeLLDB adapter and liblldb paths must be configured together"
        )
    if configured_adapter and configured_liblldb:
        adapter = Path(configured_adapter).expanduser()
        liblldb = Path(configured_liblldb).expanduser()
        if not adapter.is_file():
            raise ValueError(f"configured CodeLLDB adapter does not exist: {adapter}")
        if not liblldb.is_file():
            raise ValueError(f"configured liblldb does not exist: {liblldb}")
        return _codelldb_command_with_pyrust_settings(adapter, liblldb)

    extension_root = Path.home() / ".vscode-server" / "extensions"
    candidates = sorted(extension_root.glob("vadimcn.vscode-lldb-1.12.2*"))
    for extension in reversed(candidates):
        adapter = extension / "adapter" / "codelldb"
        liblldb = extension / "lldb" / "lib" / "liblldb.so"
        if adapter.is_file() and liblldb.is_file():
            return _codelldb_command_with_pyrust_settings(adapter, liblldb)
    raise ValueError("CodeLLDB 1.12.2 platform package is not installed")


def _codelldb_command_with_pyrust_settings(
    adapter: Path,
    liblldb: Path,
) -> list[str]:
    """Supply settings normally injected by CodeLLDB's VS Code extension."""

    settings = json.dumps({"consoleMode": "evaluate"}, separators=(",", ":"))
    return [
        str(adapter),
        "--liblldb",
        str(liblldb),
        "--settings",
        settings,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Relay stdio DAP traffic to a child CodeLLDB adapter",
    )
    parser.add_argument(
        "--hooks",
        default=os.environ.get("PYRUST_DAP_HOOKS"),
        help="optional ProxyHooks provider as module:attribute",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=5.0,
        help="seconds allowed for hook-generated downstream requests",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=2.0,
        help="seconds allowed for each downstream shutdown phase",
    )
    parser.add_argument(
        "--downstream-cwd",
        help="working directory for the downstream adapter",
    )
    parser.add_argument(
        "--codelldb",
        default=os.environ.get("PYRUST_CODELLDB"),
        help="explicit path to the CodeLLDB adapter executable",
    )
    parser.add_argument(
        "--liblldb",
        default=os.environ.get("PYRUST_LIBLLDB"),
        help="explicit path to CodeLLDB's bundled liblldb",
    )
    parser.add_argument(
        "downstream_command",
        nargs=argparse.REMAINDER,
        help="downstream adapter command, normally following --",
    )
    args = parser.parse_args()

    command = args.downstream_command
    if command[:1] == ["--"]:
        command = command[1:]
    automatic_configuration = not command
    if automatic_configuration:
        try:
            command = _default_codelldb_command(args.codelldb, args.liblldb)
        except ValueError as error:
            parser.error(str(error))

    try:
        hooks = (
            MixedStackHooks()
            if automatic_configuration and not args.hooks
            else _load_hooks(args.hooks)
        )
        downstream_environment = os.environ.copy()
        downstream_environment["DEBUGINFOD_URLS"] = ""
        proxy = DapProxy(
            command,
            hooks=hooks,
            request_timeout=args.request_timeout,
            shutdown_timeout=args.shutdown_timeout,
            downstream_cwd=args.downstream_cwd
            or (str(Path(__file__).resolve().parents[2]) if automatic_configuration else None),
            downstream_env=downstream_environment,
        )
        return proxy.run()
    except (ImportError, AttributeError, TypeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
