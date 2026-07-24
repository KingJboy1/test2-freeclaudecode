"""Tests for installed CLI entrypoints, commands, and launchers."""

import json
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from free_claude_code.config.settings import Settings


def _launcher_settings(
    *,
    port: int = 8082,
    token: str = "freecc",
    open_admin_browser: bool = True,
) -> Settings:
    return Settings.model_construct(
        host="0.0.0.0",
        port=port,
        anthropic_auth_token=token,
        model="nvidia_nim/test-model",
        open_admin_browser=open_admin_browser,
    )


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_legacy_env_migration_supports_xdg_path(tmp_path: Path) -> None:
    """Server startup preserves config from ~/.config/free-claude-code/.env."""
    from free_claude_code.cli.commands import _migrate_legacy_env_if_missing

    legacy_env = tmp_path / ".config" / "free-claude-code" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=open_router/free-model\n", encoding="utf-8")

    with patch("pathlib.Path.home", return_value=tmp_path):
        migrated_from = _migrate_legacy_env_if_missing()

    env_file = tmp_path / ".fcc" / ".env"
    assert migrated_from == legacy_env
    assert env_file.read_text("utf-8") == "MODEL=open_router/free-model\n"


def test_legacy_env_migration_does_not_overwrite_managed_env(
    tmp_path: Path,
) -> None:
    """Legacy migration never overwrites an existing ~/.fcc/.env."""
    from free_claude_code.cli.commands import _migrate_legacy_env_if_missing

    managed_env = tmp_path / ".fcc" / ".env"
    managed_env.parent.mkdir(parents=True)
    managed_env.write_text("MODEL=nvidia_nim/current\n", encoding="utf-8")
    legacy_env = tmp_path / "free-claude-code" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=deepseek/legacy\n", encoding="utf-8")

    with patch("pathlib.Path.home", return_value=tmp_path):
        migrated_from = _migrate_legacy_env_if_missing()

    assert migrated_from is None
    assert managed_env.read_text("utf-8") == "MODEL=nvidia_nim/current\n"


def test_cli_scripts_are_registered() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert pyproject["project"]["scripts"] == {
        "pcc-server": "free_claude_code.cli.entrypoints:serve",
        "pcc-claude": "free_claude_code.cli.launchers.claude:launch",
    }


@pytest.mark.parametrize(
    "argv",
    [("--version",), ("--version", "--help"), ("--help", "--version")],
)
def test_fcc_server_reports_version_without_side_effects(
    argv: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from free_claude_code.cli import entrypoints

    with patch.object(entrypoints, "package_version", return_value="9.8.7"):
        entrypoints.serve(argv)

    assert capsys.readouterr() == ("free-claude-code 9.8.7\n", "")


def test_version_entrypoint_does_not_import_command_runtime() -> None:
    script = "\n".join(
        (
            "import json",
            "import sys",
            "from free_claude_code.cli.entrypoints import serve",
            "serve(['--version'])",
            "forbidden = ('uvicorn', 'fastapi', 'openai', "
            "'free_claude_code.cli.commands', "
            "'free_claude_code.runtime.bootstrap')",
            "print(json.dumps([name for name in forbidden if name in sys.modules]))",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1]) == []


def test_non_version_entrypoint_delegates_to_server_command() -> None:
    from free_claude_code.cli import commands, entrypoints

    with patch.object(commands, "serve") as command:
        entrypoints.serve(())

    command.assert_called_once_with()


def test_schedule_open_admin_browser_opens_when_health_ready() -> None:
    """Opening /admin runs after /health preflight succeeds."""
    from free_claude_code.cli import commands
    from free_claude_code.config.server_urls import local_admin_url

    settings = _launcher_settings(port=31337)
    opened_urls: list[str] = []

    class ImmediateThread:
        def __init__(self, target=None, args=(), **_kwargs: object) -> None:
            self._target = target
            self._args = args

        def start(self) -> None:
            assert self._target is not None
            self._target(*self._args)

    with (
        patch.object(commands.threading, "Thread", ImmediateThread),
        patch.object(commands, "preflight_proxy", return_value=None),
        patch.object(
            commands.webbrowser,
            "open",
            side_effect=lambda url: opened_urls.append(url),
        ),
        patch.object(commands.time, "sleep"),
    ):
        commands.schedule_open_admin_browser(settings)

    assert opened_urls == [local_admin_url(settings)]


def test_serve_skips_admin_browser_when_setting_is_disabled() -> None:
    from free_claude_code.cli import commands

    settings = _launcher_settings(open_admin_browser=False)
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands.ServerSupervisor, "_run_once", return_value=False
        ) as run_server,
        patch.object(commands, "kill_all_best_effort"),
    ):
        commands.serve()

    run_server.assert_called_once_with(
        settings,
        open_admin_browser=False,
        restart_generation=0,
    )


def test_serve_supervisor_restarts_when_app_requests_restart() -> None:
    from free_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(side_effect=[settings, settings])
    get_settings.cache_clear = MagicMock()
    servers: list[object] = []
    restart_callbacks: list[Callable[[], None]] = []

    apps: list[SimpleNamespace] = []

    def build_asgi_app(_settings: Settings, restart_callback: Callable[[], None]):
        restart_callbacks.append(restart_callback)
        app = SimpleNamespace(runtime=SimpleNamespace(is_closed=False))
        apps.append(app)
        return app

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            servers.append(self)

        def run(self):
            if len(servers) == 1:
                restart_callbacks[-1]()
                assert self.should_exit is True
                self.config.app.runtime.is_closed = True

    def fake_config(app, **kwargs):
        return SimpleNamespace(app=app, kwargs=kwargs)

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "schedule_open_admin_browser") as schedule_open_admin,
        patch.object(commands, "kill_all_best_effort") as kill_all,
    ):
        commands.serve()

    assert len(servers) == 2
    schedule_open_admin.assert_called_once_with(settings)
    get_settings.cache_clear.assert_called_once()
    kill_all.assert_called_once()


def test_serve_supervisor_refuses_restart_after_incomplete_shutdown() -> None:
    from free_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    servers: list[object] = []
    restart_callbacks: list[Callable[[], None]] = []

    def build_asgi_app(_settings: Settings, restart_callback: Callable[[], None]):
        restart_callbacks.append(restart_callback)
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=False))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            servers.append(self)

        def run(self):
            restart_callbacks[-1]()
            assert self.should_exit is True

    def fake_config(app, **kwargs):
        return SimpleNamespace(app=app, kwargs=kwargs)

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "schedule_open_admin_browser"),
        patch.object(commands, "kill_all_best_effort") as kill_all,
    ):
        commands.serve()

    assert len(servers) == 1
    get_settings.cache_clear.assert_not_called()
    kill_all.assert_called_once()


def test_serve_migrates_legacy_env_before_loading_settings(tmp_path: Path) -> None:
    from free_claude_code.cli import commands

    legacy_env = tmp_path / "free-claude-code" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=deepseek/deepseek-chat\n", encoding="utf-8")
    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.ServerSupervisor, "_run_once", return_value=False),
        patch.object(commands, "kill_all_best_effort"),
    ):
        commands.serve()

    assert (tmp_path / ".fcc" / ".env").read_text("utf-8") == (
        "MODEL=deepseek/deepseek-chat\n"
    )
    get_settings.assert_called_once_with()


def test_serve_migrates_hf_token_before_loading_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from free_claude_code.cli import commands

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("HF_TOKEN=legacy-hf\n", encoding="utf-8")
    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    monkeypatch.chdir(repo)

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.ServerSupervisor, "_run_once", return_value=False),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "explicit_env_file_migration_warning"),
    ):
        commands.serve()

    assert (repo / ".env").read_text(encoding="utf-8") == (
        "HUGGINGFACE_API_KEY=legacy-hf\n"
    )
    get_settings.assert_called_once_with()


def test_config_env_key_migration_warns_for_explicit_env_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from free_claude_code.cli import commands

    explicit = tmp_path / "custom.env"
    explicit.write_text("HF_TOKEN=legacy-hf\n", encoding="utf-8")

    with patch.dict(commands.os.environ, {"FCC_ENV_FILE": str(explicit)}):
        migrated = commands._migrate_config_env_keys()

    assert migrated == ()
    assert "HF_TOKEN" in capsys.readouterr().err
    assert explicit.read_text(encoding="utf-8") == "HF_TOKEN=legacy-hf\n"


def test_serve_handles_keyboard_interrupt_without_traceback() -> None:
    from free_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands.ServerSupervisor,
            "_run_once",
            side_effect=KeyboardInterrupt,
        ),
        patch.object(commands, "kill_all_best_effort") as kill_all,
    ):
        commands.serve()

    get_settings.cache_clear.assert_not_called()
    kill_all.assert_called_once()


def test_claude_child_env_targets_current_proxy_config() -> None:
    from free_claude_code.cli.claude_env import build_claude_proxy_env

    env = build_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:9090",
        auth_token=" proxy-token ",
        base_env={
            "PATH": "keep",
            "ANTHROPIC_API_URL": "https://api.anthropic.com/v1",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_AUTH_TOKEN": "old-token",
            "ANTHROPIC_API_KEY": "official-key",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "0",
            "DISABLE_FEEDBACK_COMMAND": "0",
            "DISABLE_ERROR_REPORTING": "0",
            "DISABLE_TELEMETRY": "0",
        },
    )

    assert env["PATH"] == "keep"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9090"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "190000"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["DISABLE_FEEDBACK_COMMAND"] == "1"
    assert env["DISABLE_ERROR_REPORTING"] == "1"
    assert env["DISABLE_TELEMETRY"] == "1"
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert env["no_proxy"] == env["NO_PROXY"]
    assert "ANTHROPIC_API_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in env


def test_claude_child_env_uses_sentinel_for_blank_configured_auth_token() -> None:
    from free_claude_code.cli.claude_env import build_claude_proxy_env

    env = build_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:8082",
        auth_token="",
        base_env={
            "ANTHROPIC_AUTH_TOKEN": "inherited-token",
            "ANTHROPIC_API_KEY": "official-key",
        },
    )

    assert env["ANTHROPIC_AUTH_TOKEN"] == "fcc-no-auth"
    assert "ANTHROPIC_API_KEY" not in env


def test_launch_claude_passes_args_and_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-token")
    monkeypatch.setenv("KEEP_ME", "yes")


_launcher_settings(port=9191, token="proxy-token")


def test_launch_claude_keyboard_interrupt_kills_child_tree() -> None:
    from free_claude_code.cli.launchers.claude import launch

    settings = _launcher_settings(port=9191, token="proxy-token")

    with (
        patch(
            "free_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch(
            "free_claude_code.cli.launchers.claude.preflight_proxy", return_value=None
        ),
        patch(
            "free_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-claude.cmd",
        ),
        patch("free_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("free_claude_code.cli.launchers.common.register_pid"),
        patch(
            "free_claude_code.cli.launchers.common.kill_pid_tree_best_effort"
        ) as kill_tree,
        patch("free_claude_code.cli.launchers.common.unregister_pid") as unregister_pid,
        pytest.raises(KeyboardInterrupt),
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.side_effect = [KeyboardInterrupt, 0]

        launch([])

    kill_tree.assert_called_once_with(12345)
    unregister_pid.assert_called_once_with(12345)


def test_launch_claude_exits_when_command_cannot_be_resolved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from free_claude_code.cli.launchers.claude import launch

    settings = _launcher_settings()
    with (
        patch(
            "free_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch(
            "free_claude_code.cli.launchers.claude.preflight_proxy", return_value=None
        ),
        patch("free_claude_code.cli.launchers.common.shutil.which", return_value=None),
        patch("free_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        pytest.raises(SystemExit) as exc_info,
    ):
        launch([])

    assert exc_info.value.code == 127
    popen.assert_not_called()
    captured = capsys.readouterr()
    assert "Could not find Claude Code command: claude" in captured.err
    assert "npm install -g @anthropic-ai/claude-code" in captured.err


def test_launch_claude_unreachable_proxy_exits_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from free_claude_code.cli.launchers.claude import launch

    settings = _launcher_settings(port=9393)
    with (
        patch(
            "free_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch(
            "free_claude_code.cli.launchers.claude.preflight_proxy",
            return_value="connection refused",
        ),
        patch("free_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        pytest.raises(SystemExit) as exc_info,
    ):
        launch([])

    assert exc_info.value.code == 1
    popen.assert_not_called()
    captured = capsys.readouterr()
    assert "http://127.0.0.1:9393" in captured.err
    assert "pcc-server" in captured.err
