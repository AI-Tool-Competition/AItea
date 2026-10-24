import hashlib
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from app.shared.infrastructure.persistence.blob_store import BlobIntegrityError, BlobStore


def blob_path(files_dir, owner, content):
    digest = hashlib.sha256(content).hexdigest()
    return (
        files_dir / "blobs" / hashlib.sha256(owner.encode()).hexdigest() / digest[:2] / digest[2:4] / digest
    )


def test_stream_is_only_published_when_complete_and_preserves_legacy_files(tmp_path):
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    legacy = files_dir / "legacy-document.pdf"
    legacy.write_bytes(b"legacy")
    store = BlobStore(files_dir)
    content = b"first-second"
    destination = blob_path(files_dir, "alice", content)

    def chunks():
        yield b"first-"
        assert not destination.exists()
        assert len(list((files_dir / ".staging").iterdir())) == 1
        yield b"second"

    result = store.put("alice", chunks())

    assert result.path == destination
    assert result.digest == hashlib.sha256(content).hexdigest()
    assert result.size == len(content)
    assert result.path.read_bytes() == content
    assert stat.S_IMODE(result.path.stat().st_mode) & 0o077 == 0
    for parent in result.path.parents:
        if parent == files_dir:
            break
        assert stat.S_IMODE(parent.stat().st_mode) & 0o077 == 0
    assert not list((files_dir / ".staging").iterdir())
    assert legacy.read_bytes() == b"legacy"


def test_owners_are_isolated_and_cannot_create_paths_from_their_names(tmp_path):
    store = BlobStore(tmp_path / "files")
    first = store.put_bytes("../../outside", b"same bytes")
    second = store.put_bytes("alice", b"same bytes")

    assert first.path != second.path
    assert first.digest == second.digest
    assert first.path.is_relative_to(tmp_path / "files" / "blobs")
    assert first.path.stat().st_ino != second.path.stat().st_ino


def test_concurrent_threads_publish_the_same_blob_once(tmp_path):
    files_dir = tmp_path / "files"
    store = BlobStore(files_dir)
    barrier = Barrier(8)

    def publish(_):
        def chunks():
            yield b"shared-"
            barrier.wait(timeout=10)
            yield b"content"

        return store.put("alice", chunks())

    with ThreadPoolExecutor(max_workers=8) as pool:
        blobs = list(pool.map(publish, range(8)))

    assert len({blob.path for blob in blobs}) == 1
    assert blobs[0].path.read_bytes() == b"shared-content"
    assert len([path for path in (files_dir / "blobs").rglob("*") if path.is_file()]) == 1
    assert not list((files_dir / ".staging").iterdir())


def _publish_in_process(files_dir, barrier, results):
    def chunks():
        yield b"process-"
        barrier.wait(timeout=15)
        yield b"content"

    try:
        blob = BlobStore(Path(files_dir)).put("alice", chunks())
        results.put((str(blob.path), blob.path.stat().st_ino, None))
    except BaseException as error:
        results.put((None, None, repr(error)))


def test_concurrent_processes_publish_the_same_blob_once(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    results = context.Queue()
    files_dir = tmp_path / "files"
    processes = [
        context.Process(target=_publish_in_process, args=(str(files_dir), barrier, results)) for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        received = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        assert all(error is None for _, _, error in received), received
        assert len({(path, inode) for path, inode, _ in received}) == 1
        assert Path(received[0][0]).read_bytes() == b"process-content"
        assert not list((files_dir / ".staging").iterdir())
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("failure", [RuntimeError("download interrupted"), KeyboardInterrupt()])
def test_interrupted_stream_removes_only_its_own_temporary_file(tmp_path, failure):
    files_dir = tmp_path / "files"
    store = BlobStore(files_dir)
    existing = store.put_bytes("alice", b"already published")
    other_pending = files_dir / ".staging" / "another-workers-file"
    other_pending.write_bytes(b"in progress")

    def chunks():
        yield b"unfinished"
        raise failure

    with pytest.raises(type(failure)):
        store.put("alice", chunks())

    assert existing.path.read_bytes() == b"already published"
    assert list((files_dir / ".staging").iterdir()) == [other_pending]
    assert len([path for path in (files_dir / "blobs").rglob("*") if path.is_file()]) == 1


@pytest.mark.parametrize("chunks", [[], [b""], [b"", b""]])
def test_empty_content_is_rejected_without_publishing(tmp_path, chunks):
    files_dir = tmp_path / "files"
    with pytest.raises(ValueError, match="empty"):
        BlobStore(files_dir).put("alice", chunks)
    assert not list((files_dir / ".staging").iterdir())
    assert not list((files_dir / "blobs").rglob("*"))


def test_size_limit_is_checked_while_reading_and_stops_consuming_stream(tmp_path):
    files_dir = tmp_path / "files"
    store = BlobStore(files_dir, max_bytes=4)

    def chunks():
        yield b"1234"
        yield b"5"
        pytest.fail("an oversized stream must not be consumed further")

    with pytest.raises(ValueError, match="size"):
        store.put("alice", chunks())
    assert not list((files_dir / ".staging").iterdir())
    assert store.put_bytes("alice", b"1234").size == 4


@pytest.mark.parametrize("chunk", ["text", None, 1, bytearray(b"mutable")])
def test_non_bytes_chunks_are_rejected_without_publishing(tmp_path, chunk):
    files_dir = tmp_path / "files"
    with pytest.raises(TypeError, match="bytes"):
        BlobStore(files_dir).put("alice", [b"valid", chunk])
    assert not list((files_dir / ".staging").iterdir())


def test_existing_corrupt_blob_is_rejected_and_never_overwritten(tmp_path):
    files_dir = tmp_path / "files"
    store = BlobStore(files_dir)
    original = store.put_bytes("alice", b"law-v1")
    os.chmod(original.path, 0o600)
    original.path.write_bytes(b"broken")  # Same length, different digest.

    with pytest.raises(BlobIntegrityError, match="integrity"):
        store.put_bytes("alice", b"law-v1")

    assert original.path.read_bytes() == b"broken"
    assert not list((files_dir / ".staging").iterdir())


def test_dedup_tolerates_another_publisher_removing_its_staging_link(tmp_path, monkeypatch):
    files_dir = tmp_path / "files"
    store = BlobStore(files_dir)
    original = store.put_bytes("alice", b"law")
    pending_link = files_dir / ".staging" / "previous-publisher.part"
    os.link(original.path, pending_link)
    inode = original.path.stat().st_ino
    real_fstat = os.fstat

    def finish_other_publisher(fd):
        result = real_fstat(fd)
        if result.st_ino == inode and pending_link.exists():
            pending_link.unlink()
        return result

    monkeypatch.setattr(os, "fstat", finish_other_publisher)

    assert store.put_bytes("alice", b"law") == original
    assert not list((files_dir / ".staging").iterdir())


def test_existing_symlink_is_rejected_without_modifying_its_target(tmp_path):
    files_dir = tmp_path / "files"
    store = BlobStore(files_dir)
    destination = blob_path(files_dir, "alice", b"law")
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"law")
    destination.symlink_to(outside)

    with pytest.raises(BlobIntegrityError):
        store.put_bytes("alice", b"law")

    assert destination.is_symlink()
    assert outside.read_bytes() == b"law"
    assert not list((files_dir / ".staging").iterdir())


def test_symlinked_owner_directory_does_not_receive_any_blob_files(tmp_path):
    files_dir = tmp_path / "files"
    store = BlobStore(files_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    owner_directory = files_dir / "blobs" / hashlib.sha256(b"alice").hexdigest()
    owner_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="real directories"):
        store.put_bytes("alice", b"law")

    assert not list(outside.iterdir())
    assert not list((files_dir / ".staging").iterdir())


def test_failed_disk_sync_does_not_publish_partial_content(tmp_path, monkeypatch):
    files_dir = tmp_path / "files"
    store = BlobStore(files_dir)
    real_fsync = os.fsync

    def fail_file_sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("simulated disk failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_file_sync)
    with pytest.raises(OSError, match="disk failure"):
        store.put_bytes("alice", b"law")
    assert not blob_path(files_dir, "alice", b"law").exists()
    assert not list((files_dir / ".staging").iterdir())
