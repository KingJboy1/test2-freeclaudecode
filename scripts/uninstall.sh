#!/bin/sh
set -eu

PACKAGE_NAME="free-claude-code"
FCC_HOME_DIRNAME=".fcc"
FCC_MACOS_BUNDLE_ID="io.github.king-jboy.kingjboy-claude-code"
FCC_MACOS_OWNER_FILE=".free-claude-code-owner"
# Include retired entry points so older installations are fully stopped and removed.
FCC_COMMANDS="pcc-server pcc-claude free-claude-code"

dry_run=0
uv_tool_bin=""

show_usage() {
    cat <<'USAGE'
Usage: uninstall.sh [options]

Removes the Free Claude Code uv tool and deletes ~/.fcc/ after removal is verified.
Does not remove uv, Claude Code, the uv-managed Python runtime, or shared PATH entries.
Options:
  --dry-run                Print commands without running them.
  --help                   Show this help text.
USAGE
}
fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}
step() {
    printf '\n==> %s\n' "$1"
}
quote_arg() {
    case "$1" in
        *[!A-Za-z0-9_./:@%+=,-]*|"")
            escaped=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
            printf '"%s"' "$escaped"
            ;;
        *)
            printf '%s' "$1"
            ;;
    esac
}
print_command() {
    printf '+'
    for arg in "$@"; do
        printf ' '
        quote_arg "$arg"
    done
    printf '\n'
}
run() {
    print_command "$@"
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi
    if "$@"; then
        return 0
    else
        status=$?
    fi
    fail "Command failed with exit code $status: $1"
}
is_missing_uv_tool_error() {
    normalized=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    case "$normalized" in
        *"$PACKAGE_NAME"*"is not installed"*) return 0 ;;
        *) return 1 ;;
    esac
}
add_path_entry() {
    [ -n "$1" ] || return 0
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1:$PATH" ;;
    esac
}
add_known_uv_paths() {
    if [ -n "${XDG_BIN_HOME:-}" ]; then
        add_path_entry "$XDG_BIN_HOME"
    fi
    add_path_entry "$HOME/.local/bin"
    add_path_entry "$HOME/.cargo/bin"
    export PATH
    hash -r 2>/dev/null || true
}
fcc_process_ids() {
    command_name=$1
    if command -v pgrep >/dev/null 2>&1; then
        {
            pgrep -x "$command_name" 2>/dev/null || true
            pgrep -f "(^|/)${command_name}([[:space:]]|$)" 2>/dev/null || true
        } | sort -nu
        return 0
    fi
    ps -A -o pid= -o args= 2>/dev/null |
        awk -v command_name="$command_name" '
            BEGIN {
                pattern = "(^|/)" command_name "([[:space:]]|$)"
            }
            {
                process_id = $1
                sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "")
                if ($0 ~ pattern) {
                    print process_id
                }
            }
        ' || true
}
is_fcc_command_running() {
    [ -n "$(fcc_process_ids "$1")" ]
}
assert_no_fcc_processes_running() {
    running=""
    for command_name in $FCC_COMMANDS; do
        if is_fcc_command_running "$command_name"; then
            running="${running} ${command_name}"
        fi
    done
    if [ -n "$running" ]; then
        fail "Running Free Claude Code processes detected:${running}. Stop them before proceeding."
    fi
}
initialize_uv_context() {
    add_known_uv_paths
    if [ "$dry_run" -eq 1 ]; then
        print_command uv tool dir --bin
        return 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
        fail "uv is required for uninstallation. Install it first, then rerun this uninstaller."
    fi
    print_command uv tool dir --bin
    if uv_tool_bin=$(uv tool dir --bin); then
        :
    else
        status=$?
        fail "Could not determine the uv tool bin directory (exit code $status); ~/.fcc was not deleted."
    fi
    [ -n "$uv_tool_bin" ] || fail "uv returned an empty tool bin directory; ~/.fcc was not deleted."
}
remove_fcc_uv_tool() {
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi
    output_status=$(uv tool uninstall free-claude-code 2>&1) || true
    output=$(printf '%s\n' "$output_status" | head -n -0)
    _uv_exit=$?
    if is_missing_uv_tool_error "$output_status"; then
        printf 'Free Claude Code uv tool is already absent; verifying its entry points.\n'
        return 0
    fi
    if [ -n "$output_status" ]; then
        printf '%s\n' "$output_status"
    fi
    if [ "$_uv_exit" -ne 0 ]; then
        if [ -n "$output_status" ]; then
            printf '%s\n' "$output_status" >&2
        fi
        fail "uv tool uninstall failed with exit code $_uv_exit"
    fi
}
verify_fcc_commands_removed() {
    if [ "$dry_run" -eq 1 ]; then
        printf '+ verify all Free Claude Code entry points are absent from the uv tool bin directory\n'
        return 0
    fi
    remaining=""
    for command_name in $FCC_COMMANDS; do
        command_path="$uv_tool_bin/$command_name"
        if [ -e "$command_path" ] || [ -L "$command_path" ]; then
            remaining="${remaining} ${command_path}"
        fi
    done
    if [ -n "$remaining" ]; then
        fail "Free Claude Code entry points remain:${remaining}"
    fi
}
macos_app_is_fcc_owned() {
    app_dir=$1
    owner_file="$app_dir/Contents/$FCC_MACOS_OWNER_FILE"
    [ -d "$app_dir" ] &&
        [ ! -L "$app_dir" ] &&
        [ -f "$owner_file" ] &&
        [ "$(cat "$owner_file")" = "$FCC_MACOS_BUNDLE_ID" ]
}
remove_macos_desktop_app() {
    [ "$(uname -s)" = "Darwin" ] || return 0
    app_dir="$HOME/Applications/Free Claude Code.app"
    desktop_link="$HOME/Desktop/Free Claude Code.app"
    if ! macos_app_is_fcc_owned "$app_dir"; then
        if [ -e "$app_dir" ] || [ -L "$app_dir" ]; then
            printf 'An app not managed by Free Claude Code exists at %s; leaving it unchanged.\n' "$app_dir"
        fi
        if [ -e "$desktop_link" ] || [ -L "$desktop_link" ]; then
            printf 'The Free Claude Code desktop item cannot be verified; leaving it unchanged.\n'
        fi
        return 0
    fi
    if [ -L "$desktop_link" ]; then
        if [ "$(readlink "$desktop_link")" = "$app_dir" ]; then
            run rm -f "$desktop_link"
        else
            printf 'A non-FCC link exists at %s; leaving it unchanged.\n' "$desktop_link"
        fi
    elif [ -e "$desktop_link" ]; then
        printf 'A non-FCC item exists at %s; leaving it unchanged.\n' "$desktop_link"
    fi
    if [ -e "$app_dir" ]; then
        run rm -rf "$app_dir"
    fi
}
purge_fcc_home() {
    fcc_home="$HOME/$FCC_HOME_DIRNAME"
    if [ ! -e "$fcc_home" ]; then
        printf 'No FCC config directory at %s; skipping purge.\n' "$fcc_home"
        return 0
    fi
    run rm -rf "$fcc_home"
    if [ "$dry_run" -eq 0 ] && [ -e "$fcc_home" ]; then
        fail "FCC config directory still exists after deletion: $fcc_home"
    fi
}
parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dry-run)
                dry_run=1
                ;;
            --help|-h)
                show_usage
                exit 0
                ;;
            *)
                show_usage >&2
                fail "unknown option: $1"
                ;;
        esac
        shift
    done
}
parse_args "$@"
[ -n "${HOME:-}" ] || fail "HOME is not set; cannot locate Free Claude Code data."
step "Checking for running Free Claude Code processes"
assert_no_fcc_processes_running
step "Locating the uv-managed Free Claude Code installation"
initialize_uv_context
step "Removing the Free Claude Code uv tool"
remove_fcc_uv_tool
step "Verifying Free Claude Code entry points were removed"
verify_fcc_commands_removed
step "Removing the Free Claude Code desktop launcher"
remove_macos_desktop_app
step "Purging FCC config and data from ~/.fcc"
purge_fcc_home
if [ "$dry_run" -eq 1 ]; then
    printf '\nDry run complete. No changes were made.\n'
else
    printf '\nFree Claude Code has been removed and verified.\n'
    printf 'uv, Claude Code, Codex, Pi, the uv-managed Python runtime, and shared PATH entries were left installed.\n'
fi