"""Proxy hook implementations used only by fake-downstream tests."""

from __future__ import annotations

from typing import Any

from prototype.adapter import LocalResponse, ProxyContext, ProxyHooks


class SliceHooks(ProxyHooks):
    def on_stopped(
        self,
        event: dict[str, Any],
        context: ProxyContext,
    ) -> dict[str, Any]:
        outgoing = dict(event)
        outgoing["body"] = {
            **event.get("body", {}),
            "hookEpoch": context.state.stop_epoch,
        }
        return outgoing

    def on_continued(
        self,
        event: dict[str, Any],
        context: ProxyContext,
    ) -> dict[str, Any]:
        outgoing = dict(event)
        outgoing["body"] = {
            **event.get("body", {}),
            "hookSawStopped": context.state.is_stopped,
        }
        return outgoing

    def on_stack_trace(
        self,
        request: dict[str, Any],
        context: ProxyContext,
    ) -> LocalResponse:
        arguments = request.get("arguments", {})
        thread_id = arguments["threadId"]
        downstream = context.request_downstream(
            "stackTrace",
            {"threadId": thread_id, "startFrame": 0, "levels": 0},
        )
        if not downstream.get("success"):
            return LocalResponse(
                success=False,
                message=downstream.get("message", "downstream stackTrace failed"),
            )

        native_frames = list(downstream["body"]["stackFrames"])
        native_ids = [frame["id"] for frame in native_frames]
        synthetic_id = context.synthetic_frames.allocate(
            thread_id,
            ("python", 0),
            {"name": "python_outer"},
            native_frame_ids=native_ids,
        )
        merged = native_frames + [
            {
                "id": synthetic_id,
                "name": "python_outer",
                "line": 4,
                "column": 1,
                "source": {"path": "/fixture/app.py"},
            }
        ]

        start = arguments.get("startFrame", 0)
        levels = arguments.get("levels")
        end = None if levels is None or levels == 0 else start + levels
        return LocalResponse(
            body={
                "stackFrames": merged[start:end],
                "totalFrames": len(merged),
            }
        )


class TimeoutHooks(ProxyHooks):
    def on_stack_trace(
        self,
        request: dict[str, Any],
        context: ProxyContext,
    ) -> LocalResponse:
        context.request_downstream("neverRespond")
        raise AssertionError("unreachable")
