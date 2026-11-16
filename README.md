# claude_memguard

Claude 앱과 Claude Code가 띄운 **자식 프로세스만** 감시해, 메모리를 너무 많이 쓰면 멈추는 macOS용 가드입니다. Python 표준 라이브러리만 씁니다.

A small macOS guard that watches only the processes spawned by the Claude app / Claude Code and stops them before they exhaust RAM. Standard library only.

## 왜 만들었나

2026-10-15, 서브에이전트들이 동시에 띄운 Whisper large-v3 프로세스 4개(각 약 10 GB, 합계 약 37 GB)가 24 GB 맥의 메모리를 모두 써서 커널 워치독이 응답을 받지 못했고, 맥이 재부팅됐습니다. 에이전트는 "전사 작업 4개를 병렬로" 같은 결정을 내리지만 그 비용을 모릅니다. 사람이 매번 지켜볼 수는 없어서, 에이전트가 만든 프로세스에만 한도를 거는 별도의 감시자를 만들었습니다.

## 동작 (3초마다)

1. Claude 앱과 Claude Code CLI 프로세스를 찾습니다. 이 둘 자체는 절대 건드리지 않습니다.
2. 그 아래의 모든 자손 프로세스(python, ffmpeg, node …)를 모읍니다.
3. 아래 규칙으로 판정하고, 걸리면 SIGTERM을 보낸 뒤 3초 안에 끝나지 않으면 SIGKILL을 보냅니다. 모든 조치는 로그와 macOS 알림으로 남깁니다.

| 규칙 | 조건 (기본값) | 조치 |
|---|---|---|
| 프로세스 한도 | 자손 하나의 상주 메모리 > 6144 MB | 그 프로세스 종료 |
| 합계 한도 | 자손 전체 합 > 12288 MB | 가장 큰 자손 종료 |
| 시스템 압박 | `kern.memorystatus_level` < 12% 이고 자손 합 > 3072 MB | 가장 큰 자손 종료 |

의도한 큰 작업은 `~/.claude_memguard_allow`에 MB 단위 한도를 적으면 그 파일을 마지막으로 쓴 뒤 3시간 동안 한도가 올라갑니다.

```bash
echo 12000 > ~/.claude_memguard_allow
```

## 사용법

```bash
python3 claude_memguard.py --once --dry    # 한 번 스캔하고 보고만 함
python3 claude_memguard.py                 # 계속 실행 (LaunchAgent가 이렇게 실행)
```

로그: `~/Library/Logs/claude-memguard.log`

상시 실행하려면 `launchd/claude-memguard.plist.example`의 경로를 고친 뒤 `~/Library/LaunchAgents/`에 복사하고 `launchctl load`로 등록합니다.

## 실제 기록

2026-10-15부터 11-13까지 한도에 걸려 실제로 자동 종료한 것이 26번입니다 (테스트 1번 제외).

```
2026-10-15 22:30:11 kill pid 2122 Python (6.5 GB): over per-process limit 6144 MB
2026-10-28 16:33:19 kill pid 27366 Python (6.3 GB): over per-process limit 6144 MB
2026-11-10 09:27:58 kill pid 71354 qemu-system-aarch64 (6.2 GB): over per-process limit 6144 MB
```

## 한계

- 노트북 한 대를 위한 임시방편입니다. 일이 터진 뒤 밖에서 감시하는 방식이라, 프로세스가 아주 빠르게 메모리를 잡으면 3초 간격 사이에 놓칠 수 있습니다.
- macOS 전용입니다 (`ps`, `sysctl kern.memorystatus_level`, `osascript`).
- Claude 프로세스는 실행 파일 경로(`/Claude.app/`, `/claude-code/`)로 찾습니다. 설치 경로가 다르면 `CLAUDE_MARKERS`를 고쳐야 합니다.
