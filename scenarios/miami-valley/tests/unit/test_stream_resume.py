from pathlib import Path

from mvoi.services import stream_resume

CONFIG = Path(__file__).parents[2] / "config" / "fabric.yaml"


class FakeClient:
    """Destination statuses advance one step per topology read; `down` reads fail first."""

    def __init__(self, timeline: list[str], down: int = 0):
        self.timeline, self.down, self.reads, self.resumes = timeline, down, 0, []

    def workspace_id(self, name):
        if self.down:
            self.down -= 1
            raise RuntimeError("CapacityNotActive")
        return "ws"

    def find_item(self, ws, item_type, name):
        return {"id": "es"}

    def request(self, method, path, body=None):
        if method == "POST":
            self.resumes.append((path, body))
            return None
        status = self.timeline[min(self.reads, len(self.timeline) - 1)]
        self.reads += 1
        return {"destinations": [{"id": "d1", "name": "EventhouseEvents", "status": status}]}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(client, wait_minutes=30):
    clock = Clock()
    return stream_resume.run(client, CONFIG, wait_minutes=wait_minutes, poll_seconds=30,
                             sleep=clock.sleep, clock=clock)


def test_running_destination_is_left_alone():
    c = FakeClient(["Running"])
    assert _run(c) and c.resumes == []


def test_paused_destination_resumes_from_where_it_stopped():
    c = FakeClient(["Paused", "Resuming", "Running"])
    assert _run(c)
    assert c.resumes == [("/workspaces/ws/eventstreams/es/destinations/d1/resume",
                          {"startType": "WhenLastStopped"})]


def test_retries_while_capacity_is_suspended():
    c = FakeClient(["Paused", "Running"], down=3)
    assert _run(c) and len(c.resumes) == 1


def test_gives_up_after_wait():
    c = FakeClient(["Paused"], down=1000)
    assert not _run(c, wait_minutes=2) and c.resumes == []
