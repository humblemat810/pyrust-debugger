"""Transparent DAP transport used by the first PyRust debugger slice."""

from .framing import DapProtocolError, DapReader, DapStreamParser, DapWriter
from .mixed_stack import MixedStackHooks
from .proxy import (
    DapProxy,
    DownstreamRequestError,
    DownstreamRequestTimeout,
    LocalResponse,
    ProxyContext,
    ProxyHooks,
)
from .state import ProxySessionState, SyntheticFrameRegistry

__all__ = [
    "DapProtocolError",
    "DapProxy",
    "DapReader",
    "DapStreamParser",
    "DapWriter",
    "DownstreamRequestError",
    "DownstreamRequestTimeout",
    "LocalResponse",
    "MixedStackHooks",
    "ProxyContext",
    "ProxyHooks",
    "ProxySessionState",
    "SyntheticFrameRegistry",
]
