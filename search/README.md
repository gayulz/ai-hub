# search

웹 서버 없이 실행하는 Java 21·Spring Boot 4.1.2·Lucene/Nori 10.5.2 볼트 검색 도구다. 이번 단계는 볼트 읽기, 조각 분할, 증분 색인, 검색과 사용자 과제의 자리까지다.

## 빌드

- JDK 21, Gradle 9.8.2(Spring Boot 4.1.2는 Gradle 8.14 이상이 필요하다).
- Gradle Wrapper(`gradlew`, `gradle/wrapper/`)를 쓴다. `gradle-wrapper.jar`는 공개 검사 허용 목록에 내용 해시를 적어 둔 파일이다.

```
cd search
JAVA_HOME=$(/usr/libexec/java_home -v 21) ./gradlew --offline test
```

의존성은 Gradle 캐시에 미리 받아 두어야 한다. 추가 라이브러리는 사용하지 않는다. 테스트는 임시 폴더의 합성 볼트와 색인만 사용한다. `EvalRunnerTest` 하나는 사용자 과제라 건너뛴다.

## 명령

먼저 `./gradlew --offline bootJar`로 실행 파일을 만든다.

```sh
java -jar build/libs/ai-hub-search-0.1.0.jar index --search.vault=/tmp/acme-vault
java -jar build/libs/ai-hub-search-0.1.0.jar query "임시저장" --k 5
java -jar build/libs/ai-hub-search-0.1.0.jar query "스프링부트" --include-company
java -jar build/libs/ai-hub-search-0.1.0.jar eval
```

- 볼트는 `--search.vault=<경로>` 또는 `SEARCH_VAULT`로 지정한다. 명령줄 값이 우선이다.
- 색인 경로는 `search.index-dir` 설정 또는 `SEARCH_INDEX_DIR`로 바꾼다. 기본은 `${user.home}/.ai-hub/index`다.
- `index`는 추가·수정·삭제·변경 없음 노트 수, 색인의 전체 조각 수, 읽지 못한 파일 수를 한 줄로 출력한다. 같은 해시는 문서를 갱신하지 않는다. 읽지 못한 파일의 기존 조각은 보존하고, 폴더 탐색 실패는 색인을 중단한다. 빈 노트의 해시는 Lucene 커밋 메타데이터에 보관한다.
- `query`는 노트별 최고 점수 조각 하나를 선택해 점수순으로 출력한다. 기본 5개이며 회사 노트는 질의의 제외 조건으로 거른다. `--include-company`일 때만 포함한다.
- 정상 종료는 0, 파일·색인 작업 오류는 1, 사용법 오류·볼트 경로 누락·미구현 `eval`은 2다. 색인이 없으면 먼저 `index`를 실행하라는 오류를 낸다.

## 읽기와 분할

- 볼트 아래 `.md` 일반 파일만 UTF-8로 읽으며 링크는 따라가지 않는다. 비밀 접두 경로와 `.obsidian`, `.git`, `.trash` 폴더는 내용 읽기 전에 제외한다. 비밀 접두는 `VaultReader.SECRET_PREFIX` 한 곳에 정의한다.
- 경로와 전체 텍스트는 NFC로 정규화하고, 머리말을 포함한 전체 텍스트로 SHA-256을 계산한다. 머리말의 `type`, `sensitivity`, `created`와 인라인·목록 `tags`만 해석한다. 머리말이 없으면 빈 메타데이터가 되고, 머리말이 있는데 읽을 수 없으면 회사 노트(`sensitivity=company`)로 간주해 기본 검색에서 뺀다. `sensitivity` 값은 소문자로 비교한다.
- `## `에서 조각 구역을 나누고 도입부는 빈 소제목 경로를 쓴다. 더 깊은 제목은 본문에 남기며 조각 안에서 마지막으로 본 제목 계층을 ` > `로 연결한다. 코드 울타리 안의 제목은 무시한다.
- 소프트 상한을 넘으면 빈 줄로 나눈 문단을 묶는다. 한 문단이 하드 상한보다 길면 코드 포인트 단위로 자른다. 빈 조각은 만들지 않는다.

## 설정과 사용자 과제

`SearchSettings`가 기본값과 설정 바인딩을 담당한다. 색인과 검색 모두 `AnalyzerFactory`에서 분석기를 받고 같은 BM25 값을 사용한다. 분석기를 바꾼 뒤에는 별도의 빈 색인 경로에 전체를 다시 색인해야 한다. 증분 비교는 노트 해시를 기준으로 한다.

| 설정 | 기본값 |
|---|---|
| `search.chunk-soft-limit` / `search.chunk-hard-limit` | 1200 / 2000 코드 포인트 |
| `search.bm25-k1` / `search.bm25-b` | 1.2 / 0.75 |
| `search.body-boost` / `search.heading-boost` / `search.note-title-boost` | 1.0 / 1.5 / 2.0 |

6-2c에서 사용자가 직접 구현할 곳은 다음 세 곳이다.

1. `DefaultAnalyzerFactory`: 현재 기본 생성자의 `KoreanAnalyzer`만 반환한다. 사용자 사전, 복합어 MIXED·DISCARD, 영문 소문자 필터를 구현한다. 참고 클래스는 코드 주석에 적었다.
2. `SearchSettings`: BM25 k1·b를 조정한다.
3. `EvalRunner`: 현재 `UnimplementedEvalRunner`는 `사용자 과제` 예외만 낸다. `EvalQuery`의 정답은 노트 상대 경로 목록이고 `NoteSearcher`는 노트 경로 순위를 돌려준다. `EvalRow`는 질의별 검색 경로·Recall@5·역순위를 담는다. 구현 뒤 `EvalRunnerTest`의 실행기를 교체하고 `@Disabled`를 제거한다. 합성 질의 4개의 기대값은 Recall@5 0.75, MRR 0.425다.

평가 계산, 분석기 사용자화, 웹·MCP·벡터 검색은 이 단계에서 구현하지 않는다.
