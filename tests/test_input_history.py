import json

from manyselves.core.input_history import InputHistoryStore


def test_input_history_is_project_local_deduplicated_and_bounded(tmp_path):
    store = InputHistoryStore(tmp_path, max_entries=3)

    store.append("main", "first")
    store.append("main", "second")
    store.append("main", "first")
    store.append("main", "third")
    entries = store.append("main", "fourth")

    assert entries == ["first", "third", "fourth"]
    assert store.entries("other") == []
    assert store.path == tmp_path / ".manyselves" / "input_history.json"
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["agents"]["main"] == entries


def test_corrupt_input_history_is_ignored(tmp_path):
    path = tmp_path / ".manyselves" / "input_history.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")

    store = InputHistoryStore(tmp_path)

    assert store.entries("main") == []
