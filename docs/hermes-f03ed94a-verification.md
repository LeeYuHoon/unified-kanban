# Hermes f03ed94a 격리 호환성 검증 결과

검증 당시 상태: 요청된 격리 검증 완료. 당시에는 main 변경, 프로젝트 commit·push 및 GitHub 반영을 하지 않았다. 아래 미커밋·미푸시·미반영 문구는 publication 승인 전 검증 시점의 기록이다. 운영 적용은 별도 승인 범위이며 이 publication에도 포함하지 않는다.

이 문서의 기존 포팅 결과는 1차 후보 `b349b09e259c593a9f8fd493f20ce05fb75a68f4`의 증거로 보존한다. 사용자 승인 후 관찰 카드 UI를 수정한 현재 release는 `f9ab37d661d16a09014a0859f49064e8ca1ed7b8`이며 마지막 후속 절과 [허용 행위 결정표](observation-cards.md)를 따른다.

중단 전 작업과 로그를 보존하여 재개했다. exit -9, KeyboardInterrupt 및 Interrupted API는 테스트 실패나 완료로 계산하지 않았다. 반복 강제 종료의 원인은 확인되지 않았다. 최종 결과는 아래 실제 완료 로그와 정확한 후보 신원을 기준으로 판정했다.

## 검증 대상

- 프로젝트 전용 작업 브랜치: `compat/hermes-f03ed94a`.
- 프로젝트 기준 HEAD: `468870bd63c95a0726b28093b108320668df4bb5`.
- 이전 공식 기반: `2237be355906fbe6065ce1815711eee52b2d646e`.
- 고정 공식 기반: `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140`.
- 1차 포팅 carried HEAD: `b349b09e259c593a9f8fd493f20ce05fb75a68f4`.
- 1차 포팅 Hermes tree: `b4182e50037a75ac1c7dbf955063920c34946abf`.
- Hermes 버전: `0.21.1`. 버전 문자열 변경은 없지만 공식 Git ancestry에서 추가 커밋 253개를 확인했다. 이후 official main 이동은 추적하지 않았다.
- 환경: macOS arm64, 별도 Python/Node 의존성 환경. source와 프로젝트의 테스트 HOME·임시 DB를 분리했다.

공식 HTTPS에서 고정 SHA를 가져왔으며 이전 공식 기반의 descendant임을 Git으로 재확인했다. 순수 upstream 누적 diff는 367개 변경 항목, 23,005줄 추가, 4,134줄 삭제다. merge·format·문서·설치 테스트 변경도 포함하므로 253개 모두를 독립적인 기능 개선이라고 표현하지 않는다.

## 추가 변경 분석

| 영역 | 확인한 변화와 평가 | 위험 및 검증 경계 |
| --- | --- | --- |
| 메시지 보안 | relay 목적지 출처와 thread 부모 결합 검증, native token snapshot 일치, guard 오류 시 거부, 승인 거부 뒤 일반 텍스트 fallback 억제 | 실질적인 안전 개선. 실제 외부 connector와 인증 계정 왕복은 미실행 |
| 인증 복구 | WebSocket 4401에서 토큰 갱신 1회 재시도, generation·pending marker로 연결 경합 구분 | 단순 인증 완화가 아니라 복구 경로 수정. 관련 회귀 포함 |
| runtime·서비스 | system-service 사용자 linger/user bus, 안전한 runtime-dir/socket 확인 후 spawn 환경 복사 | Kanban dispatch의 새 `systemd_user_bus_env` 호출을 보존. 실제 Linux systemd는 미검증 |
| 세션·subagent | transport membership/fanout, 느린 peer mailbox 상한, 재연결 generation 보호, list/tail/interrupt 소유권 검증 | session/transport identity 경합이 중요. 확장 회귀 포함 |
| 브라우저 프로필 | SQLite raw-copy fallback 대신 deadline 있는 online backup | WAL 누락·실패를 로그인 해제로 숨기는 문제를 줄임. 실제 개인 브라우저 DB는 접근하지 않음 |
| 설치·업데이트 | uv/pip 출력 병합으로 pipe deadlock 감소, 설치 E2E matrix·녹화·실패 분류 확장 | 기존 실행 프로세스에 이미 로드된 코드까지 소급되는 개선은 아님. 실제 설치/서비스 활성화는 이번 범위 밖 |
| 모델·프롬프트 | Gemini union/nullable 처리, 압축 lean-tail 유지, key_cmd 식별 및 가용 memory tool에 맞춘 안내 | schema·cache·retention 의미를 관련 회귀로 확인 |
| Desktop·TUI | subagent 표시/제어, gateway 그룹, bot room 순서·attention, plugin 설치 화면, monitor prompt handoff | Kanban 외 변경 UI도 React/TUI 테스트에 추가. 실제 GUI 전체 E2E를 대신하지 않음 |
| 문서·skill·인프라 | RSS/reddit optional 이동, property listing 선택형 안내, sandbox 및 CI 정리 | 제품 보안 수정과 구분. 모든 파일의 모든 줄을 같은 깊이로 감사했다고 주장하지 않음 |

전체 변경 경로와 주요 소스별 근거는 독립 소스 리뷰에 기록했다. carried와 upstream의 직접 Kanban 접점은 dispatch의 user bus 환경 추가였다. 기존 5개 carried 기능을 분리된 connect/dispatch/graph/workspace 모듈에 유지했고, 이전 단일 파일로 덮어쓰지 않았다. 새로운 creator origin, review/PR 계약, lifecycle hook도 유지했다. cherry-pick 무충돌만을 호환성 근거로 삼지 않았다.

## 실패 원인과 수정

1. 확장 최초 실행: 2,717 passed / 22 failed / 25 skipped. 이 완료된 RED 실행과 중간에 끊긴 `expanded-green.log`를 구분했다.
2. 21개 실패는 upstream 테스트 fixture의 macOS 호스트 의존/오래된 기대값이었다. 별도 frozen baseline에서도 해당 4개 파일의 실패를 재현했다.
   - autostash 9개: Git 테스트에서 실제 macOS 서비스 탐색으로 넘어감. 기존 systemd/process mock과 같은 수준으로 macOS 탐색을 격리했다.
   - HEAD 이동 1개: 서비스 탐색과 모듈 purge가 monkeypatch 참조를 교체함. 이 Git 계약 테스트에서만 탐색 및 purge를 격리했다. purge 자체 검증은 별도 import-guard 회귀의 책임이다.
   - launchd 3개: 실제 호스트 PID 유입, OS에 따라 다른 경고, 오래된 kickstart 기대값. 고정 renamed label PID 300을 사용하고 bootstrap 안내 및 kickstart 부재를 검증했다.
   - process registry 8개: Linux scope 모의 테스트가 macOS에서 non-Linux 경로로 빠짐. 해당 클래스에만 `_IS_LINUX=True` fixture를 적용했다.
3. 나머지 1개는 Anthropic SDK 부재에 따른 ImportError였다. source의 optional provider 요구에 맞춰 격리 venv에 `anthropic==0.87.0`을 설치했다. 생산 코드 결함으로 분류하지 않았다.
4. 위 수정 후 집중 5개 파일은 445 passed / 6 skipped. 최종 확장 114개 파일은 2,739 passed / 25 skipped / exit 0이다. 테스트 삭제·skip 추가·생산 코드의 보호 조건 완화는 하지 않았다. fixture 수정은 임시 Hermes bundle용 여섯 번째 local commit에 담았다.
5. 프로젝트 pin 갱신 뒤 bootstrap fixture가 이전 SHA를 써서 exact-byte digest 거부를 재현했다(`bootstrap-red.log`). 7곳의 고정 fixture/expected argv/receipt SHA만 새 frozen으로 맞췄다. 해당 검증을 포함한 프로젝트 전체 1,218개가 통과했다.
6. 독립 bundle 검증용 bare 저장소에는 처음에 `FETCH_HEAD`와 사용자 정의 carried refs만 있었다. verifier의 `git clone --bare --no-local`이 일반 브랜치를 가져오지 못해 prerequisite 누락으로 실패했다(`bundle-verify.log`). 임시 bare 저장소의 `refs/heads/main`을 exact frozen에 고정한 후 동일 verifier가 새 격리 clone에서 verify/unbundle/선형 chain 검사를 통과했다(`bundle-verify-green.log`). 후보 bundle 및 verifier 생산 코드를 수정한 것이 아니라 검증 fixture의 ref 구성을 바로잡았다.

## 최종 테스트 결과

모든 아래 실행은 완료 로그의 `EXIT_CODE=0`을 직접 읽었다. 이전 baseline, RED, 집중 재검증과 skip 사유 확인을 위한 재실행은 합계에 중복 포함하지 않는다.

| 범위 | 통과 | 건너뜀 | 완료 로그 접미사 |
| --- | ---: | ---: | --- |
| 프로젝트 전체, release-required 포함 | 1,218 | 0 | `project-final.log` |
| Hermes Kanban 69개 파일 | 534 | 8 | `kanban-final.log` |
| 확장 upstream 회귀 114개 파일 | 2,739 | 25 | `expanded-final.log` |
| React Kanban + 변경된 desktop UI 38개 파일 | 347 | 0 | `react-final.log` |
| web 전체 40개 파일 | 295 | 0 | `web-final.log` |
| 변경된 TUI 9개 파일 | 149 | 0 | `tui-regression.log` |
| 합계 | 5,282 | 33 | 위 여섯 suite 합산 |

Kanban과 확장 upstream 파일 집합이 겹치지 않음을 프로그램으로 확인했다. 이는 선택한 suite의 테스트 실행 합계이며 모든 upstream 테스트 또는 서로 다른 기능 5,282개를 검증했다는 뜻은 아니다. 프로젝트 전체는 `1218 passed in 809.35s`, exit 0이다. 완료 후 불필요한 전체 재실행을 하지 않았다.

건너뜀 사유를 해당 13개 파일의 `pytest -rs` 출력으로 다시 수집했다. 총 33개는 Linux 전용 22개, native Windows 전용 10개, APFS 대소문자 비구분 파일명 1개다. Kanban 8개는 Linux 7개/Windows 1개, 확장 25개는 Linux 15개/Windows 9개/APFS 1개다. 실패를 숨기기 위한 새 skip은 없다.

추가 완료:

- desktop production build: renderer, electron main/preload, native dependency stage, `assert-dist-built`까지 exit 0.
- web production build: TypeScript 및 Vite exit 0.
- Ink build: exit 0.
- 격리 실제 CLI: 보드 생성 → 비공개 제목 파일로 관찰 카드 생성 → 중복 댓글 2회 전송 후 1개 readback → 전체 결과 파일로 완료 → synthetic run 부재 확인 → 보관 readback.
- 같은 격리 DB의 FastAPI TestClient: 전체 결과/token readback, 보관 후 token 합계 유지, 명시적 보드 삭제 후 404 확인. 인증된 네트워크 Dashboard smoke 또는 실제 운영 adapter 설치 smoke로 확대 해석하지 않는다.
- Bash syntax와 source/project `git diff --check`: 통과.
- 결과 문서 추가 후 문서 계약 2개 파일을 별도 재검증하여 6 passed / exit 0을 확인했다(`docs-final.log`). 위 5,282 합계에는 이 중복 확인을 추가하지 않았다.

## bundle·pin·배포 계약

carried 순서:

1. `c48bab43909ad268fd5313db2159aceaba79b600`
2. `f36446a584467b5ef2ddbaa9191608771acc7817`
3. `ecd6578f2da01f1dcba515a467c2b99a196e9b48`
4. `eafeea45fd14ec62f7cb3497216a2ed0f162857f`
5. `6ac89237290d38694ca7502bda6ac9a3f01d8aec`
6. `b349b09e259c593a9f8fd493f20ce05fb75a68f4`

- bundle: 6개 ordered refs, 48,338 bytes.
- bundle SHA-256: `6793a22dc0c8fad47a3b1af1bb35bab36b409da285913435349a8416fcf9e53c`.
- 공식 HTTPS frozen만 별도 bare 저장소에 fetch한 뒤 bundle을 verify/fetch했다. 이후 일반 main ref를 고정하고 verifier의 독립 clone 검증도 성공했다. 기존 carried object 저장소 공유를 fresh import 증거로 사용하지 않았다.
- bootstrap installer는 upstream diff가 없고 실제 SHA-256 `5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968`을 유지한다. URL·upstream·manifest/pin digest만 동기화했다.
- 기존 전체 테스트가 실제 만든 sdist를 회수했다. README, pyproject, Hermes version 파일의 아카이브 바이트가 후보와 같고 tests payload가 제외됨을 재확인했다.
- sdist: 46,068 bytes, SHA-256 `e9cd6d6b3481c246d62d9005ed05779e1dab9fb01bf28aedfb11b1a93ac1d82c`. 보존본 `/tmp/unified-kanban-f03ed94a-verified-sdist.tar.gz`.
- sdist는 기존 library 배포 계약을 검증한 것이다. setup용 Git 저장소 전체나 Hermes bundle을 sdist에 포함했다고 주장하지 않는다.

## 독립 리뷰와 남는 한계

별도 read-only 리뷰 두 건을 회수하고 검토 대상 신원을 부모가 다시 비교했다.

- 소스 리뷰: exact HEAD/tree 일치. 신규 차단 결함 없음, H0/M0, 기존 비차단 L1 한 건.
- 프로젝트 리뷰: 후보 tracked 8개 파일의 전체 binary diff SHA-256 `c4cd25d1c08d7d61c2798f11ecaf6d3693ec2b282be489b1d874e655777f6a1a` 일치. pin·hash·chain·version·fixture 동기화 승인, guard 약화 없음. 이 결과 문서는 리뷰 이후 추가했으며 코드 후보는 변경하지 않았다.
- 기존 L1은 후속 승인 작업에서 해결했다. legacy UI의 관찰 드래그/worker 상태 버튼과 배정·모델·혼합 선택·진단 경로를 제한했고 실제 Chrome DOM 회귀·화면을 확인했다. 서버 보호는 유지했다. 아래 후속 절에 증거를 구분했다.
- trusted token은 author/header/schema/event 일치 검증이지 암호학적 출처 인증이 아니다. 결과 접기는 표시 기능이며 API 비밀 차단이 아니다.
- 관찰 TTL 만료의 done은 외부 실행 성공을 증명하지 않는다. 대형 보드 댓글 집계의 부하 한계는 실측하지 않았다.
- macOS 모의 Linux 테스트가 실제 Linux systemd 동작을 증명하지 않는다. Windows/WSL2/Linux 실행, GUI installer E2E, 인증 provider 호출, 운영 activation·launchd binding·서비스 재시작·setup-twice는 실행하지 않았다.
- 경고: npm의 release-age 설정 인식 경고, Vite native config/중복 dynamic import/plugin 시간 경고, 테스트 DOM의 Window.open 미구현 안내가 남지만 해당 실행은 성공했다. source 격리 Python의 SQLite 3.50.4는 알려진 WAL-reset 취약 버전으로 판정되어 Hermes가 DELETE journal로 안전 전환했다. 이 경고를 없애기 위해 운영 Python을 변경하지 않았다. 초기 모의 process registry 실행에는 thread/MagicMock 직렬화 경고가 있었으며 최종 skip 증거 재검증에서는 재발하지 않았다.

## 변경 파일과 재현 증거

프로젝트 tracked 변경은 다음 8개이며 이 결과 문서를 새로 추가했다.

- `README.md`
- `patches/hermes-agent-supported-upstream`
- `patches/hermes-agent-carried-commits`
- `patches/hermes-agent-carried.bundle`
- `patches/hermes-agent-carried-bundle-metadata.json`
- `patches/hermes-agent-bootstrap-manifest`
- `scripts/bootstrap-hermes-macos.sh`
- `tests/test_macos_hermes_bootstrap.py`

임시 Hermes source 누적 carried diff는 34개 파일, 2,956줄 추가/93줄 삭제다. 기존 5개 기능 이식과 fixture 수정 1개이며 자세한 파일 목록은 source-review에 있다. 기존 사용자 worktree·이전 clone·운영 checkout은 보존했다. 재개 시 남은 테스트/build 프로세스 검색은 일치 없음이었고 unrelated 프로세스를 종료하지 않았다.

로그 공통 접두사는 `/tmp/unified-kanban-f03ed94a-`다.

- `project-final.log`: 전용 project cwd, `.venv/bin/python -m pytest -o addopts='' -q`, 격리 `HOME=.venv/port-home`, `TMPDIR=.venv/port-tmp`, `env -u HERMES_HOME`, `UNIFIED_KANBAN_TEST_HERMES_SOURCE=/tmp/unified-kanban-hermes-f03ed94a-source`, `UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1`.
- `expanded-final.log`: 첫 줄에 exact HEAD와 114개 파일을 포함한 전체 명령. `resume-check.py`는 clean-env canonical `scripts/run_tests.sh`, `HERMES_TEST_FILE_RETRIES=0`, `-j 4 --tb=short -rs`를 실행했다.
- `kanban-final.log`: source의 CLI/plugin `test_kanban*.py`, 같은 격리 canonical runner와 retries=0.
- `ui-final.py`: 변경된 React 테스트와 Kanban, web 테스트 및 desktop/web build의 전체 명령. 각 로그 첫 줄에 명령과 HEAD.
- `extra-ui.py`: Ink build와 변경된 TUI 회귀 명령.
- `smoke.py`, `smoke.log`: 실제 CLI·FastAPI smoke assertion과 readback.
- `source-review.md`, `project-review.md`: 독립 리뷰 전문.
- `evidence.json`: 실제 읽은 완료 로그의 SHA-256, 집계, skip 분류, sdist 신원.
- `progress.md`: 중단 전후 체크포인트. `result.md`: 이 최종 보고서의 별도 보존본.

주요 완료 로그 SHA-256:

| 로그 | SHA-256 |
| --- | --- |
| `project-final.log` | `4648e60d8117608e0d4c40fcb2436639fd72d9c390f371a85ba17bbf22c0d527` |
| `kanban-final.log` | `16b3c50b931a7a41b4298ac144a1b759d8d2c09bfa6d974c0b107b11c455382f` |
| `expanded-final.log` | `307fde71e209c3b6b78f164fe38c91c16fa8c7b77e3aa1fd6a7374a3e9aabacf` |
| `react-final.log` | `0f4b17ee6a83c1b8e8cd80cd4f63d7e36e08bc273a0627fd7ced4fd4f3463709` |
| `web-final.log` | `8058d0d26b660be200236f0da51535166e422538a70b2c5cfb2415eb79dcb1fc` |
| `tui-regression.log` | `a07bb3f8daf46bcec135ae84ab986d892d8a6b86e0e289fe7272f08c2d34feb0` |

당시 완료 판정은 위 격리 후보의 호환성 검증에 한정했다. main merge, 운영 적용, GitHub publication은 당시 검증 요청의 금지 범위여서 별도 승인 작업으로 남겼다.

## 관찰 카드 UI 후속 승인 작업

### 최종 후보 신원

- frozen은 `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140` 그대로다.
- 기존 6개 carried commit 뒤에 UI 수정 `94869b1327e7817cbf5b3ea3c2f07e3cb695313c`, 브라우저 회귀 보강 `f9ab37d661d16a09014a0859f49064e8ca1ed7b8`을 순서대로 추가했다. reset·clean·기존 commit 재작성은 하지 않았다.
- 최종 source tree: `971e7b4799b11b3d2f52e2974f55d8ae5ee7e5f6`.
- 최종 bundle: ordered refs 8개, 55,204 bytes, SHA-256 `4d7a6e73f4d1a4887b6a6b81e1d938d28c57f1fcce88d6cb330adb7483f9bb4a`.
- README release SHA, carried manifest, bundle refs와 metadata를 동기화했다. bootstrap 후보 변경과 이전 6개 기능·fixture commit은 보존했다.
- official HTTPS에서 frozen만 받은 fresh shallow bare에 verifier의 독립 clone 검증을 수행하고 최종 bundle을 반입했다. 반입한 carried-08의 HEAD/tree가 위 신원과 일치한다. 기존 source의 object store를 공유하지 않았다. 전체 upstream history를 새로 검증한 것이 아니라 exact frozen prerequisite와 추가 선형 chain의 반입 검증이다.

### Before / After 및 검증 범위

- 시연 URL: `http://127.0.0.1:19129/kanban`. HOME/HERMES_HOME은 `/tmp/kanban-legacy-demo-home` 아래의 기존 격리 환경이다. 관찰 `t_d748bd83`, 일반 `t_8c7aea79`를 사용했다.
- Before: `/tmp/kanban-legacy-demo.png` — 관찰 상세에 →triage/→ready/Block/Unblock, 편집 가능한 담당자·모델이 노출됐고 draggable=true였다.
- After: `/tmp/kanban-legacy-demo-fixed.png` — 한국어 관찰 안내, Complete/Archive 유지, 담당자·모델 읽기 표시, 댓글·설명 유지. 일반 비교 화면은 `/tmp/kanban-legacy-demo-ordinary.png`다.
- 실제 제공되는 JS와 source JS의 SHA-256이 `6098ff871610362f8343c8687b40e7d32b2048e9935412db0e6445d448839fd7`로 일치했다. backend 코드 변경이 없어 시연 서버 재시작 없이 static asset 재조회로 반영했다.
- 실제 UI 댓글 전송·조회 및 설명 저장을 검증했다. 관찰 상태와 담당자는 유지됐다. 일반 카드의 ready→triage→ready는 두 PATCH HTTP 200과 각 readback으로 확인했다.
- 마우스 draggable, 위조 혼합 drop, 담당자 선택 후 혼합 Apply 비활성/일반 Apply 활성, 모델·의존성 선택·진단 복구 동작을 실제 Chrome DOM에서 검증했다. 별도 touch PointerEvent 검증에서 관찰 proxy 부재·일반 proxy 생성을 확인했다. 물리 터치 기기 검증은 아니다.
- 진단 fixture는 화면 응답에만 주입했고 혼합 드롭의 쓰기는 가로챘다. 반면 댓글·설명·일반 상태 이동은 실제 시연 서버에 반영했다. 서버 거부 재확인은 관찰 triage/ready/blocked·담당자·모델 PATCH를 직접 요청한 별도 API 검증이다.
- current React의 기존 drag/drop/context 제한은 유지하고 누락된 bulk 배정·상세 배정/모델/reasoning·Note & requeue·진단 reclaim을 수정했다. 관찰 기록 완료 진입점, 보관·삭제·댓글·설명·의존성 조회는 유지했다.

### 테스트와 리뷰

최종 실행 로그 접두사는 `/tmp/unified-kanban-observation-`다. 1차 포팅 테스트 합계와 중복 합산하지 않는다.

- `project.log`: 최종 source SHA와 `UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1`로 프로젝트 전체 release-required pytest 실행. `1218 passed in 858.12s (0:14:18)`, skip 0, exit 0. 격리 HOME/TMPDIR와 실제 명령은 `checks.py`에 있다.
- `backend.log`: final source의 Kanban 74개 파일, 580 passed / 8 skipped / 0 failed. 건너뜀은 Linux 전용 7개와 Windows 전용 1개다.
- `react.log`: React Kanban 7개 파일, 58 passed.
- `web.log`: 40개 파일, 295 passed. `build.log`, `desktop-build.log`: 최종 source의 web/desktop production build. 모든 실행 exit 0.
- `docs.log`: 후속 문서의 문체·유지관리 계약 6 passed. 전체 suite에 중복 합산하지 않는다.
- `sdist.log`: offline `uv build --sdist` 성공. `/tmp/unified-kanban-observation-dist/unified_kanban-0.1.0.tar.gz`의 README·pyproject·Hermes version 파일을 현재 후보와 byte 비교했다. 46,396 bytes, SHA-256 `e8989f07920794d2e3e1567bfccb2e9d8a5e8e999c3c12ef141ff887d74276df`. 이 library sdist에 setup용 Hermes bundle까지 담았다는 의미는 아니다.
- `browser-final.log`: 실제 Chrome DOM 회귀, pageErrors=[]와 최종 screenshot 저장 확인.
- `live.log`: 실제 시연 UI 쓰기와 readback.
- `touch-server.log`: touch PointerEvent 양성/음성 대조와 서버 거부·상태 보존.
- `mutation.log`: bulk assignment와 observation-child 필터를 각각 응답에서만 제거했을 때 의도한 DOM 실패를 탐지. 빈 입력이나 존재하지 않는 native option 때문에 통과하는 검사를 방지했다.
- `bundle-verify.log`: CARRIED_BUNDLE_PASS refs=8, metadata와 동일한 크기·digest·prerequisite.
- `review.md`: 독립 read-only 리뷰에서 UI commit의 차단 결함 없음. 리뷰 diff SHA-256 `dea04a86a483d595f47ed3f305814821b867b1d1c90ebc562f8fd3deca7e8bfd`를 부모가 재계산해 일치 확인했다. final source와 UI commit의 차이는 `observation.browser.js` 하나뿐이며, 리뷰가 지적한 Apply 테스트의 빈 선택값 문제를 고쳤다. 최종 테스트 보강분은 부모의 Chrome 및 mutation 검증 근거이며 독립 리뷰를 받은 것처럼 확대하지 않는다.

### 실패·중단 기록

- 초기 backend 1개 실패는 기존 JS substring 검사가 Select 속성 추가 위치에 의존했기 때문이다. disabled를 value 앞에 배치해 의미 변화 없이 기존 handler 계약 검사를 보존했고 재실행했다. `backend-red.log` 보존.
- DOM RED에서는 draggable, 담당자, 모델, 혼합 drop, 진단과 댓글 안내의 노출을 각각 재현했다. React RED는 bulk Assign 및 진단 reclaim 노출을 재현했다.
- browser harness IPC 시간 초과와 about:blank 대상 전환은 별도 Playwright Chrome context로 우회했다. 초기 screenshot은 drawer 애니메이션 중간이라 최종 증거로 쓰지 않고 재촬영했다.
- 테스트 harness의 native select 가정·잘못된 drop 속성·mutation asset URL은 실제 DOM/로드 URL 확인 후 수정했다. mutation 초기 harness 실패는 `mutation-harness-red.log`에 남겼다.
- 독립 반입 후속 명령에서 잘못된 상대 경로 `patches/invalid`를 한 번 사용해 fetch가 실패했다. 후보 파일은 변경하지 않았으며 실제 bundle 절대 경로로 다시 fetch하고 HEAD/tree를 확인했다.
- 7-commit 후보의 프로젝트 전체 실행은 회귀 보강 후 최종 판정에 쓰지 않고 본인이 시작한 테스트 프로세스만 중단했다(`project-superseded.log`). 8-commit 최종 후보로 전체 release-required 실행을 다시 시작했다. 중단 실행을 통과로 집계하지 않는다.
- 최종 빌드에도 기존 npm release-age 설정 인식, Vite native config와 중복 dynamic import 경고가 남는다. 해당 로그를 그대로 보존했으며 모두 exit 0이다. 실패를 숨기는 skip이나 서버 보호 약화는 추가하지 않았다.

완료 증거의 파일별 digest와 exact source/반입 신원은 `/tmp/unified-kanban-observation-evidence.json`에 기록했다. 프로젝트 전체 최종 로그 SHA-256은 `3112d37cd3474cb7d2b4fdbff8333e4ffe3e50e0629bb3c264caa0bc5f1adf8d`다.

위 검증을 마친 시점에는 프로젝트 자체 commit/push, main merge 및 GitHub publication을 수행하지 않았다. 이 문서는 그 시점의 증거를 보존한다. 운영 activation·서비스 재시작·운영 DB 변경은 후속 publication 승인에도 포함되지 않으며 미적용 상태를 유지한다.
