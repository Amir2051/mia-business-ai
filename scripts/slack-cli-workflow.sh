#!/usr/bin/env bash
set -Eeuo pipefail

# Guided Slack CLI workflow for MIA/RZ Agent. Run locally on a trusted machine.
# This script never reads, prints, stores, or uploads Slack tokens or passwords.
INSTALL_URL="https://downloads.slack-edge.com/slack-cli/install.sh"

usage() {
  cat <<'USAGE'
MIA Slack CLI workflow

Usage:
  ./scripts/slack-cli-workflow.sh [check|install|login|create|run|guide]

Commands:
  check    Check whether Slack CLI is installed and show its version.
  install  Download the official installer to a temporary file, show its source,
           then run it only after interactive confirmation.
  login    Start Slack CLI workspace authentication (interactive).
  create   Create a Slack app project using the Slack CLI (interactive).
  run      Run the current Slack app project (run inside its project directory).
  guide    Print the recommended workflow (default).

Recommended sequence:
  ./scripts/slack-cli-workflow.sh check
  ./scripts/slack-cli-workflow.sh install   # only if needed
  ./scripts/slack-cli-workflow.sh login
  ./scripts/slack-cli-workflow.sh create
  cd <new-project-directory>
  ./scripts/slack-cli-workflow.sh run

Official docs: https://docs.slack.dev/quickstart
USAGE
}

check_cli() {
  if ! command -v slack >/dev/null 2>&1; then
    printf 'Slack CLI: not installed (slack command not found).\n'
    printf 'Next step: %s install\n' "$0"
    return 1
  fi
  printf 'Slack CLI found at: %s\n' "$(command -v slack)"
  slack --version
}

install_cli() {
  if command -v slack >/dev/null 2>&1; then
    printf 'Slack CLI is already installed.\n'
    slack --version
    return 0
  fi
  command -v curl >/dev/null 2>&1 || { echo 'curl is required to download the official installer.' >&2; return 1; }
  command -v bash >/dev/null 2>&1 || { echo 'bash is required to run the official installer.' >&2; return 1; }

  local tmp
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  echo "Official installer source: $INSTALL_URL"
  echo 'Downloading the installer to a temporary file for review (not piping directly to bash)…'
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$INSTALL_URL" -o "$tmp"
  echo
  echo 'Installer preview (first 35 lines):'
  sed -n '1,35p' "$tmp"
  echo
  read -r -p 'Run this downloaded installer now? [y/N] ' answer
  case "$answer" in
    y|Y|yes|YES) bash "$tmp" ;;
    *) echo 'Cancelled. No installer was run.'; return 1 ;;
  esac
  trap - EXIT
  rm -f "$tmp"
  check_cli
}

login_cli() {
  command -v slack >/dev/null 2>&1 || { echo 'Slack CLI is not installed. Run: scripts/slack-cli-workflow.sh install' >&2; return 1; }
  echo 'This opens Slack CLI authentication. Complete the workspace authorization in the browser/terminal.'
  echo 'Do not paste Slack tokens, client secrets, or passwords into MIA chat or source files.'
  slack login
}

create_project() {
  command -v slack >/dev/null 2>&1 || { echo 'Slack CLI is not installed. Run: scripts/slack-cli-workflow.sh install' >&2; return 1; }
  echo 'Slack CLI will interactively create a project from a starter template.'
  echo 'Choose a project name and location you control. Review generated files before adding secrets.'
  slack create
}

run_project() {
  command -v slack >/dev/null 2>&1 || { echo 'Slack CLI is not installed. Run: scripts/slack-cli-workflow.sh install' >&2; return 1; }
  if [[ ! -f "slack.json" && ! -f "manifest.json" ]]; then
    echo 'No Slack app manifest found in the current directory.'
    echo 'Change into the project directory created by slack create, then run this command again.'
    return 1
  fi
  echo 'Starting the Slack app locally. Keep this terminal open and stop with Ctrl+C.'
  slack run
}

guide() {
  cat <<'GUIDE'
MIA Slack CLI setup
1. Check installation: ./scripts/slack-cli-workflow.sh check
2. Install if needed:  ./scripts/slack-cli-workflow.sh install
3. Authenticate:      ./scripts/slack-cli-workflow.sh login
4. Create a project:  ./scripts/slack-cli-workflow.sh create
5. Enter the project directory created by Slack CLI.
6. Run locally:       ./scripts/slack-cli-workflow.sh run

Security:
- The installer is downloaded over HTTPS to a temporary file and requires confirmation before execution.
- Authentication is handled by Slack CLI. This helper does not request or persist credentials.
- Never commit .env files, access tokens, signing secrets, or client secrets.
- Keep permissions to the minimum your app needs; review the app manifest and scopes.
- `slack run` runs the app locally; it does not deploy it to production.
Official docs: https://docs.slack.dev/quickstart
GUIDE
}

action="${1:-guide}"
case "$action" in
  check) check_cli ;;
  install) install_cli ;;
  login) login_cli ;;
  create) create_project ;;
  run) run_project ;;
  guide|-h|--help) usage; guide ;;
  *) usage >&2; exit 2 ;;
esac
