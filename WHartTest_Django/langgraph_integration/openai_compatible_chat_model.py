from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


def repair_tool_call_chunks(
    message: AIMessageChunk, tool_call_ids: dict[Any, str]
) -> bool:
    """Keep tool-call fragments associated with the first valid call ID."""
    tool_call_chunks = list(getattr(message, "tool_call_chunks", None) or [])
    if not tool_call_chunks:
        return False

    named_calls_by_index = {
        call.get("index"): call
        for call in tool_call_chunks
        if isinstance(call, dict) and (call.get("id") or call.get("name"))
    }
    repaired_chunks = []
    changed = False

    for call in tool_call_chunks:
        if not isinstance(call, dict):
            repaired_chunks.append(call)
            continue

        repaired_call = dict(call)
        index = repaired_call.get("index")
        call_id = repaired_call.get("id")
        call_name = repaired_call.get("name")
        named_call = named_calls_by_index.get(index)

        if call_id:
            tool_call_ids[index] = str(call_id)
        elif not call_name and named_call and repaired_call.get("args") == named_call.get("args"):
            # A complete duplicate appears in the same provider chunk. Dropping it
            # prevents LangGraph from creating a second ToolMessage without an ID.
            changed = True
            continue
        elif not call_name and index in tool_call_ids:
            # This is a later argument fragment. It must retain the original ID so
            # LangChain can merge it with the first name-bearing fragment.
            repaired_call["id"] = tool_call_ids[index]
            changed = True

        repaired_chunks.append(repaired_call)

    if not changed:
        return False

    message.tool_call_chunks = repaired_chunks
    # AIMessageChunk is re-parsed from tool_call_chunks when LangChain combines
    # fragments. Do not expose malformed per-fragment parsed calls meanwhile.
    message.tool_calls = [
        call
        for call in (getattr(message, "tool_calls", None) or [])
        if call.get("id") and call.get("name")
    ]
    message.invalid_tool_calls = []
    return True


class ToolCallCompatibleChatOpenAI(ChatOpenAI):
    """OpenAI-compatible model adapter for malformed streamed tool calls."""

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        tool_call_ids: dict[Any, str] = {}
        for generation_chunk in super()._stream(*args, **kwargs):
            message = generation_chunk.message
            if isinstance(message, AIMessageChunk):
                repair_tool_call_chunks(message, tool_call_ids)
            yield generation_chunk

    async def _astream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Apply the same repair to the async path used by LangGraph agents."""
        tool_call_ids: dict[Any, str] = {}
        async for generation_chunk in super()._astream(*args, **kwargs):
            message = generation_chunk.message
            if isinstance(message, AIMessageChunk):
                repair_tool_call_chunks(message, tool_call_ids)
            yield generation_chunk
