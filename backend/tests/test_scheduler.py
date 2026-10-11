from app.scheduler import ScheduledCollector


class SourceService:
    def __init__(self):
        self.calls = []

    def due_sources(self):
        return [{"owner_id": "alice", "id": "broken"}, {"owner_id": "bob", "id": "good"}]

    def fetch_source(self, owner_id, source_id):
        self.calls.append((owner_id, source_id))
        if source_id == "broken":
            raise RuntimeError("source unavailable")


def test_a_failed_source_does_not_prevent_another_owner_being_collected():
    service = SourceService()
    result = ScheduledCollector(service).run_once()
    assert service.calls == [("alice", "broken"), ("bob", "good")]
    assert result == {"succeeded": 1, "failed": 1}


def test_stopped_collector_never_fetches_more_sources():
    service = SourceService()
    collector = ScheduledCollector(service)
    collector.stop()
    assert collector.run_once() == {"succeeded": 0, "failed": 0}
    assert service.calls == []


def test_database_failure_is_reported_without_crashing_the_scheduler():
    class BrokenStore(SourceService):
        def due_sources(self):
            raise RuntimeError("database temporarily busy")

    assert ScheduledCollector(BrokenStore()).run_once() == {"succeeded": 0, "failed": 1}


def test_one_cycle_has_a_bounded_batch_size():
    class ManySources(SourceService):
        def due_sources(self):
            return [{"owner_id": "alice", "id": str(i)} for i in range(100)]

    service = ManySources()
    ScheduledCollector(service, batch_size=3).run_once()
    assert len(service.calls) == 3
