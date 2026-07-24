"""Launch Claude Code CLI pointed at the local PCC proxy."""

import os
import sys

from free_claude_code.cli.launchers.common import (
    preflight_proxy,
    resolve_client_binary,
    run_client_process,
)
from free_claude_code.config.server_urls import local_proxy_root_url
from free_claude_code.config.settings import get_settings

_CLAUDE_BINARY_NAME = "claude"
_CLAUDE_DISPLAY_NAME = "Claude Code"
_CLAUDE_INSTALL_HINT = (
    "Install it with:\n"
    "  npm install -g @anthropic-ai/claude-code\n"
    "Or:\n"
    "  curl -fsSL https://claude.ai/install.sh | sh"
)


def launch(argv: list[str] | None = None) -> None:
    """Start Claude Code with environment variables targeting the local proxy."""
    settings = get_settings()
    proxy_url = local_proxy_root_url(settings)

    proxy_error = preflight_proxy(proxy_url)
    if proxy_error is not None:
        print(
            f"PCC proxy at {proxy_url} is not reachable ({proxy_error}).\n"
            f"Start it first with: pcc-server",
            file=sys.stderr,
        )
        raise SystemExit(1)

    client_command = resolve_client_binary(
        binary_name=_CLAUDE_BINARY_NAME,
        display_name=_CLAUDE_DISPLAY_NAME,
        install_hint=_CLAUDE_INSTALL_HINT,
    )

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = proxy_url
    env["CLAUDE_CODE_USE_BEDROCK"] = "0"
    env["CLAUDE_CODE_USE_VERTEX"] = "0"

    auth_token = settings.anthropic_auth_token.strip()
    if auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = auth_token

    args = argv if argv is not None else sys.argv[1:]
    command = [client_command, *args]

    run_client_process(
        command=command,
        env=env,
        binary_name=_CLAUDE_BINARY_NAME,
        display_name=_CLAUDE_DISPLAY_NAME,
        install_hint=_CLAUDE_INSTALL_HINT,
    )
