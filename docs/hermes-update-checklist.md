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
- [ ] Hermes checkout은 읽기 전용 입력이다. 업데이터는 checkout을 절대 변경하지 않으므로 dirty 여부가 활성화를 막지 않는다. 대신 활성화 전후로 checkout이 변하지 않았음을 확인한다.
- [ ] `hermes version`의 install directory가 현재 선택된 release(`<HERMES_AGENT_REPO>.releases/current`가 가리키는 경로)와 같은지 확인한다.
- [ ] 현재 선택된 release, 검증된 carried head, carried commit 개수를 증거 템플릿에 기록한다.
- [ ] release 시작 시 official provenance를 확인한 full 40-character SHA가 `patches/hermes-agent-supported-upstream`에 고정됐는지 확인한다. setup과 updater는 fixed official HTTPS 저장소에서 그 exact object를 가져와 identity를 검증하며, 이후 `main` 이동은 다음 maintenance cycle에서 처리한다.
- [ ] `--check` 결과가 `UP_TO_DATE`인지 `UPDATE_AVAILABLE`인지 기록한다.
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

`--check`는 selector를 읽기만 한다. lock, pending marker, release, 서비스 상태를 만들거나 바꾸지 않아야 하며 네트워크 조회도 하지 않는다.

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
- [ ] `git bundle list-heads`가 현재 bundle 생성 계약인 `refs/heads/carried-*` 13개를 출력하는지 확인한다.
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

업데이터는 checkout을 변경하지 않는다. 검증된 release를 `<HERMES_AGENT_REPO>.releases/release-<carried>`에 불변으로 구성하고, path transaction 안에서 regular selector 파일 하나만 교체한 뒤, 실행 중이던 서비스만 재시작한다. 정상 경로는 다음 중 하나다.

- `SKIPPED`: 검증된 release가 이미 선택돼 있고 pending marker도 없다. release 준비, bundle 검증, upstream 조회, 서비스 restart, state·lock write가 일어나면 안 된다.
- `RESUMING`: 이전 실행이 selector 교체 후 서비스 restart 전에 끊겼다. pending marker를 근거로 restart만 완료한다.
- 실제 활성화: official HTTPS 저장소에서 frozen pin의 exact object와 reviewed bundle identity를 검증해 release를 준비하거나 기존 release를 정확히 재검증하고, selector를 교체한 뒤 서비스를 재시작한다. live `main`은 informational observation이며 현재 frozen release authority가 아니다.

- [ ] 활성화한 release 경로와 carried head를 기록한다.
- [ ] `<HERMES_AGENT_REPO>.releases/current`의 내용이 그 release 경로와 정확히 같은지 확인한다.
- [ ] 이전 release directory가 삭제되지 않았는지 확인한다. 업데이터는 사용 중인 release를 증명할 수 없으므로 어떤 release도 지우지 않는다.
- [ ] update 전 Dashboard가 실행 중이었다면 update 후에도 준비 상태인지 확인한다.
- [ ] update 전 Dashboard가 중지 상태였다면 업데이터가 임의로 계속 실행시키지 않았는지 확인한다.
- [ ] 성공 후 `${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-update.pending`을 `update-state.py read pending`으로 읽었을 때 exit 3(논리적으로 없음)인지 확인한다. inode race를 피하기 위한 `cleared` tombstone 파일은 남는 것이 정상이다. `--no-restart`로 활성화만 한 경우에는 restart가 남아 있으므로 pending marker가 유지되는 것이 정상이다.
- [ ] `${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-update.lock`이 남지 않았는지 확인한다.

업데이트 직후 다시 확인한다. checkout이 그대로인지도 함께 본다.

```bash
hermes version
./scripts/update-hermes-if-needed.sh --check
HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
cat "$HERMES_AGENT_REPO.releases/current"
git -C "$HERMES_AGENT_REPO" status --short --branch
```

예상 결과는 새 release 경로, `UP_TO_DATE`, 그리고 활성화 전과 동일한 Hermes checkout이다.

## 4. 설치 호환성과 멱등성

먼저 비파괴 preflight를 실행한다.

```bash
./scripts/setup.sh --dry-run --no-restart --skip-smoke
```

- [ ] Hermes CLI가 보고하는 upstream SHA와 선택된 release의 completion receipt, `patches/hermes-agent-supported-upstream`의 frozen SHA가 일치하는지 확인한다. checkout의 moving `origin/main`은 equality authority로 사용하지 않는다.
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

- [ ] 전체 pytest가 통과한다. 테스트 수가 달라지면 의도된 추가/삭제인지 diff로 확인하고, 고정된 과거 개수를 통과 기준으로 사용하지 않는다.
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

- [ ] 현재 기준 **99 passed, 1 skipped**를 만족한다.
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
- [ ] Dashboard가 새로 선택된 release로 재시작됐다.
- [ ] Gateway가 정상 상태다.
- [ ] `hermes kanban boards list --json`이 성공한다. plugin API는 Dashboard 인증을 요구하므로 인증 없는 raw `curl`의 401을 기능 실패로 오판하지 않는다.
- [ ] 브라우저의 인증된 Dashboard에서 Kanban 화면을 열어 board list와 carried UI가 실제로 렌더링되는지 확인한다. `/api/status`만으로 `--skip-build`로 시작한 frontend의 호환성을 승인하지 않는다.

업데이터를 한 번 더 실행한다.

```bash
pgrep -f 'hermes dashboard' || true
./scripts/update-hermes-if-needed.sh
pgrep -f 'hermes dashboard' || true
```

- [ ] 출력이 `SKIPPED: Hermes release <carried>는 이미 선택됨`에 해당하는 `SKIPPED:` 줄이다.
- [ ] release 준비, bundle 검증, upstream 조회, 서비스 restart가 다시 실행되지 않는다.
- [ ] selector 파일의 inode가 바뀌지 않았다.
- [ ] 두 `pgrep` 결과가 같아 Dashboard가 재시작되지 않았음을 확인한다. 기동 방식에 따라 `pgrep`가 비어 있을 수 있으므로 `/api/status`와 함께 판정한다.
- [ ] `SKIPPED` 경로는 lock도 pending marker도 state directory도 만들지 않는다. no-change 판정은 selector와 pending marker 두 번의 읽기만으로 lock 획득 전에 끝나고, lock directory는 활성화가 필요한 실행에서만 잠시 생성됐다가 제거된다.

## 9. 실패 복구

### 활성화 실패

- 업데이터는 실패 시 이전 selector를 되돌리고, 재시작했던 서비스를 복원된 release로 되돌린다. 실패 로그에 롤백과 service 복원 결과가 각각 남는지 확인한다.
- 롤백이 "foreign paths were preserved"로 끝났다면 제3자가 selector를 바꾼 것이다. 해당 leaf를 삭제하지 말고 현재 내용과 소유자를 먼저 기록한다.
- release directory는 실패해도 지우지 않는다. 불변 release이므로 다음 실행이 정확히 재검증한 뒤 재사용한다.

### completion receipt가 없는 release

- release는 dependency sync보다 먼저 고정 경로에 게시된다. venv가 절대 경로를 기록하므로 나중에 옮길 수 없기 때문이다. 따라서 sync 실패나 중단은 completion receipt가 없는 release를 남길 수 있다.
- 다음 실행은 그 release가 (1) 정확한 carried tip이고 (2) 구성 이후 venv와 receipt 후보 외에 아무것도 바뀌지 않았으며 (3) selector가 가리키지 않는다는 것을 모두 증명한 경우에만 private 이름으로 durable하게 retire한 뒤 재구성한다. 하나라도 증명되지 않으면 지우지 않고 실패한다.
- retire 도중 중단되면 `<HERMES_AGENT_REPO>.releases/.retired-<random>`가 남는다. 이것은 선택될 수 없는 비활성 디렉터리이며, 다음 실행은 정상적으로 재구성한다. 내용을 확인한 뒤 수동으로 지워도 된다.

### foreign launcher

- `~/.local/bin/hermes`가 관리 launcher가 아니면 업데이터는 release 준비 전에 거부한다.
- 해당 파일을 임의로 덮어쓰지 말고 내용을 기록한 뒤 `./scripts/setup.sh`로 재설치할지 사용자와 확인한다. setup은 사용자의 원본 launcher를 보관본으로 유지하고 그 신원을 launcher에 묶는다.

### pending marker가 남은 경우

```bash
sed -n '1,2p' "${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-update.pending"
./scripts/update-hermes-if-needed.sh
```

- pending 첫 줄은 target carried head, 둘째 줄은 활성화 전 Dashboard 실행 상태다.
- 같은 업데이터를 다시 실행한다. selector가 이미 target이면 `RESUMING`이 서비스 restart만 완료하고, 아니면 일반 활성화 경로가 처음부터 다시 수행한다.
- 성공을 확인하기 전에 pending marker를 수동 삭제하지 않는다. marker가 남아 있다는 것은 서비스가 아직 이전 release를 실행 중일 수 있다는 뜻이다.

### lock이 남은 경우

```bash
readlink "${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-update.lock"
```

- lock은 `PID:random-token`을 target으로 하는 atomic symlink다. PID가 살아 있고 해당 PID의 process command가 실제 updater인지 확인되면 다른 update가 진행 중이므로 기다린다. PID 재사용 가능성이 있으므로 숫자만 보고 소유권을 단정하지 않는다. live lock을 삭제하지 않는다.
- PID가 죽었더라도 successor lock 삭제 race를 피하기 위해 updater는 stale lock을 자동 회수하지 않는다. PID와 process command를 별도로 확인한 뒤에만 symlink를 수동 격리하고 다시 실행한다.
- symlink가 아니거나 token을 읽을 수 없는 legacy/foreign lock은 소유권을 증명할 수 없어 자동 회수하지 않는다. 실행 중인 updater process가 없음을 별도로 확인한 뒤에만 해당 leaf를 수동 격리한다.
- live lock symlink를 먼저 수동 삭제해 동시 실행 보호를 우회하지 않는다.

### release 구성 실패

- release는 공식 upstream을 그대로 fetch하고 저장소가 검증한 bundle의 carried chain만 적용해 임시 directory에서 만든 뒤 원자적으로 공개한다. 실패하면 임시 directory만 사라지고 기존 release와 selector는 그대로다.
- manifest 또는 bundle을 변경하려면 회귀 테스트와 fresh import 검증을 함께 갱신한다.
- release 시작 후 official `main`이 pin보다 앞서도 현재 frozen candidate를 중단하지 않는다. source·pin·manifest·bundle 또는 candidate tree 자체가 바뀐 경우에만 관련 evidence를 stale 처리하고 fresh 검증한다. 새 `main`은 현재 release 완료 후 다음 maintenance cycle의 새 frozen snapshot으로 처리한다.

### rollback 원칙

- Hermes checkout은 읽기 전용 입력이다. 어떤 경로에서도 checkout을 reset하거나 update하지 않는다.
- 이전 release로 되돌려야 하면 selector가 가리키는 값을 바꾸는 것으로 충분하며, 되돌린 뒤에도 그 release가 완전한 completion receipt를 가지고 있는지 먼저 검증한다.
- 활성화 전 selector 값, target release, Dashboard 상태와 실패 로그를 보존한다.
- 적용 여부를 속이기 위해 selector나 pending marker를 수동 편집하지 않는다.

## 10. 업데이트 증거 템플릿

매 업데이트 결과를 이 형식으로 작업 기록이나 PR에 남긴다. credential과 원문 세션 데이터는 제외한다.

```text
Hermes update verification
- Date/time:
- Operator/model:
- OpenAI Codex auth status: logged in / unavailable
- Platform: macOS version / architecture
- Unified Kanban commit:
- Before: selected release / reviewed upstream / carried head:
- Update check: UP_TO_DATE | UPDATE_AVAILABLE
- Target release path:
- Update path: SKIPPED | RESUMING | ACTIVATED
- Hermes checkout unchanged after activation: PASS/FAIL
- Previous release directories retained: PASS/FAIL
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
