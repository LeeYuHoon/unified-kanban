# Unified Kanban

[한국어](README.md) | English

Unified Kanban shows work from Hermes Agent, Claude Code, and Codex CLI in one Hermes Kanban board.

> Only **macOS is supported** right now.

## What does it do?

A card is created for each real user request. When the response finishes, the card stores the result and available usage details.

- See Hermes, Claude Code, and Codex work on one board.
- Choose the right board from the current project folder.
- Keep existing hooks and settings while adding only managed entries.
- Skip duplicate cards for automatic notifications and internal helper work.

For implementation and security details, see the [implementation specification](docs/unified-kanban-spec.md) and [maintenance guide](docs/maintenance.md).

## Install

### Requirements

- macOS
- On a new Mac: Git, Bash, and `curl`. Setup prepares the remaining Hermes tools.
- For an existing Hermes installation: Python 3.11+, Hermes CLI, Git, `uv`, Node, and `npm`
- GitHub access to this repository

This repository may be private. If clone reports `Repository not found`, check your repository access and GitHub login first.

Keep the cloned folder in a permanent location. Installed links point to it.

### New Mac

If Hermes is not installed yet, do not run a separate Hermes installer first.

```bash
cd "$HOME"
git clone https://github.com/LeeYuHoon/unified-kanban.git
cd unified-kanban
./scripts/setup.sh
```

Then configure Hermes and create the smoke-test board:

```bash
export PATH="$HOME/.local/bin:$PATH"
hermes setup
hermes kanban boards create --name "Unified Kanban Smoke" unified-kanban-smoke
./scripts/kanban-smoke.sh
```

The smoke test creates, updates, completes, and archives a temporary card.

### Existing Hermes user

Setup does not reset or edit your existing Hermes source folder. It also preserves your configuration, authentication, boards, and cards. Setup builds a separate verified Hermes copy and switches the managed launcher to it. It adds only the managed Hermes configuration needed to enable the Unified Kanban plugin.

Preview the changes, then install:

```bash
./scripts/setup.sh --dry-run --no-restart --skip-smoke
./scripts/setup.sh
```

If your Hermes source folder is not in the default location, provide its absolute path:

```bash
HERMES_AGENT_REPO="/absolute/path/to/hermes-agent" ./scripts/setup.sh
```

If a managed link or service conflicts with another installation, the managed state is incomplete, or file permissions are unsafe, setup stops without overwriting it. Do not delete or reinstall Hermes to hide the error; read the error first.

Quit and reopen any Hermes CLI/TUI/Desktop, Claude Code, and Codex CLI processes that were running before installation.

## Use

Open the Dashboard:

```bash
hermes dashboard
```

1. Open **Kanban** and create or select a board.
2. Set **Project directory** to the absolute path of the project you work in.
3. Run Hermes, Claude Code, or Codex in that folder or one of its subfolders.

Each real user request should now create a card and save the result when the work finishes. The final response is stored on the card, so do not include passwords, API keys, or other sensitive information in requests or responses.

Check the installation with:

```bash
command -v kanban-adapter
hermes kanban boards list --json
./scripts/kanban-smoke.sh
```

### Update

```bash
cd "$HOME/unified-kanban"  # Replace with your clone location.
git pull --ff-only
./scripts/update-hermes-if-needed.sh
./scripts/setup.sh
```

Updates preserve the existing Hermes checkout, user configuration, authentication, boards, and cards.

### Troubleshooting

| Symptom | What to do |
| --- | --- |
| `Repository not found` | Check GitHub authentication and repository access. |
| `hermes: command not found` | Open a new terminal or run `export PATH="$HOME/.local/bin:$PATH"`. |
| `Hermes Agent checkout not found` | On a new Mac, run the real `./scripts/setup.sh` instead of dry-run. For an existing installation, set an absolute `HERMES_AGENT_REPO`. |
| `Hermes version mismatch` | Run the update script, then run setup again. |
| `Refusing foreign ...` | Do not delete files blindly. If they came from an older checkout, run its uninstall script first. |
| Smoke test fails | Run `hermes doctor`, list boards, then rerun `./scripts/kanban-smoke.sh`. |

Keep the original error message and consult the [Hermes update checklist](docs/hermes-update-checklist.md) if the problem continues.

## Uninstall

Remove only the links and integration entries managed by Unified Kanban:

```bash
cd "$HOME/unified-kanban"  # Replace with your clone location.
./scripts/uninstall.sh
```

Uninstall keeps your existing Hermes checkout, configuration, authentication, boards, and cards. It also keeps a small amount of state used for safe reinstall and troubleshooting.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [SECURITY.md](SECURITY.md) for private security reports.
