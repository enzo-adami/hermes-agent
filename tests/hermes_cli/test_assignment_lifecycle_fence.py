"""Assignment writers must serialize with profile retirement before SQLite."""
import concurrent.futures
import threading
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import profile_lifecycle as lifecycle
from hermes_cli import profiles


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    # Exercise real profile mutation and authority; never touch host services.
    for name in ('_cleanup_gateway_service', '_maybe_unregister_gateway_service',
                 '_stop_gateway_process', '_stop_profile_backends',
                 '_migrate_honcho_profile_host', 'remove_wrapper_script',
                 'create_wrapper_script'):
        monkeypatch.setattr(profiles, name, lambda *a, **k: None)
    monkeypatch.setattr(profiles, '_check_gateway_running', lambda *a: False)
    monkeypatch.setattr(profiles, 'check_alias_collision', lambda *a: None)
    monkeypatch.setattr(profiles, '_get_wrapper_dir', lambda: tmp_path / 'bin')
    profiles.get_profile_dir('retiring').mkdir(parents=True)
    conn = kb.connect()
    task = kb.create_task(conn, title='Assignment fence', assignee='other')
    yield conn, task, home / 'kanban.db'
    conn.close()


def retire(operation):
    if operation == 'delete':
        profiles.delete_profile('retiring', yes=True)
    else:
        profiles.rename_profile('retiring', 'renamed')


@pytest.mark.parametrize('operation', ['delete', 'rename'])
@pytest.mark.parametrize('writer', ['assign_task', 'reassign_task'])
def test_retirement_wins_before_waiting_writer(board, monkeypatch, operation, writer):
    conn, task, path = board
    attempted = threading.Event()
    real_lock = lifecycle.profile_lifecycle_lock

    def observed_lock(*a, **k):
        if threading.current_thread() is not threading.main_thread():
            attempted.set()
        return real_lock(*a, **k)

    monkeypatch.setattr(lifecycle, 'profile_lifecycle_lock', observed_lock)

    def assign():
        other = kb.connect(path)
        try:
            return getattr(kb, writer)(other, task, 'retiring')
        finally:
            other.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with real_lock():
            future = pool.submit(assign)
            assert attempted.wait(5), 'writer did not reach lifecycle authority'
            assert not future.done()
            # A separate connection can still take the SQLite writer lock:
            # the waiting assignment must not have acquired it first.
            with kb.write_txn(conn):
                pass
            retire(operation)
        with pytest.raises(ValueError, match='unavailable'):
            future.result(timeout=5)
    assert conn.execute('SELECT assignee FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'other'
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='assigned'", (task,)).fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['delete', 'rename'])
@pytest.mark.parametrize('writer', ['assign_task', 'reassign_task'])
def test_committed_assignment_prevents_retirement(board, operation, writer):
    conn, task, path = board
    other = kb.connect(path)
    try:
        assert getattr(kb, writer)(other, task, 'retiring')
        with pytest.raises(RuntimeError, match='open Kanban assignments'):
            retire(operation)
        assert profiles.profile_exists('retiring')
        assert conn.execute('SELECT assignee FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'retiring'
    finally:
        other.close()


@pytest.mark.parametrize('operation', ['delete', 'rename'])
@pytest.mark.parametrize('writer', ['assign_task', 'reassign_task'])
def test_retired_identity_rejected_and_recreated_identity_accepted(board, operation, writer):
    conn, task, _ = board
    retire(operation)
    with pytest.raises(ValueError, match='unavailable'):
        getattr(kb, writer)(conn, task, 'retiring')
    profiles.get_profile_dir('retiring').mkdir()
    assert getattr(kb, writer)(conn, task, 'retiring')


def test_running_claim_still_blocks_assignment(board):
    conn, task, _ = board
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='running', claim_lock='active' WHERE id=?", (task,))
    with pytest.raises(RuntimeError, match='currently running'):
        kb.assign_task(conn, task, 'retiring')
    assert not kb.reassign_task(conn, task, 'retiring')


def test_unassign_and_unknown_profile_remain_supported(board):
    conn, task, _ = board
    assert kb.assign_task(conn, task, 'never-seen')
    assert kb.reassign_task(conn, task, None)
    assert conn.execute('SELECT assignee FROM tasks WHERE id=?', (task,)).fetchone()[0] is None


@pytest.mark.parametrize('operation', ['delete', 'rename'])
def test_dispatch_default_assignment_rechecks_retired_identity(board, monkeypatch, operation):
    conn, task, _ = board
    kb.assign_task(conn, task, None)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))
    real_exists = profiles.profile_exists
    checked = False

    def retire_after_check(name):
        nonlocal checked
        exists = real_exists(name)
        if name == 'retiring' and not checked:
            checked = True
            retire(operation)
        return exists

    monkeypatch.setattr(profiles, 'profile_exists', retire_after_check)
    def forbidden_spawn(*a, **k):
        pytest.fail('retired default assignee must not spawn')
    result = kb.dispatch_once(conn, default_assignee='retiring', spawn_fn=forbidden_spawn,
                              reconcile_orphans=False)
    assert checked
    assert task in result.skipped_unassigned
    assert conn.execute('SELECT assignee FROM tasks WHERE id=?', (task,)).fetchone()[0] is None


def test_nested_assignment_rejected_before_lifecycle_lock(board, monkeypatch):
    conn, task, _ = board
    def forbidden(*a, **k):
        pytest.fail('nested assignment tried to acquire lifecycle authority')
    monkeypatch.setattr(lifecycle, 'profile_lifecycle_lock', forbidden)
    with kb.write_txn(conn):
        with pytest.raises(RuntimeError, match='transaction'):
            kb.assign_task(conn, task, 'retiring')
