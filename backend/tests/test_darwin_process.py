"""Native macOS identity and argument boundaries, without exposing credentials."""

import ctypes
import errno
import os
import subprocess
import sys

import pytest
from app.configuration.paths import PROJECT_ROOT
from app.shared.infrastructure import darwin_process as native
from app.contexts.documents.infrastructure.providers.embedding_admission import SharedEmbeddingBudget


def test_argument_boundaries_preserve_spaces_empty_arguments_and_equals():
    argv = [b"python3", b"script with spaces.py", b"", "茶叶".encode()]
    raw = bytes(ctypes.c_int(len(argv))) + b"/usr/bin/python3\0\0\0"
    raw += b"\0".join(argv) + b"\0TEA_DEV_ROOT=/private/tmp/with spaces\0TEA_DEV_TOKEN=a=b=c\0\0"
    cmdline, environment = native._decode_arguments(raw)
    assert cmdline.split("\0")[:-1] == [argument.decode() for argument in argv]
    assert environment == {b"TEA_DEV_ROOT": b"/private/tmp/with spaces", b"TEA_DEV_TOKEN": b"a=b=c"}


@pytest.mark.parametrize("raw", [b"", bytes(ctypes.c_int(-1)), bytes(ctypes.c_int(2)) + b"python\0\0arg\0"])
def test_incomplete_arguments_cannot_prove_ownership(raw):
    with pytest.raises(ValueError):
        native._decode_arguments(raw)


@pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS APIs")
def test_unreadable_process_cannot_inherit_parent_credentials(monkeypatch):
    monkeypatch.setenv("TEA_DEV_ROOT", "/parent")
    monkeypatch.setenv("TEA_DEV_TOKEN", "parent-token")

    def denied(*args):
        raise PermissionError(errno.EPERM, "process inspection denied")

    monkeypatch.setattr(native, "_arguments", denied)
    assert native.process_identity(os.getpid()) is None
    monkeypatch.setattr(native, "_pidinfo", denied)
    with pytest.raises(PermissionError):
        native.process_start(os.getpid())


@pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS APIs")
def test_embedding_budget_keeps_live_foreign_process_slot_and_reclaims_exit(tmp_path):
    path = tmp_path / "admission.sqlite3"
    parent = SharedEmbeddingBudget(path, "native-mac-test", 1)
    script = """
import sys,time
from app.contexts.documents.infrastructure.providers.embedding_admission import SharedEmbeddingBudget
budget=SharedEmbeddingBudget(sys.argv[1], 'native-mac-test', 1)
assert budget.try_acquire('foreign-process', 'query')
print('acquired', flush=True)
time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        text=True,
        cwd=PROJECT_ROOT / "backend",
    )
    try:
        assert child.stdout.readline().strip() == "acquired"
        assert parent.snapshot()["active"]["query"] == 1
        assert not parent.try_acquire("parent-waiting", "query")
        started = native.process_start(child.pid)
        assert started and started != native.process_start(os.getpid())
        child.terminate()
        child.wait(timeout=5)
        assert parent.try_acquire("parent-waiting", "query")
        assert parent.snapshot()["reclaimed"] >= 1
        parent.release("parent-waiting")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
