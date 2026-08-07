# Hermes 업데이트 검증 체크리스트

이 문서는 Hermes Agent를 업데이트한 뒤 Unified Kanban 통합이 계속 동작하는지 매번 같은 기준으로 확인하기 위한 실행 체크리스트다. 명령은 Unified Kanban 저장소 루트에서 실행한다. 반복 가능한 테스트 목록은 [`hermes-update-test-cases.md`](hermes-update-test-cases.md), 최신 실측 결과는 [`hermes-update-verification-2026-08-07.md`](hermes-update-verification-2026-08-07.md)에 기록한다.

## 지원 및 검증 범위

- [ ] 실제 검증 환경이 macOS인지 기록한다.
- [ ] Windows와 WSL2는 지원 대상이 아님을 유지한다.
- [ ] Linux는 실제 머신 검증 전까지 공식 지원으로 표기하지 않는다.
- [ ] 비밀정보, API key, credential 값, `.env` 내용은 로그나 이 문서에 복사하지 않는다.

## 1. 사전 점검

업데이트 전 저장소와 Hermes checkout의 상태를 기록한다.

```bash
git status --short --branch
git log -3 --oneline

HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
git -C "$HERMES_AGENT_REPO" status --short --branch
hermes version
./scripts/update-hermes-if-needed.sh --check
```

- [ ] Unified Kanban working tree의 기존 변경을 확인하고 보존한다.
- [ ] Hermes checkout이 clean인지 확인한다. 업데이터는 dirty checkout을 `--force`로도 덮어쓰지 않는다.
- [ ] `hermes version`의 install directory가 점검할 checkout과 같은지 확인한다.
- [ ] 현재 upstream SHA, local SHA, carried commit 개수를 증거 템플릿에 기록한다.
- [ ] `patches/hermes-agent-supported-upstream`이 검증 대상 upstream의 full 40-character SHA인지 확인한다. setup은 이 값과 checkout의 `origin/main`이 다르면 모든 설치 write 전에 중단한다.
- [ ] `--check` 결과가 `UP_TO_DATE`인지 `UPDATE_AVAILABLE: N commits behind origin/main`인지 기록한다.
- [ ] Dashboard가 실행 중이었다면 PID와 `/api/status` 응답 여부를 기록한다.

OpenAI Codex provider를 사용하는 경우 credential 값을 출력하지 않고 인증 상태만 확인한다.

```bash
hermes auth status openai-codex
```

- [ ] `openai-codex: logged in`인지 확인한다.
- [ ] 로그인되지 않은 경우에만 `hermes auth add openai-codex`를 실행하고 사용자가 device-code 승인을 완료하도록 한다. 이미 유효한 credential이 있으면 중복 credential을 만들기 위해 다시 실행하지 않는다.
- [ ] device code, access token, refresh token은 작업 기록이나 문서에 남기지 않는다.

```bash
pgrep -f 'hermes dashboard' || true
HERMES_DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"
HERMES_DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
case "$HERMES_DASHBOARD_HOST" in
  0.0.0.0) HERMES_DASHBOARD_PROBE_HOST="127.0.0.1" ;;
  ::|::1) HERMES_DASHBOARD_PROBE_HOST="[::1]" ;;
  *) HERMES_DASHBOARD_PROBE_HOST="$HERMES_DASHBOARD_HOST" ;;
esac
curl --fail --silent --show-error --max-time 2 \
  "http://$HERMES_DASHBOARD_PROBE_HOST:$HERMES_DASHBOARD_PORT/api/status" \
  >/dev/null && echo DASHBOARD_READY
```

`--check`는 upstream fetch와 동시 실행 보호용 lock의 일시 생성·제거 외에 pending marker나 서비스 상태를 변경하지 않아야 한다.

## 2. carried manifest와 bundle 검증

저장소가 다른 컴퓨터에서도 필요한 commit object를 공급할 수 있어야 한다.

```bash
(
set -euo pipefail
HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
BUNDLE="$PWD/patches/hermes-agent-carried.bundle"
[[ -f "$BUNDLE" && ! -L "$BUNDLE" ]] || {
  echo "bundle is missing, not a regular file, or is a symlink: $BUNDLE" >&2
  exit 1
}
git -C "$HERMES_AGENT_REPO" bundle verify "$BUNDLE"
git bundle list-heads "$BUNDLE"

python3 - <<'PY'
from collections import Counter
from pathlib import Path

items = []
for raw in Path("patches/hermes-agent-carried-commits").read_text().splitlines():
    value = raw.split("#", 1)[0].strip()
    if value:
        items.append(value)
duplicates = [sha for sha, count in Counter(items).items() if count > 1]
raise SystemExit(f"duplicate carried commits: {duplicates}" if duplicates else 0)
PY

while IFS= read -r line || [[ -n "$line" ]]; do
  commit="${line%%#*}"
  commit="${commit//[[:space:]]/}"
  [[ -n "$commit" ]] || continue
  git -C "$HERMES_AGENT_REPO" cat-file -e "$commit^{commit}" || exit 1
  output="$(git -C "$HERMES_AGENT_REPO" cherry HEAD "$commit")" || exit 1
  match="$(printf '%s\n' "$output" | python3 -c '
import sys
target = sys.argv[1]
matches = []
for row in sys.stdin:
    parts = row.split()
    if len(parts) == 2 and (parts[1].startswith(target) or target.startswith(parts[1])):
        matches.append(parts[0])
if len(matches) > 1:
    raise SystemExit(1)
print(matches[0] if matches else "-")
' "$commit")" || exit 1
  printf '%s %s\n' "$match" "$commit"
  [[ "$match" == "-" ]] || exit 1
done < patches/hermes-agent-carried-commits
)
```

- [ ] `patches/hermes-agent-carried.bundle`이 일반 파일이며 심볼릭 링크가 아닌지 확인한다.
- [ ] `git bundle verify`가 성공하는지 확인한다.
- [ ] `git bundle list-heads`가 현재 bundle 생성 계약인 `refs/heads/carried-*` 11개를 출력하는지 확인한다.
- [ ] manifest의 공백·주석 제외 항목이 유효한 commit object인지 확인한다.
- [ ] 중복 commit SHA가 없는지 확인한다.
- [ ] 각 대상 SHA에 해당하는 `git cherry` 행이 `-`인지 확인한다. stack의 뒤쪽 commit은 선행 commit 행까지 여러 줄로 출력할 수 있으므로 대상 SHA 행만 판정한다. `+`는 아직 적용되지 않은 patch다.
- [ ] manifest를 바꿨다면 bundle도 함께 갱신하고, carried commit이 없는 fresh Hermes upstream clone에서 bundle fetch를 별도로 검증한다. 이 bundle은 upstream history를 prerequisite로 가지므로 빈 Git repository를 기준으로 삼지 않는다.

fresh upstream clone 검증은 다음처럼 실행한다. 임시 clone은 현재 머신의 carried refs나 object database를 공유하지 않는다.

```bash
(
set -euo pipefail
HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
BUNDLE="$PWD/patches/hermes-agent-carried.bundle"
MANIFEST="$PWD/patches/hermes-agent-carried-commits"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TMP_ROOT"' EXIT
git clone --no-local --no-tags --single-branch --branch main \
  "$(git -C "$HERMES_AGENT_REPO" remote get-url origin)" \
  "$TMP_ROOT/hermes-upstream"
git -C "$TMP_ROOT/hermes-upstream" fetch "$BUNDLE" \
  '+refs/heads/*:refs/unified-kanban/carried/*'
while IFS= read -r line || [[ -n "$line" ]]; do
  commit="${line%%#*}"
  commit="${commit//[[:space:]]/}"
  [[ -n "$commit" ]] || continue
  git -C "$TMP_ROOT/hermes-upstream" cat-file -e "$commit^{commit}" || exit 1
done < "$MANIFEST"
echo FRESH_BUNDLE_IMPORT_PASS
)
```

`git log --contains`만으로 대체하지 않는다. 동일 patch가 다른 SHA로 cherry-pick됐을 수 있으므로 patch-equivalence를 확인하는 `git cherry`가 기준이다.

## 3. 업데이트 실행

```bash
./scripts/update-hermes-if-needed.sh
```

정상 경로는 다음 중 하나다.

- `SKIPPED`: upstream과 carried patch가 모두 현재 상태다. setup, dependency sync, Gateway/Dashboard restart가 일어나면 안 된다.
- `REPAIRING`: upstream은 현재지만 carried patch가 빠졌거나 마지막 적용 upstream 상태가 오래되어 patch와 setup을 복구한다.
- `RESUMING`: 이전 실패의 pending marker를 읽어 post-update repair를 재개한다.
- 실제 update: `hermes update --yes --branch main` 후 carried commit 적용, setup, 필요할 때 Dashboard 복원을 수행한다.

- [ ] update target의 전체 upstream SHA를 기록한다.
- [ ] carried commit이 순서대로 재적용되거나 이미 적용된 것으로 판정됐는지 확인한다.
- [ ] update 전 Dashboard가 실행 중이었다면 update 후에도 준비 상태인지 확인한다.
- [ ] update 전 Dashboard가 중지 상태였다면 업데이터가 임의로 계속 실행시키지 않았는지 확인한다.
- [ ] `UPDATED`, `REPAIRING`, `RESUMING` 후에는 `${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-last-applied-sha`가 target upstream SHA인지 확인한다. 최초부터 정상인 `SKIPPED` 경로는 이 파일을 새로 만들지 않는 것이 정상이다.
- [ ] 성공 후 `${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-update.pending`이 남지 않았는지 확인한다.
- [ ] `${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-update.lock`이 남지 않았는지 확인한다.

업데이트 직후 다시 확인한다.

```bash
hermes version
./scripts/update-hermes-if-needed.sh --check
git -C "${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}" status --short --branch
```

예상 결과는 새 upstream SHA, configured carried commit 개수, `UP_TO_DATE`, clean Hermes checkout이다.

## 4. 설치 호환성과 멱등성

먼저 비파괴 preflight를 실행한다.

```bash
./scripts/setup.sh --dry-run --no-restart --skip-smoke
```

- [ ] Hermes CLI upstream SHA, checkout의 `origin/main`, `patches/hermes-agent-supported-upstream`의 세 값이 일치하는지 확인한다.
- [ ] 필요한 `hermes kanban` CLI option 검사가 성공하는지 확인한다.
- [ ] dry-run이 Git object, hook 설정, symlink, plugin, board, Gateway를 변경하지 않는지 확인한다.
- [ ] 외부 소유 파일이나 다른 대상을 가리키는 symlink를 덮어쓰지 않는지 확인한다.

실제 setup을 다시 실행해 멱등성을 확인한다.

```bash
./scripts/setup.sh --no-restart --skip-smoke
./scripts/setup.sh --no-restart --skip-smoke
```

- [ ] 관리형 링크가 동일한 저장소 파일을 가리킨다.
- [ ] Claude/Codex hook 설정이 중복되지 않는다.
- [ ] Hermes plugin이 활성 상태다.
- [ ] 두 번째 setup이 새로운 항목을 중복 생성하지 않는다.

## 5. Unified Kanban 전체 테스트

프로젝트 virtual environment를 사용한다.

```bash
uv --version
uv sync --frozen --group dev
.venv/bin/python -m pytest -q
bash -n \
  scripts/setup.sh \
  scripts/uninstall.sh \
  scripts/update-hermes-if-needed.sh \
  scripts/kanban-smoke.sh
git diff --check
```

- [ ] 전체 pytest가 통과한다. 현재 기준은 **654 passed**다. 테스트 수가 달라지면 의도된 추가/삭제인지 diff로 확인한다.
- [ ] `uv`가 없다면 테스트를 건너뛰지 말고 먼저 공식 Hermes 설치 또는 uv 설치 절차로 준비한다. 애플리케이션 설치 자체와 달리 이 검증 단계에는 `uv`가 필요하다.
- [ ] 현재 검증한 uv 기준은 **0.11.11**이다. 더 오래된 uv가 `--isolated`, `--frozen`, `--extra` 조합을 지원하지 않으면 버전을 올리고 동일 명령을 다시 실행한다.
- [ ] setup, uninstall, updater, smoke script의 Bash syntax가 유효하다.
- [ ] whitespace error가 없다.

## 6. Hermes carried-path 회귀 테스트

Hermes runtime venv에는 pytest가 없을 수 있으므로 runtime 환경을 변경하지 않는 isolated dev environment를 사용한다.

```bash
HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
(cd "$HERMES_AGENT_REPO" && uv run --isolated --frozen --extra dev pytest \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_observation.py \
  tests/plugins/test_kanban_dashboard_plugin.py \
  tests/plugins/test_kanban_token_usage.py \
  -q)
```

- [ ] 현재 기준 **93 passed, 1 skipped**를 만족한다.
- [ ] warning이 생기면 새 warning인지와 기능 영향 여부를 기록한다. 현재 확인된 Starlette/httpx deprecation warning은 별도 upstream 추적 대상으로 남긴다.
- [ ] collection error를 기능 실패로 오판하지 않는다. `No module named pytest/fastapi`이면 runtime venv가 아니라 위 isolated dev 명령을 사용한다.

## 7. 실제 Kanban smoke

Unified Kanban 저장소 루트로 돌아와 실제 CLI 왕복을 검증한다.

```bash
./scripts/kanban-smoke.sh
```

예상 결과:

```text
SMOKE PASS: archived card on board unified-kanban-smoke
```

- [ ] board list/create가 동작한다.
- [ ] 카드 create → comment → complete → show → archive가 성공한다.
- [ ] 테스트 카드는 마지막에 archive된다.
- [ ] smoke 실패를 단순 재실행으로 숨기지 않고 실패 단계와 Hermes CLI 출력을 기록한다.

## 8. 서비스와 두 번째 실행 검증

```bash
HERMES_DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"
HERMES_DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
case "$HERMES_DASHBOARD_HOST" in
  0.0.0.0) HERMES_DASHBOARD_PROBE_HOST="127.0.0.1" ;;
  ::|::1) HERMES_DASHBOARD_PROBE_HOST="[::1]" ;;
  *) HERMES_DASHBOARD_PROBE_HOST="$HERMES_DASHBOARD_HOST" ;;
esac
curl --fail --silent --show-error --max-time 2 \
  "http://$HERMES_DASHBOARD_PROBE_HOST:$HERMES_DASHBOARD_PORT/api/status" \
  >/dev/null && echo DASHBOARD_READY
hermes kanban boards list --json >/dev/null && echo KANBAN_CLI_READY
hermes status
```

Dashboard가 update 전에 실행 중이었던 경우 다음을 확인한다.

- [ ] `/api/status`가 성공한다.
- [ ] Dashboard가 새 Hermes checkout으로 재시작됐다.
- [ ] Gateway가 정상 상태다.
- [ ] `hermes kanban boards list --json`이 성공한다. plugin API는 Dashboard 인증을 요구하므로 인증 없는 raw `curl`의 401을 기능 실패로 오판하지 않는다.
- [ ] 브라우저의 인증된 Dashboard에서 Kanban 화면을 열어 board list와 carried UI가 실제로 렌더링되는지 확인한다. `/api/status`만으로 `--skip-build`로 시작한 frontend의 호환성을 승인하지 않는다.

업데이터를 한 번 더 실행한다.

```bash
pgrep -f 'hermes dashboard' || true
./scripts/update-hermes-if-needed.sh
pgrep -f 'hermes dashboard' || true
```

- [ ] 출력이 `SKIPPED: Hermes Agent already matches origin/main`이다.
- [ ] `hermes update`, setup, dependency sync가 다시 실행되지 않는다.
- [ ] 두 `pgrep` 결과가 같아 Dashboard가 재시작되지 않았음을 확인한다. 기동 방식에 따라 `pgrep`가 비어 있을 수 있으므로 `/api/status`와 함께 판정한다.
- [ ] pending/applied state의 timestamp가 불필요하게 바뀌지 않았는지 확인한다. lock directory는 `SKIPPED` 실행에서도 동시 실행 보호를 위해 잠시 생성됐다가 제거되는 것이 정상이다.

## 9. 실패 복구

### dirty checkout

- 사용자 변경을 commit/stash/revert할 주체와 범위를 확인한다.
- 업데이터의 `--force`는 하위 호환 no-op이며 dirty checkout 우회 수단이 아니다.
- 승인 없이 `git reset --hard`, `git clean`, stash 삭제를 실행하지 않는다.

### pending marker가 남은 경우

```bash
sed -n '1,2p' "${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-update.pending"
./scripts/update-hermes-if-needed.sh
```

- pending 첫 줄은 target SHA, 둘째 줄은 update 전 Dashboard 실행 상태다.
- checkout이 clean한지 확인한 뒤 같은 업데이터를 다시 실행한다. upstream이 그대로면 `RESUMING` 경로가 post-update repair를 완료한다. 그 사이 upstream이 다시 전진했다면 일반 update 경로가 새 target으로 pending을 갱신하고 repair를 완료할 수 있다.
- 성공을 확인하기 전에 pending marker를 수동 삭제하지 않는다.

### lock이 남은 경우

```bash
cat "${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-update.lock/pid"
```

- PID가 살아 있고 해당 PID의 process command가 실제 updater인지 확인되면 다른 update가 진행 중이므로 기다린다. PID 재사용 가능성이 있으므로 숫자만 보고 소유권을 단정하지 않는다. live lock을 삭제하지 않는다.
- PID가 죽었다면 다음 업데이터 실행이 stale lock을 회수한다.
- lock directory를 먼저 수동 삭제해 동시 실행 보호를 우회하지 않는다.

### cherry-pick conflict

- conflict 파일과 carried commit을 기록하고 자동으로 `--skip`하지 않는다.
- patch가 upstream에 동등하게 포함됐는지 확인한다.
- conflict를 해결할 수 없다면 `git cherry-pick --abort`로 clean checkout을 복구하고 pending marker를 보존한 채 원인을 수정한다.
- manifest 또는 bundle을 변경하려면 회귀 테스트와 fresh import 검증을 함께 갱신한다.

### rollback 원칙

- 사용자 승인 없이 Hermes checkout을 임의의 SHA로 hard reset하지 않는다.
- update 전 SHA, target SHA, Dashboard 상태와 실패 로그를 보존한다.
- 이전 버전 복원이 필요하면 Hermes 공식 update/install 절차와 저장소 carried patch 호환성을 별도로 검증한다.
- 적용 여부를 속이기 위해 applied SHA나 pending marker를 수동 편집하지 않는다.

## 10. 업데이트 증거 템플릿

매 업데이트 결과를 이 형식으로 작업 기록이나 PR에 남긴다. credential과 원문 세션 데이터는 제외한다.

```text
Hermes update verification
- Date/time:
- Operator/model:
- OpenAI Codex auth status: logged in / unavailable
- Platform: macOS version / architecture
- Unified Kanban commit:
- Before: Hermes upstream/local/carried:
- Update check: UP_TO_DATE | UPDATE_AVAILABLE N
- Target upstream SHA:
- Update path: SKIPPED | REPAIRING | RESUMING | UPDATED
- Carried manifest entries:
- Bundle verify: PASS/FAIL
- Patch-equivalence: PASS/FAIL
- Setup dry-run: PASS/FAIL
- Setup idempotency: PASS/FAIL
- Unified Kanban pytest: passed/failed count
- Hermes carried-path pytest: passed/skipped/failed count
- Kanban smoke: PASS/FAIL
- Gateway after update: PASS/FAIL
- Dashboard before/after PID and health:
- Second updater run: SKIPPED/PASS/FAIL
- Pending marker after success: absent/present
- Lock after success: absent/present
- Warnings/new deprecations:
- Changed files or compatibility fixes:
- Remaining risks:
```

모든 항목이 통과한 뒤에만 업데이트 호환성이 검증됐다고 보고한다.
