# Dashboard OAuth 설치 안내

이 기능은 검증 중인 후보이며 실제 Dashboard 활성화·브라우저 로그인 확인을 대신하지 않습니다.
처음 쓰는 PC는 Hermes 설치 후 사용할 HOME/profile에서 `hermes auth add nous`와
`hermes dashboard register`를 별도로 실행해야 합니다.

## 설치 시 확인하는 것

`./scripts/setup.sh --dashboard-oauth --no-restart --skip-smoke`는 저장된 client ID 형식과
공식 Portal·loopback 설정 충돌만 오프라인으로 검사합니다. 등록이 없으면 설치 변경 전에 중단합니다.
공식 `hermes config set dashboard.require_auth true`를 격리된 임시 작업 공간(staging)에서
실행한 뒤, 기존 설치의 트랜잭션 절차에 따라 설정을 반영합니다.
로그인·토큰 읽기/갱신·원격 등록은 하지 않으며, 원격 등록의 유효성이나 현재 로그인 여부도 보증하지 않습니다.
`--dry-run`은 검사 예정만 출력하고 인증·설정을 쓰지 않습니다. 플래그가 없으면 기존 설치 동작을 유지합니다.

## 이전 등록 표시가 남아 있다면

`.dashboard-oauth-registration-pending`이 있으면 지우지 않고 중단합니다.
Portal `/local-dashboards`와 로컬 설정을 직접 대조해 해결한 뒤 재시도하세요.
등록 정보는 설치 되돌리기(rollback)와 제거 후에도 유지됩니다.

## 설치 후 별도로 확인할 것

Gateway 재시작만으로 Dashboard 인증이 확인되지는 않습니다.
standalone Dashboard를 loopback에서 다시 시작하고 실제 브라우저 로그인을 별도로 검증해야 합니다.
인증 연결은 대화 수집 활성화가 아닙니다. 수집은 계속 꺼져 있으며 별도 권한 검토가 필요합니다.
