# Security Policy

## Supported versions

Until the first tagged stable release, security fixes target the current `main` branch. Older commits and unsupported Hermes upstream SHAs are not maintained.

## Reporting a vulnerability

Please do not open a public issue for suspected vulnerabilities. Use GitHub's **Report a vulnerability** / private security advisory flow for this repository:

`https://github.com/LeeYuHoon/unified-kanban/security/advisories/new`

Include affected revision, reproduction steps, impact, and any suggested mitigation. Do not include real credentials, private session transcripts, Kanban databases, or personal paths. The maintainer will acknowledge a complete report when it is reviewed and will coordinate disclosure after a fix is available.

## Security boundaries

Unified Kanban executes hooks with the local user's permissions and reads local Claude, Codex, and Hermes metadata. It intentionally:

- stores implementation only in this repository and installs managed links;
- refuses when the reviewed pin, `origin/main`, active checkout `HEAD`, final carried commit, or
  CLI-reported Hermes upstream disagree;
- treats malformed or unreadable compatibility state as incompatible;
- does not store prompt bodies, tool arguments, or intermediate transcripts in cards;
- creates private state files and rejects symlink substitution;
- does not require API keys or ship credential files.

A report that shows these boundaries can be bypassed is security-sensitive. Operational failures that do not cross a trust boundary may be filed as normal bug reports after removing private data.
