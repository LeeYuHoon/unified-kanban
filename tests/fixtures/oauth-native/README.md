# 고정된 OAuth 공식 원본 테스트 자료

다음 두 파일은 공식 소스의 바이트를 그대로 보존한 **원시 테스트 자료**입니다.
프로젝트가 관리하는 Python 소스나 대체 런타임이 아니므로 `.py.txt`로 보관하며 번역하지 않습니다.

| 저장 경로 | 공식 Git 경로 | 용도 |
| --- | --- | --- |
| `hermes_constants.py.txt` | `hermes_constants.py` | 표준 라이브러리만 사용하는 HOME·root·profile 해석 |
| `hermes_cli/config_defaults.py.txt` | `hermes_cli/config_defaults.py` | 준비된 런타임의 기능 선언 |

`provenance.json`은 고정된 commit과 필요한 두 tree 객체를 담습니다.
`tests/oauth_native_source.py`는 기존 bundle 메타데이터·헤더 검증기를 실행하고
bundle SHA-256과 manifest의 마지막 commit을 대조합니다. 이어 commit/tree 경로의
Git 객체 ID로 `.py.txt`의 바이트를 검증합니다. 증명 속 경로는 공식 `.py` 경로 그대로입니다.
모든 검증이 끝난 뒤에만 임시 폴더에 원래 `.py` 이름으로 복원하며 프로세스 종료 시 정리합니다.
고정 버전·bundle·증명·원본이 달라지면 중단합니다. 개발자의 다른 소스 폴더를 빌리지 않습니다.

얇은 carried bundle은 기반 객체 없이 해제하면 미해결 delta 401개가 남습니다.
전체 기반 객체를 배포하거나 테스트 중 몰래 내려받는 대신 이 최소 자료로 오프라인 검증합니다.
기존 테스트 대역과 인증 모듈 가져오기 금지 검사는 유지합니다. 저장소의 `.py` 주석 검사에도
fixture 폴더 전체를 제외하는 예외를 추가하지 않습니다.

## 승인된 고정 버전 변경 후 갱신

공식 기반 객체가 있는 임시 bare Git 저장소에서 carried bundle을 대상으로
`git bundle verify`와 `git bundle unbundle`을 실행합니다.
`git show <manifest-tip>:<path>`로 위 표의 공식 경로를 읽어 대응하는 `.py.txt`에
바이트 그대로 저장합니다. 변경된 작업 폴더에서 복사하거나 번역하지 마세요.
`git cat-file commit <tip>`, `git cat-file tree <tip>^{tree}`,
`git cat-file tree <tip>:hermes_cli`의 원시 출력을 객체 ID별 base64로 기록하고
마지막 commit과 bundle SHA-256을 `provenance.json`에 넣습니다.
원본·증명을 검토한 뒤 전체 OAuth 테스트와 저장소 위치 변경 테스트를 실행하세요.
테스트 실행 중에는 네트워크가 필요하지 않습니다.
