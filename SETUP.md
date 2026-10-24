# 새 PC로 ai-hub 옮기기

macOS에서 ai-hub 코드와 로컬 상태를 옮긴 뒤 `install`과 `check`로 설치·점검한다. 코드 저장소에는 개인 설정·비밀키·볼트 원문을 넣지 않는다. 아래 URL·볼트 경로는 본인 값으로 바꾼다.

## 이전 순서

| 단계 | 옛 PC가 있을 때 | 옛 PC가 없을 때 |
| --- | --- | --- |
| 1. 프로그램 | Homebrew로 `git`, `git-crypt`, `age`, `python@3.14`를 설치한다. `claude`, `codex`, `gemini`, `agy` CLI도 각 배포 안내에 따라 설치한다. | 같음. |
| 2. 코드 | ai-hub 저장소를 clone하고 그 폴더로 이동한다. | 같음. |
| 3. 로컬 상태 | 옛 PC의 `~/.ai-hub`를 통째로 새 PC에 복사한다. 키 두 개의 권한을 확인한다. | 비밀번호 관리자에 보관한 키 두 개를 `~/.ai-hub/keys/`에 둔다. iCloud에 로그인하고 사본이 내려온 뒤 아래 복원 명령을 실행한다. 복원 결과의 `ai-hub/` 내용만 `~/.ai-hub/`로 옮긴다. |
| 4. 볼트 | 볼트 저장소를 clone하고 아래 명령으로 git-crypt 잠금을 푼다. `sources.toml`의 볼트·원천·첨부 경로를 새 PC에 맞춘다. | 같음. |
| 5. 설치 | `python3 -m hubsetup install`로 차이를 확인한 뒤 `python3 -m hubsetup install --apply`를 실행한다. | 같음. |
| 6. AI 로그인 | AI CLI 네 개에 각각 로그인한다. | 각각 로그인한다. 임시 복원 폴더의 `claude/`, `codex/`, `gemini/` 설정은 새 PC 설정과 비교한 뒤 필요한 것만 해당 도구의 설정 폴더로 옮긴다. |
| 7. 점검·수집 | `python3 -m hubsetup check`의 실패를 모두 해결한 뒤 `python3 -m collector run`을 한 번 실행한다. | 같음. |

프로그램과 코드 준비 예시:

```sh
brew install git git-crypt age python@3.14
git clone <ai-hub-저장소-URL> ~/workspace/ai-hub
cd ~/workspace/ai-hub
python3 --version
```

`python3`가 3.14 이상을 가리키도록 터미널 PATH를 먼저 맞춘다. 설치에 사용한 Python의 `sys.executable`이 도구·예약 실행에도 들어간다. 훅을 터미널에서 직접 사용할 수 있도록 `~/.ai-hub/tools`도 PATH에 추가한다.

## 상태와 볼트 복원

키 두 개는 `vault-git-crypt.key`와 `backup.agekey`다. 키는 비밀번호 관리자·별도 저장 장치에서 가져오고 내용을 출력하지 않는다. 옛 PC에서 복사한 경우에도 권한을 확인한다.

```sh
mkdir -p ~/.ai-hub/keys
chmod 600 ~/.ai-hub/keys/vault-git-crypt.key ~/.ai-hub/keys/backup.agekey
```

옛 PC가 없을 때는 iCloud의 `~/Library/Mobile Documents/com~apple~CloudDocs/ai-hub-backup/state/`에 접근할 수 있어야 한다. 목록과 필요한 암호화 파일이 내려온 뒤, 비어 있는 임시 폴더로 복원한다. 복원 명령은 기존 파일을 기본적으로 덮어쓰지 않는다.

```sh
python3 -m hubbackup restore --key ~/.ai-hub/keys/backup.agekey --to /tmp/ai-hub-restore-acme
cp -R /tmp/ai-hub-restore-acme/ai-hub/. ~/.ai-hub/
```

다음으로 볼트를 clone한다. 아래 볼트 경로는 예시다.

```sh
git clone <볼트-저장소-URL> ~/vault
git -C ~/vault crypt unlock ~/.ai-hub/keys/vault-git-crypt.key
```

`~/.ai-hub/sources.toml`의 `vault`, 분류·저장소 경로, 첨부 사본 경로와 `plans.toml` 안의 경로를 확인한다. 같은 사용자명이라도 원천 폴더는 저절로 복원되지 않으므로 별도로 옮긴다. 홈 아래 경로는 가능하면 `~/`로 적는다. 다른 사용자 홈을 가리키는 `/Users/xyz/` 같은 값이 남으면 `check`가 실패한다.

## 설치와 점검

저장소 폴더에서 실행한다.

```sh
python3 -m hubsetup install
python3 -m hubsetup install --apply
python3 -m hubsetup check
```

- 기본 `install`은 바뀔 파일의 unified diff와 권한 차이를 출력한다. 파일·Git 작성자·launchd를 바꾸지 않는다.
- `--apply`는 훅 두 개, 도구 두 개, plist 세 개를 쓰고 실행 권한을 맞춘다. 같은 파일은 건너뛴다. 바뀐 예약은 `launchctl bootout` 뒤 `bootstrap`하고, 파일이 같아도 등록이 빠졌으면 등록한다.
- 수집기는 매시 10분과 등록 직후, vaultsync는 12:30·23:30, 백업은 12:45·23:45에 실행된다. 따라서 `--apply` 전에 볼트 잠금 해제와 설정 수정을 마친다. 백업은 저장소에서 `python -m hubbackup run`으로 돌며 두 로그 스트림은 `<ai-hub 홈>/logs/hubbackup.log`에 쌓인다.
- `AI_HUB_HOME`을 지정했다면 그 폴더를 설정·도구·로그 기준으로 사용하고 예약 환경에도 전달한다. 기본은 `~/.ai-hub`다.
- 기본 launchd 라벨 접두어는 `com.`과 로그인 사용자명을 이은 값이다. 바꾸려면 install과 check 모두 같은 `--label-prefix com.acme`를 준다.
- 예약 PATH는 기존 설치의 홈 부분을 현재 홈으로 바꾼 값이다: `~/.nvm/versions/node/v22.22.1/bin`, `~/.local/bin`, `/opt/homebrew/bin`, `/usr/bin`, `/bin`. 필요한 AI CLI와 런타임을 이 경로에서 사용할 수 있게 준비한다. 새 PC의 셸 PATH와 예약 PATH는 다를 수 있다.

ai-hub 저장소 자체의 push 전 검사(공개 저장소용: 새 줄·경로·작성자·메시지의 회사 이름과 비밀값)는 저장소에 들어 있다. 새 PC에서 한 번 `git config core.hooksPath .githooks`로 연결한다.

작성자는 `sources.toml`의 다음 항목에서 읽는다. 예시 값은 합성 값이므로 본인에게 맞게 바꾼다.

```toml
[hubsetup]
author_name = "acme"
author_email = "xyz@example.invalid"
```

또는 `install --apply --author-name <이름> --author-email <이메일>`로 전달한다. 두 값이 없으면 작성자 설정만 건너뛰고 안내한다. 값이 있으면 볼트의 로컬 `user.name`·`user.email`만 설정한다. 전역 Git 설정에는 쓰지 않는다.

`check`는 다음을 표로 출력한다. 실패가 하나라도 있으면 종료 코드 1, 실패가 없으면 0이다.

| 항목 | 기준 |
| --- | --- |
| 프로그램 | git·git-crypt·age·age-keygen, 실행 중인 Python 3.14 이상. AI CLI 네 개의 부재는 경고다. 로그인 여부는 검사하지 않는다. |
| 설정 | sources·plans의 읽기·형식, 경로 존재와 다른 사용자 홈의 잔존 여부. glob은 일치하는 경로가 있는지, 상대 doc_dirs는 해당 저장소 아래에 있는지 확인한다. |
| 키 | 키 두 개가 일반 파일이고 권한이 정확히 `0o600`인지 확인한다. 내용은 읽지 않는다. |
| 볼트 | 내부 git-crypt 키의 존재와 필수 clean·smudge 설정, 로컬 작성자 설정을 확인한다. 노트 본문은 읽지 않는다. |
| 설치물·예약 | 템플릿 결과와 내용·권한 일치, plist 세 개의 `launchctl print gui/<uid>/<label>` 성공 여부. |
| 추가 훅 | post-commit 등 템플릿에 없는 활성 훅은 경고만 표시한다. install은 이 훅이나 관리 목록 밖 파일을 지우지 않는다. Git 기본 `.sample` 훅은 제외한다. |
| 백업 | iCloud 사본 폴더 접근·쓰기 권한, hubbackup status의 마지막 백업이 36시간 이내이고 마지막 실패가 0인지 확인한다. |

로컬 백업 기록이 없는 복원 직후에는 마지막 백업 점검이 실패할 수 있다. 상태를 확인하고 `python3 -m hubbackup run`을 한 번 실행하거나 다음 예약 성공 후 check를 다시 실행한다. check 자체는 백업·복원·수집을 실행하지 않는다.

다른 저장소의 볼트 경로 설정은 각 저장소에서 따로 바꾼다.

## 확인 범위와 한계

새 PC 실물 시험은 하지 못했다. 임시 HOME·임시 볼트 Git·가짜 launchctl로 설치 미리보기, 적용, 재적용과 점검을 리허설했다. 실제 홈에 대한 적용과 실제 launchd 등록·예약 실행은 별도로 확인해야 한다. 종료 코드 0은 iCloud 원격 업로드 완료나 AI 로그인 성공까지 보장하지 않는다.

```sh
python3 -m unittest discover -s tests
```
