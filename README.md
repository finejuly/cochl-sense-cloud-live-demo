# Cochl.Sense Cloud Live Demo

마이크로 들어오는 주변 소리를 실시간으로 분석하고, AI가 감지한 소리를 화면에 보여 주는 로컬 데모 앱입니다. 소리를 짧은 구간으로 나누어 Cochl.Sense Cloud로 보내고, 의미 있는 비음성 구간만 골라 이 컴퓨터에 저장합니다.

> 전체 녹음 원본은 만들지 않습니다. 다만 분석에 사용되는 짧은 오디오는 Cochl.Sense Cloud로 전송되며, 선택된 구간은 로컬 파일로 남습니다.

## 무엇을 할 수 있나요?

- 마이크 주변에서 감지된 소리와 AI 신뢰도를 실시간으로 확인합니다.
- 소리의 시간·주파수 분포를 스펙트로그램으로 봅니다.
- 무음과 음성 관련 구간을 제외하고 중요한 소리만 짧은 세그먼트로 저장합니다.
- 저장된 세그먼트를 재생하거나 개별/세션 단위로 삭제합니다.
- 필요하면 한 세션을 Google Cloud Storage(GCS)에 업로드합니다.

## 동작 방식

1. 브라우저가 마이크 소리를 짧은 구간으로 나눕니다.
2. 이 컴퓨터에서 실행 중인 백엔드가 해당 구간을 Cochl.Sense Cloud로 보냅니다.
3. 감지된 소리 이름, 신뢰도, 처리 지연을 화면에 표시합니다.
4. 기본 수집 규칙에 따라 무음·음성·반복 구간을 제외하고 선택된 구간만 `recordings/collected/`에 저장합니다.
5. 사용자가 선택한 경우에만 저장된 세션을 GCS로 업로드합니다.

### 화면 용어

| 용어 | 뜻 |
| --- | --- |
| 감지 소리(라벨) | AI가 들렸다고 판단한 소리 종류입니다. 결과가 항상 정확한 것은 아닙니다. |
| 신뢰도 | 해당 소리라고 판단한 정도입니다. 높을수록 모델의 확신이 크지만, 정확한 확률을 뜻하지는 않습니다. |
| 스펙트로그램 | 시간에 따라 어떤 높낮이의 소리가 강했는지 색으로 보여 주는 그림입니다. |
| 청크 | 실시간 분석을 위해 만든 짧은 오디오 조각입니다. |
| 세그먼트 | 수집 규칙을 통과해 실제 파일로 저장된 구간입니다. |

## 데이터와 개인정보

- 마이크 오디오는 분석을 위해 Cochl.Sense Cloud로 전송됩니다.
- 음성 제외는 클라우드 분석 결과를 받은 뒤 **로컬 저장 여부를 결정하는 규칙**입니다. 음성이 클라우드로 전송되는 것 자체를 막는 기능은 아닙니다.
- 전체 세션을 하나의 녹음 파일로 저장하지 않습니다.
- 기본 설정에서는 무음, 낮은 신뢰도의 감지, 말하기·속삭임·노래 같은 음성 관련 구간을 로컬 수집에서 제외합니다.
- 자동 분류는 개인정보 보호를 보장하지 않습니다. 외부로 전달하거나 GCS에 올리기 전에 저장된 세그먼트를 직접 확인하세요.
- 서버는 기본적으로 이 컴퓨터의 로컬 주소에서만 접근할 수 있습니다.

## 빠른 시작

### 준비물

- Cochl.Sense Cloud 프로젝트 키
- Python 3.10 이상
- Node.js 20.19 이상 (`.nvmrc`의 버전 권장)
- `ffmpeg` 권장: 전송 최적화와 수집 파일의 MP3 변환에 사용
- macOS 앱 빌드 시 macOS 13 이상과 Xcode Command Line Tools

핵심 실시간 분석은 `ffmpeg` 없이도 동작하지만 전송이 느려질 수 있고, 수집 파일이 WAV로 남을 수 있습니다.

### 1. 설치

프로젝트 루트에서 운영체제에 맞는 명령을 실행합니다.

#### macOS/Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -c backend/constraints.txt -e "backend[dev]"

cp .env.example .env

cd frontend
nvm install
nvm use
npm ci
cd ..
```

#### Windows (PowerShell)

Windows에서는 macOS용 `.app` 대신 브라우저로 실행합니다. 아래 명령은 가상 환경을 활성화하지 않고 실행하므로 PowerShell 실행 정책을 변경할 필요가 없습니다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -c backend/constraints.txt -e "backend[dev]"

Copy-Item .env.example .env

Set-Location frontend
npm ci
Set-Location ..
```

`py` 명령을 찾을 수 없다면 첫 줄의 `py -3`을 `python`으로 바꾸세요. Node.js 버전 관리자를 사용한다면 `.nvmrc`에 적힌 버전을 설치해 사용하고, 그렇지 않다면 Node.js 20.19 이상이 설치되어 있는지 `node --version`으로 확인하세요. `ffmpeg`를 설치했다면 `ffmpeg -version`이 PowerShell에서 실행되도록 PATH에 등록하세요.

생성된 `.env`에서 `COCHL_PROJECT_KEY`에 발급받은 키를 넣으세요. `.env`와 인증 파일은 Git에 커밋하지 마세요. 설정을 바꾼 뒤에는 백엔드를 다시 시작해야 합니다.

### 2. 실행

#### macOS 앱으로 실행

```bash
scripts/build-macos-app.sh
open CochlSenseCloudLiveDemo.app
```

이 앱은 현재 저장소의 `.venv`, `.env`, 백엔드 코드와 빌드된 프런트엔드를 사용하는 개발용 래퍼입니다. 다른 컴퓨터에 그대로 배포하는 독립 실행형 앱은 아닙니다.

#### macOS/Linux에서 브라우저로 실행

터미널 1 — 백엔드:

```bash
.venv/bin/uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

터미널 2 — 프런트엔드:

```bash
cd frontend
nvm use
npm run dev
```

브라우저에서 `http://127.0.0.1:5173`을 열고 마이크 권한을 허용하세요.

#### Windows에서 브라우저로 실행

PowerShell 창 두 개를 열고 프로젝트 루트에서 각각 실행합니다.

PowerShell 1 — 백엔드:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

PowerShell 2 — 프런트엔드:

```powershell
Set-Location frontend
npm run dev
```

Chrome 또는 Edge에서 `http://127.0.0.1:5173`을 열고 마이크 권한을 허용하세요. Windows용 독립 실행형 `.exe`나 설치 프로그램은 현재 제공하지 않습니다.

## 사용 방법

1. 필요하면 알아보기 쉬운 **세션 이름**을 입력합니다.
2. **녹음 시작**을 누르고 마이크 권한을 허용합니다.
3. 감지된 소리, 신뢰도, 스펙트로그램과 수집 현황을 확인합니다.
4. 정상적으로 마칠 때는 **완료**를 누릅니다. 이미 전송한 분석이 모두 끝난 뒤 세션을 정리합니다.
5. 즉시 멈추려면 **중단 (수집분 유지)**을 누릅니다. 이미 저장된 세그먼트는 남고, 처리 중인 청크는 취소됩니다.
6. 화면 아래 **수집된 데이터**에서 세그먼트를 재생·삭제하거나 세션을 내보냅니다.

녹음 중 **작게 보기**를 누르면 현재 상태, 녹음 시간과 최근 감지만 남긴 작은 화면으로 전환됩니다.

## 어떤 소리가 저장되나요?

기본 수집 규칙은 다음과 같습니다.

- 감지 결과가 없거나 모든 신뢰도가 50% 미만인 청크는 저장하지 않습니다.
- 말하기, 대화, 속삭임, 노래 등 음성 관련 라벨이 있는 청크는 저장하지 않습니다.
- 가까운 감지 구간을 합쳐 보통 5~20초 길이의 세그먼트로 만듭니다.
- 같은 종류가 지나치게 반복되거나 특정 소리만 많아지는 경우 일부 후보를 제외합니다.
- 한 세션에서 최대 600개, 총 60분, 예상 PCM 512 MiB까지 선택합니다.
- `ffmpeg`가 있으면 MP3로, 없으면 WAV로 저장하며 감지 정보는 JSON 메타데이터로 함께 기록합니다.

저장 위치는 다음과 같습니다.

```text
recordings/collected/<session-id>/
├── session.json
├── segment-....mp3 (또는 .wav)
└── segment-....json
```

화면에 감지가 보였는데 파일이 저장되지 않았다면 음성 제외, 반복 제한, 클래스 균형 또는 세션 상한 규칙이 적용됐을 수 있습니다. 전체 설정과 설명은 [`.env.example`](.env.example)을 참고하세요.

## 선택 기능

### GCS 업로드

GCS 업로드는 기본적으로 꺼져 있습니다. 사용하려면 실제 `.env`에 아래 세 변수의 값을 모두 설정합니다.

- `GCS_PROJECT_ID`
- `GCS_BUCKET_NAME`
- `GCS_OBJECT_PREFIX`

로컬 사용자 인증은 다음 명령으로 설정할 수 있습니다.

```bash
gcloud auth application-default login
```

앱은 객체를 새로 만드는 작업만 하므로 대상 버킷에 `roles/storage.objectCreator`처럼 필요한 범위가 작은 권한을 주는 것을 권장합니다. 인증정보는 브라우저로 전달되지 않습니다. 로컬 파일을 삭제해도 이미 업로드한 GCS 객체는 삭제되지 않습니다.

## 자주 생기는 문제

- **앱이 시작되지 않음:** `.env`의 `COCHL_PROJECT_KEY`와 백엔드 로그를 확인하세요.
- **마이크를 시작할 수 없음:** 브라우저/시스템 마이크 권한을 확인하세요. 앱이 48 kHz 입력과 자동 음성 보정 해제를 지원하지 못하는 환경에서는 안전하게 시작을 중단합니다.
- **다른 서버가 사용 중이라는 오류:** 같은 `recordings/` 폴더에는 백엔드 프로세스 하나만 실행할 수 있습니다.
- **GCS 버튼이 비활성화됨:** GCS 변수 3개, Google ADC 로그인과 버킷 IAM 권한을 확인한 뒤 백엔드를 다시 시작하세요.
- **감지나 수집이 적음:** 주변 소리가 모델의 지원 범위에 없거나, 신뢰도·음성 제외·반복 제한 규칙을 통과하지 못했을 수 있습니다.

<details>
<summary>개발자용 정보</summary>

### 프로젝트 구조

- `frontend/`: React/Vite 실시간 현황판
- `backend/`: FastAPI 로컬 서버와 Cochl.Sense Cloud 연동
- `macos/`: 로컬 서버와 웹 화면을 감싸는 macOS 앱
- `scripts/`: 앱 빌드·검증 및 지연 측정 도구
- `recordings/`: 로컬 수집 데이터(버전 관리 제외)

### 테스트

```bash
.venv/bin/python -m pytest backend/tests

cd frontend
npm test -- --run
npm run build
cd ..

/bin/zsh -n scripts/build-macos-app.sh scripts/run-macos-server.sh scripts/verify-macos-app.sh
```

macOS 앱 전체 검증:

```bash
scripts/build-macos-app.sh --clean
scripts/verify-macos-app.sh
```

백엔드 패키지 범위는 `backend/pyproject.toml`, 재현 가능한 검증 버전은 `backend/constraints.txt`에서 관리합니다.

</details>

## License

MIT
