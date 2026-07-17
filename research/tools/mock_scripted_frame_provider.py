"""One-frame LLDB provider used only to test CodeLLDB DAP presentation."""

from __future__ import annotations

import lldb
from lldb.plugins.scripted_frame_provider import ScriptedFrameProvider
from lldb.plugins.scripted_process import ScriptedFrame


class MockPythonFrame(ScriptedFrame):
    def __init__(self, thread, source_path: str) -> None:
        super().__init__(thread, lldb.SBStructuredData())
        self.source_path = source_path

    def get_id(self):
        return 0

    def get_pc(self):
        return lldb.LLDB_INVALID_ADDRESS

    def get_function_name(self):
        return "[research mock] python_inner"

    def get_symbol_context(self):
        line_entry = lldb.SBLineEntry()
        line_entry.SetFileSpec(lldb.SBFileSpec(self.source_path, True))
        line_entry.SetLine(5)
        line_entry.SetColumn(1)
        symbol_context = lldb.SBSymbolContext()
        symbol_context.SetLineEntry(line_entry)
        return symbol_context

    def is_artificial(self):
        return False

    def is_hidden(self):
        return False

    def get_register_context(self):
        return None


class MockPythonFrameProvider(ScriptedFrameProvider):
    """Prepend one fake Python source frame, then preserve every native frame."""

    def __init__(self, input_frames, args):
        super().__init__(input_frames, args)
        self.source_path = (
            args.GetValueForKey("source_path").GetStringValue(10000)
            if args and args.IsValid()
            else ""
        )

    @staticmethod
    def get_description():
        return "PyRust research mock Python frame provider"

    def get_frame_at_index(self, index):
        if index == 0:
            return MockPythonFrame(self.thread, self.source_path)
        if index - 1 < len(self.input_frames):
            return index - 1
        return None
