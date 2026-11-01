import sqlite3

import pytest

from crossfade.models import Project, new_id
from crossfade.redact import redact
from crossfade.store.artifacts import ArtifactStore, IntegrityError
from crossfade.store.ledger import Ledger


def test_objects_are_immutable_and_events_chain(tmp_path):
    led = Ledger(tmp_path / "l.db")
    p = Project(id=new_id("prj"), name="x", root="/x")
    led.put(p)
    led.append("thing.happened", subject=p.id, n=1)
    assert led.get(p.id, Project).name == "x"
    n, bad = led.verify_chain()
    assert n == 2 and bad == []
    with pytest.raises(sqlite3.DatabaseError):
        led.db.execute("UPDATE objects SET data='{}' WHERE id=?", (p.id,))
    with pytest.raises(sqlite3.DatabaseError):
        led.db.execute("DELETE FROM events")


def test_tampering_breaks_the_chain(tmp_path):
    led = Ledger(tmp_path / "l.db")
    for i in range(3):
        led.append("e", n=i)
    # someone with raw file access drops the trigger and edits history
    led.db.execute("DROP TRIGGER events_no_update")
    led.db.execute("UPDATE events SET payload='{\"n\": 99}' WHERE seq=2")
    _, bad = led.verify_chain()
    assert bad == [2]


def test_artifacts_are_content_addressed_and_verified(tmp_path):
    store = ArtifactStore(tmp_path / "a")
    d = store.put_json({"b": 1, "a": 2})
    assert d == store.put_json({"a": 2, "b": 1})
    assert store.get_json(d) == {"a": 2, "b": 1}
    path = store._path(d)
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        store.get_bytes(d)
    assert store.verify_all()[1] == [d]


def test_directory_snapshots_are_deterministic(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")
    store = ArtifactStore(tmp_path / "a")
    d1 = store.put_dir(src)
    (src / "a.txt").touch()  # mtime change must not change the digest
    assert store.put_dir(src) == d1
    out = store.extract_dir(d1, tmp_path / "out")
    assert (out / "sub" / "b.txt").read_text() == "b"


def test_secrets_are_redacted_before_storage(tmp_path):
    assert "sk-ant-abcdefghijklmnopqrstu" not in redact("key sk-ant-abcdefghijklmnopqrstu here")
    assert "hunter22" not in redact('{"password": "hunter22"}')
    store = ArtifactStore(tmp_path / "a")
    d = store.put_bytes(b"token=ghp_abcdefghijklmnopqrstuvwxyz0123")  # gitleaks:allow (fake token to test redaction)
    assert b"ghp_" not in store.get_bytes(d)


def test_plugins_resolve_by_capability_and_report_health():
    from crossfade.plugins import INTERFACE_VERSION, registry, resolve, status

    assert all(p.interface_version == INTERFACE_VERSION for p in registry())  # regression: health was once passed as the version
    assert resolve("execution.run").name == "runner.local-process"
    assert resolve("evaluation.run").name == "evaluator.default"
    rows = {r["name"]: r for r in status()}
    assert rows["instrumentation.otel-jsonl"]["healthy"] is True
