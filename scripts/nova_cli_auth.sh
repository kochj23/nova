# nova_cli_auth.sh — fleet-store-backed auth for the interactive CLIs (claude, gh).
#
# SOURCE this from an interactive/login shell; it never runs for the ~3k/day
# non-interactive ssh commands (Ubuntu's .bashrc returns early for those), so the
# two PG round-trips below are a login-time cost only.
#
# Both tokens come from the fleet secret store (1Password vault "Nova" -> nova.secrets,
# read through the `security find-generic-password -s NAME -w` shim that answers from PG
# on Linux and from the real Keychain on macOS). NOTHING is written to disk here: a
# rotation in 1Password reaches every node on the next login, and there is no stale
# copy to chase (the legacy ~/.config/nova/claude-oauth-token caches went 18 days stale).
#
# Secret names:
#   claude-code-oauth-token  long-lived `claude setup-token` token -> CLAUDE_CODE_OAUTH_TOKEN
#   nova-github-token        gh CLI OAuth token, account kochj23   -> GH_TOKEN / GITHUB_TOKEN
#
# Deployed per node as ~/.config/nova/cli-auth.sh (a LOCAL copy, like core4's
# scripts-fleet: a login shell must not need the NAS). Keep both in sync.
# 2026-10-21.

[ -n "${NOVA_CLI_AUTH_DONE:-}" ] && return 0
NOVA_CLI_AUTH_DONE=1

_nova_cli_auth_fetch() {
    command -v security >/dev/null 2>&1 || return 1
    security find-generic-password -s "$1" -w 2>/dev/null
}

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    _t=$(_nova_cli_auth_fetch claude-code-oauth-token)
    # Fall back to the legacy 0600 cache only if the store is unreachable (PG down / WAN down).
    [ -z "$_t" ] && [ -s "$HOME/.config/nova/claude-oauth-token" ] &&
        _t=$(cat "$HOME/.config/nova/claude-oauth-token")
    case "$_t" in sk-ant-oat*) export CLAUDE_CODE_OAUTH_TOKEN="$_t" ;; esac
fi

if [ -z "${GH_TOKEN:-}" ]; then
    _t=$(_nova_cli_auth_fetch nova-github-token)
    case "$_t" in gh[po]_*|github_pat_*) export GH_TOKEN="$_t" GITHUB_TOKEN="$_t" ;; esac
fi

unset _t
