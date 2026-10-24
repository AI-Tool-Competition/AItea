import pytest
from app.contexts.knowledge.infrastructure.parsing.parsing import parse_bounded


def _sleeping_worker(*args):
    import time

    time.sleep(10)


def test_normal_document_parses_in_an_isolated_process():
    result = parse_bounded("tea.md", "# 产品资料\n\n乌龙茶每袋净含量250克。".encode())
    assert result["chunks"]
    assert "250" in result["chunks"][0]["text"]


def test_parser_failure_is_returned_as_a_controlled_error():
    with pytest.raises(ValueError, match="格式.*支持"):
        parse_bounded("unknown.unsupported", b"unrecognized format")


def test_secondary_attachment_cannot_start_a_parser_process():
    with pytest.raises(ValueError, match="secondary"):
        parse_bounded("attachment.txt", b"stored only", role="secondary")


def test_empty_upload_is_rejected_before_process_creation():
    with pytest.raises(ValueError, match="不能为空"):
        parse_bounded("empty.txt", b"")


def test_a_stalled_parser_is_terminated_and_capacity_is_released(monkeypatch):
    import app.contexts.knowledge.infrastructure.parsing.parsing as parsing

    original = parsing._parse_worker
    monkeypatch.setenv("TEA_PARSE_TIMEOUT", "0.1")
    monkeypatch.setattr(parsing, "_parse_worker", _sleeping_worker)
    with pytest.raises(ValueError, match="超时"):
        parse_bounded("slow.txt", b"slow parser")
    monkeypatch.setattr(parsing, "_parse_worker", original)
    monkeypatch.setenv("TEA_PARSE_TIMEOUT", "45")
    assert parse_bounded("next.txt", "下一份材料正常处理。".encode())["chunks"]


def test_pipe_initialization_failure_does_not_leak_worker_slots(monkeypatch):
    from threading import BoundedSemaphore
    from types import SimpleNamespace

    import app.contexts.knowledge.infrastructure.parsing.parsing as parsing

    slots = BoundedSemaphore(2)
    monkeypatch.setattr(parsing, "_SLOTS", slots)

    def unavailable_pipe(**_kwargs):
        raise OSError("too many open files")

    monkeypatch.setattr(
        parsing.multiprocessing, "get_context", lambda *_args: SimpleNamespace(Pipe=unavailable_pipe)
    )
    with pytest.raises(OSError):
        parse_bounded("tea.txt", b"document")
    assert slots.acquire(blocking=False)
    assert slots.acquire(blocking=False)
