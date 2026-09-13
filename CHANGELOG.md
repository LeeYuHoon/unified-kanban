# Changelog

All notable user-visible changes to this project will be documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will use [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Hermes Agent `0.21.1`, 공식 기반 `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140` 위 대화 수집 release 후보를 준비했다. 최신 upstream 갱신은 이번 배포 이후 별도 검토한다.
- Claude Code·Codex의 서명된 대화 구간과 Hermes 네이티브 관찰 기록을 카드에서 조회한다. 과거 대화는 소급 수집하지 않는다.
- Claude 시작 시 transcript가 없으면 검증된 이후 구간만 부분 기록으로 표시한다. 최종 응답이 수집되지 않았을 때 카드 결과로 대화 응답을 만들어 채우지 않는다.
- 외부 대화 binding이 아직 없으면 비공개 캐시 금지 헤더를 포함한 HTTP 503으로 응답하고, binding이 생긴 뒤 다시 조회할 수 있다.

- 분리된 Hermes Kanban 모듈과 현재 React 화면에 관찰 카드·보드별 토큰 표시를 이식했다.
- Hermes Agent 0.21.0 호환성과 Claude Fable 5.1 모델 목록 지원.
- Unified observation cards for Claude Code, Codex CLI, and Hermes Agent user turns.
- Per-card Skill, subagent, MCP, model, and truthful token-usage metadata.
- Repository-contained setup, uninstall, smoke, and Hermes update workflows.
- Exact supported-upstream pinning and runtime compatibility gates.
- Portable Hermes carried-commit manifest and thin bundle.
- Model-family token summaries and full result/summary Dashboard presentation.
- Filtering of automatic delegation, background, compaction, and worker-only turns.
- Open-source governance, security, contribution, CI, and maintenance documentation.

### Security

- 손상된 토큰 댓글의 JSON 중첩·정수 길이 오류가 보드 조회를 중단하지 않도록 격리했다.
- Fail-closed Hermes compatibility checks use a repository-owned pin and no-follow descriptor identity validation.
- Runtime gates bind the frozen pin, selected immutable release, final carried commit, completion
  receipt, and `hermes --version` upstream; moving checkout refs are not installation authority.
- Distribution metadata publishes no unguarded mutation console script; repository setup is the
  supported deployment path and direct module execution remains fail-closed.
- Carried bundle commit metadata uses a project noreply identity and preserves the Hermes Agent
  copyright and MIT terms in `THIRD_PARTY_NOTICES.md`.
- An unavailable or mismatched exact frozen upstream object is rejected before updater mutation;
  a later move of official `main` is recorded for the next maintenance cycle.

No release has been tagged yet. The first public release should be `0.1.0` after publication gates in `docs/maintenance.md` pass.
