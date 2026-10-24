from __future__ import annotations

import threading
import time

from dapalette_runtime import control_plane as cp
from dapalette_runtime.db import connect
from dapalette_runtime.worker import claim, finish, renew_lease, stop_reason

from .helpers import WS, file_db, mem_db, owner_work, rows


def test_chat_without_mention_creates_no_work() -> None:
    conn = mem_db()
    assert cp.owner_message(conn, workspace_id=WS, thread_key="t", text="let's do a seeding campaign") is None
    assert cp.owner_message(conn, workspace_id=WS, thread_key="t", text="email@ai.com is mine") is None  # not a mention
    assert rows(conn, "SELECT * FROM work_items") == []


def test_new_message_on_live_thread_bumps_version_and_retires_old_pending_run() -> None:
    conn = mem_db()
    first = owner_work(conn, text="@AI plan a campaign")
    second = owner_work(conn, text="@AI actually, budget is 2M")
    assert (second.work_id, second.version, second.reused) == (first.work_id, 2, True)
    runs = rows(conn, "SELECT work_version, status FROM worker_runs ORDER BY work_version")
    assert runs == [{"work_version": 1, "status": "CANCELLED"}, {"work_version": 2, "status": "PENDING"}]


def test_superseded_running_run_is_told_to_stop() -> None:
    conn = mem_db()
    owner_work(conn)
    run = claim(conn, "w1")
    assert run is not None and stop_reason(conn, run) is None
    owner_work(conn, text="@AI wait, change of plan")
    assert stop_reason(conn, run) == "superseded by a newer version of this work"
    assert claim(conn, "w2") is None  # v2 waits: v1 still holds the work's RUNNING slot
    finish(conn, run, "w1", "CANCELLED")
    run2 = claim(conn, "w2")
    assert run2 is not None and run2.work_version == 2


def test_dependencies_must_be_completed() -> None:
    conn = mem_db()
    a = owner_work(conn, thread="a")
    b = owner_work(conn, thread="b")
    cp.add_dependency(conn, work_id=b.work_id, depends_on_work_id=a.work_id)
    run_a = claim(conn, "w1")
    assert run_a is not None and run_a.work_id == a.work_id
    assert claim(conn, "w2") is None  # b is blocked while a is not COMPLETED
    finish(conn, run_a, "w1", "SUCCEEDED")
    run_b = claim(conn, "w2")
    assert run_b is not None and run_b.work_id == b.work_id


def test_cancelled_work_is_never_resurrected_by_a_leftover_run() -> None:
    conn = mem_db()
    w = owner_work(conn)
    conn.execute("UPDATE work_items SET status = 'CANCELLED' WHERE work_id = ?", (w.work_id,))
    assert claim(conn, "w1") is None


def test_cancellation_is_applied_when_the_run_finishes() -> None:
    conn = mem_db()
    w = owner_work(conn)
    run = claim(conn, "w1")
    assert run is not None
    cp.request_cancel(conn, workspace_id=WS, work_id=w.work_id)
    assert stop_reason(conn, run) == "cancelled by the owner"
    finish(conn, run, "w1", "CANCELLED")
    assert cp.describe(conn, w.work_id)["status"] == "CANCELLED"
    assert rows(conn, "SELECT status FROM cancellations") == [{"status": "APPLIED"}]


def test_expired_lease_is_reclaimed_and_the_old_holder_loses_it() -> None:
    conn = mem_db()
    owner_work(conn)
    run = claim(conn, "dead-worker", lease=0.01)
    assert run is not None
    time.sleep(0.05)
    again = claim(conn, "w2")
    assert again is not None and again.run_id == run.run_id
    assert renew_lease(conn, run, "dead-worker") is False  # the dead worker can no longer extend or finish it
    finish(conn, run, "dead-worker", "FAILED")
    assert rows(conn, "SELECT status, owner_worker_id FROM worker_runs WHERE run_id = ?", run.run_id) == [{"status": "RUNNING", "owner_worker_id": "w2"}]


def test_concurrent_workers_never_claim_the_same_run() -> None:
    path, conn = file_db()
    for i in range(30):
        owner_work(conn, thread=f"t{i}")
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        c = connect(path)
        while (run := claim(c, name)) is not None:
            with lock:
                claimed.append(run.run_id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 30 and len(set(claimed)) == 30
