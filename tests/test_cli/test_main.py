from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from src.cli.commands import handle_chat_command
from src.cli.main import cli
from src.core.types import Message, MessageRole
from src.examples.install_stocks_watch import install_stocks_watch
from src.workflow.draft_store import WorkflowDraftStore
from tests.helpers import BlockingProvider, make_test_coara
from tests.workflow_wdl_fixtures import minimal_two_step_wdl


@pytest.mark.asyncio
async def test_stop_command_when_idle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_coara(tmp_path, provider=BlockingProvider())
    root.foreground_coara = root
    await root.initialize()

    printed: list[str] = []

    def _capture_print(*args, **kwargs):
        printed.append(" ".join(map(str, args)))

    monkeypatch.setattr("src.cli.commands.console.print", _capture_print)

    should_exit = await handle_chat_command(root, "/stop", tmp_path)

    assert should_exit is False
    assert any("当前没有运行中的回合" in line for line in printed)


@pytest.mark.asyncio
async def test_new_command_starts_fresh_session(tmp_path: Path) -> None:
    root = make_test_coara(tmp_path, provider=BlockingProvider())
    root.foreground_coara = root
    old_sid = root.session_id
    root.message_history.append(Message(role=MessageRole.USER, content="旧会话"))

    should_exit = await handle_chat_command(root, "/new", tmp_path)

    assert should_exit is False
    assert root.session_id != old_sid
    assert all("旧会话" not in str(getattr(m, "content", "")) for m in root.message_history)


@pytest.mark.asyncio
async def test_status_command_reads_role_field_without_key_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_test_coara(tmp_path, provider=BlockingProvider())
    await root.initialize()

    printed: list[str] = []

    def _capture_print(*args, **kwargs):
        printed.append(" ".join(map(str, args)))

    monkeypatch.setattr("src.cli.commands.console.print", _capture_print)

    should_exit = await handle_chat_command(root, "/status", tmp_path)

    assert should_exit is False
    assert any("名称：" in line for line in printed)


@pytest.mark.asyncio
async def test_usage_command_shows_current_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_coara(tmp_path, provider=BlockingProvider())
    await root.initialize()

    events_path = tmp_path / ".coara" / "usage" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            {
                "kind": "llm_turn",
                "session_id": root.session_id,
                "usage": {"input_tokens": 50, "output_tokens": 10},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "src.runtime.usage_query.resolve_usage_path_for_root",
        lambda _root: events_path,
    )

    printed: list[str] = []

    def _capture_print(*args, **kwargs):
        printed.append(" ".join(map(str, args)))

    monkeypatch.setattr("src.cli.commands.console.print", _capture_print)

    should_exit = await handle_chat_command(root, "/usage", tmp_path)

    assert should_exit is False
    joined = "\n".join(printed)
    assert "当前对话" in joined
    assert "输入 50" in joined.replace(",", "")


def test_workflow_draft_delete_removes_draft(tmp_path: Path) -> None:
    """草稿存储的删除路径。

    ``/workflow`` 命令链已随 WDL 剥离移除（`src/coara/commands/` 下已无 workflow.py），
    所以这条用例只覆盖存储层：删掉的草稿必须查不到，重复删除返回 False。
    """
    store = WorkflowDraftStore(coara_home=tmp_path)
    draft = store.save(
        minimal_two_step_wdl(name="tower defense", description="demo workflow"),
        source_subagent="root",
    )
    assert store.get(draft.draft_id) is not None

    assert store.delete(draft.draft_id) is True
    assert store.get(draft.draft_id) is None
    assert store.delete(draft.draft_id) is False


# --- examples cli ---


def test_install_stocks_watch_dry_run(tmp_path):
    result = install_stocks_watch(coara_home=tmp_path, dry_run=True)
    assert result.coara_home == tmp_path.resolve()
    assert len(result.copied) == 2
    assert result.registry_updated is True


def test_cli_examples_install_stocks_watch_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    outcome = runner.invoke(
        cli,
        ["examples", "install", "stocks-watch", "--coara-home", str(tmp_path), "--dry-run"],
    )
    assert outcome.exit_code == 0, outcome.output
    # 干跑只打印计划、不落盘：这里断言「没写任何文件」这个可观测事实。
    # 不在此断言打印文案——rich 的 Console 在 import 期绑定了当时的 stdout，
    # CliRunner 与 pytest 的捕获都拿不到它（文案由 rich 直写，见 capfd 也取不到），
    # 文案本身由 test_install_stocks_watch_dry_run（库层）覆盖。
    assert not (tmp_path / "workspaces-meta" / "stocks-watch.yaml").exists()
    assert not (tmp_path / "users" / "default" / "matters" / "definitions" / "stocks-watch-inbox-watch.yaml").exists()


def test_cli_examples_install_unknown():
    runner = CliRunner()
    outcome = runner.invoke(cli, ["examples", "install", "unknown-pack"])
    assert outcome.exit_code != 0


def _patch_lock_to_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """让 acquire_instance_lock 抛 InstanceLockError（模拟主进程已在跑）。"""
    import src.core.instance_lock as lock_mod

    def _raise(home, *, pid=None):
        raise lock_mod.InstanceLockError(4242, tmp_path / "coara.pid")

    monkeypatch.setattr(lock_mod, "acquire_instance_lock", _raise)


def _patch_active_runtime(monkeypatch: pytest.MonkeyPatch, runtime) -> None:
    """mock load_active_runtime：返回给定 runtime（None=无活主进程）。"""
    import src.coara.workspace_runtime as rt_mod

    monkeypatch.setattr(rt_mod, "load_active_runtime", lambda home: runtime)


def test_acquire_lock_conflict_non_tty_exits_with_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """active.json 无活主进程 + 非 TTY 抢锁失败：仍报错退出，不转外挂。"""
    import src.cli.main as main_mod

    _patch_active_runtime(monkeypatch, None)  # 无活主进程 → 走锁
    _patch_lock_to_conflict(monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit) as excinfo:
        main_mod._acquire_instance_lock_or_exit(tmp_path)
    assert excinfo.value.code == 1


def test_attach_into_running_instance_registers_cwd_and_runs_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd 即工作空间：未登记的目录经 ensure_workspace 落注册表，再按其名接入跑完整界面。"""
    import src.cli.attach_client as attach_mod
    import src.cli.main as main_mod
    import src.cli.workspace_cmds as ws_cmds

    entry = SimpleNamespace(name="my-dir", id="my-dir-abc123")
    ensured: dict = {}

    class _FakeRegistry:
        def ensure_workspace(self, path, name=None, summary=None):
            ensured["path"] = path
            return entry

    monkeypatch.setattr(ws_cmds, "_open_cli_registry", lambda ws: _FakeRegistry())

    run_args: dict = {}

    def _fake_run_attached(name, *, workspace_dir, host, port, token):
        run_args.update({"name": name, "workspace_dir": workspace_dir, "host": host, "port": port})
        return 0

    monkeypatch.setattr(main_mod, "_run_attached_chat_command", _fake_run_attached)
    monkeypatch.setattr(attach_mod, "load_attach_token", lambda ws: "tok")
    monkeypatch.setattr(attach_mod, "resolve_web_host_port", lambda: ("127.0.0.1", 8080))

    with pytest.raises(SystemExit) as excinfo:
        main_mod._attach_into_running_instance(tmp_path, running_pid=4242, running_workspace="nx")
    assert excinfo.value.code == 0
    assert ensured["path"] == tmp_path
    assert run_args["name"] == "my-dir"
    assert run_args["port"] == 8080
