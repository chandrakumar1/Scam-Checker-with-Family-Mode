from app.channels.telegram.dedup import SeenUpdates


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_first_sighting_is_new_second_is_duplicate():
    seen = SeenUpdates()
    assert seen.seen_before(1) is False
    assert seen.seen_before(1) is True
    assert seen.seen_before(2) is False


def test_entry_expires_after_ttl():
    clock = Clock()
    seen = SeenUpdates(ttl_seconds=60, clock=clock)
    assert seen.seen_before(1) is False
    clock.now += 59
    assert seen.seen_before(1) is True
    clock.now += 2  # 61s after the first sighting
    assert seen.seen_before(1) is False  # expired, treated as new again


def test_duplicate_does_not_extend_ttl():
    clock = Clock()
    seen = SeenUpdates(ttl_seconds=60, clock=clock)
    seen.seen_before(1)
    clock.now += 50
    assert seen.seen_before(1) is True
    clock.now += 11  # 61s after the FIRST sighting
    assert seen.seen_before(1) is False


def test_expired_entries_are_purged():
    clock = Clock()
    seen = SeenUpdates(ttl_seconds=10, clock=clock)
    for update_id in range(5):
        seen.seen_before(update_id)
    assert len(seen) == 5
    clock.now += 11
    seen.seen_before(99)
    assert len(seen) == 1


def test_max_size_evicts_oldest():
    seen = SeenUpdates(max_size=3)
    for update_id in (1, 2, 3, 4):
        seen.seen_before(update_id)
    assert len(seen) == 3
    assert seen.seen_before(4) is True
    assert seen.seen_before(2) is True
    assert seen.seen_before(1) is False  # was evicted, so it counts as new


def test_instances_are_independent():
    a, b = SeenUpdates(), SeenUpdates()
    a.seen_before(1)
    assert b.seen_before(1) is False
