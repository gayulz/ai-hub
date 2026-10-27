# search

Java 21·Spring Boot·Lucene/Nori로 만든 로컬 볼트 검색 도구다. 노트를 읽어 조각 단위로 증분 색인하고, BM25·벡터·RRF 검색, TOML 평가와 점수 기록, 읽기 전용 로컬 REST를 제공한다. 새 라이브러리는 추가하지 않는다.

## 준비와 시험

JDK 21과 캐시에 준비된 Gradle 9.8.2 의존성이 필요하다. `eval`은 `PATH`의 `python3`가 Python 3.11 이상이어야 한다. TOML은 Python 표준 `tomllib`로 해석하며 별도 TOML·JSON 라이브러리를 설치하지 않는다.

```sh
cd search
JAVA_HOME=$(/usr/libexec/java_home -v 21) ./gradlew --offline test
JAVA_HOME=$(/usr/libexec/java_home -v 21) ./gradlew --offline bootJar
```

JUnit은 임시 폴더의 합성 볼트·색인·평가 파일과 FakeEmbedder·로컬 가짜 HTTP 서버를 쓴다. 실제 볼트나 실제 Ollama를 호출하지 않는다. 분석기 토큰, 지표 경계, TOML 입력·점수 로그, 설정 변경과 롤백, RRF, REST 및 종료를 시험한다. 시험 통과는 실제 검색 품질을 보장하지 않는다. 실제 볼트의 BM25·vector·hybrid 점수는 고정한 로컬 자료로 따로 측정하고 자료 기준 시각·설정·모드를 함께 기록한다.

## 실행 명령

아래 경로와 노트 이름은 합성 예시다. 실행 JAR의 이름은 빌드 결과를 확인한다.

```sh
java -jar build/libs/ai-hub-search-0.1.0.jar index --search.vault=/tmp/acme-vault
java -jar build/libs/ai-hub-search-0.1.0.jar index --search.vault=/tmp/acme-vault --no-vector
java -jar build/libs/ai-hub-search-0.1.0.jar query "임시저장" --k 5
java -jar build/libs/ai-hub-search-0.1.0.jar query "스프링부트" --mode vector
java -jar build/libs/ai-hub-search-0.1.0.jar query "임시저장" --mode hybrid --include-company
java -jar build/libs/ai-hub-search-0.1.0.jar eval
java -jar build/libs/ai-hub-search-0.1.0.jar eval --set /tmp/acme-eval.toml --mode hybrid --k 5 --log /tmp/acme-scores.md
java -jar build/libs/ai-hub-search-0.1.0.jar serve
java -jar build/libs/ai-hub-search-0.1.0.jar serve --bind 127.0.0.1 --port 8791
java -jar build/libs/ai-hub-search-0.1.0.jar serve --allow-company
```

- `index`는 벡터 포함이 기본이다. `--no-vector`를 주면 Ollama를 호출하지 않는다. 볼트는 `--search.vault=...` 또는 `SEARCH_VAULT`로 지정하며 명령 인자가 우선이다. 색인 결과는 추가·수정·삭제·변경 없음 노트 수, 전체 조각 수, 읽지 못한 파일 수, 이번 벡터 생성 수다.
- `query` 기본은 BM25·5개·회사 제외다. `--mode bm25|vector|hybrid`, 양수 `--k`, `--include-company`를 지원한다. 노트마다 최고 점수 조각 하나를 골라 순위·점수·경로·소제목·스니펫을 출력한다.
- `eval` 기본도 BM25·k=5다. 기본 평가 파일은 `~/.ai-hub/search-eval.toml`, 기본 누적 로그는 `~/.ai-hub/search-eval-log.md`다. `--set`, `--mode`, `--k`, `--log`로 바꾼다. 원본 TOML은 수정하지 않으며 원본과 같은 파일을 로그로 지정하면 거부한다.
- `serve`는 종료 신호를 받을 때까지 실행된다. 기본 바인딩은 `127.0.0.1:8791`이며 회사 노트 조회(`includeCompany=true`)는 기본으로 403을 반환한다. `--allow-company`를 주면 회사 조회를 허용하고 포트 출력 다음 줄에 허용 모드를 알린다. AI 도구에 검색을 열 때 회사 노트가 새지 않게 하려는 기본 정책이다. `--port 0`은 시험용 임시 포트를 배정하고 실제 포트를 첫 줄로 출력한다. 종료 시 HTTP 서버·작업 executor·Spring 컨텍스트를 정리한다.
- CLI 정상 종료는 0, 파일·색인·임베딩·로그·서버 시작 실패는 1, 사용법·TOML 형식·타입·정답 경로 오류는 2다. 알 수 없는 인자나 중복 옵션을 거부한다. 벡터 실패를 BM25로 대체하지 않는다.

## 설정

`SearchSettings`에 기본값과 범위 검사가 모여 있다. Spring 설정 파일 또는 환경변수로 설정한다. 예를 들어 `SEARCH_INDEX_DIR`, `SEARCH_DECOMPOUND_MODE`, `SEARCH_USER_DICTIONARY`, `SEARCH_BM25_K1`, `SEARCH_EMBEDDING_MODEL`을 사용할 수 있다. 전역 검색 설정은 환경변수로 주고, 각 명령에는 위에 적힌 명령 인자를 준다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `search.index-dir` | `${user.home}/.ai-hub/index` | Lucene 색인 폴더 |
| `search.vault` | 없음 | 읽을 볼트 |
| `search.decompound-mode` | `DISCARD` | `DISCARD`, `MIXED`, `NONE` |
| `search.user-dictionary` | 패키지 자원 | UTF-8 Nori 사용자 사전 파일 |
| `search.bm25-k1` / `search.bm25-b` | `1.2` / `0.3` | k1은 0 이상 유한값, b는 0~1 |
| `search.body-boost` / `search.heading-boost` / `search.note-title-boost` | `1.0` / `1.5` / `2.0` | 0보다 큰 유한한 필드 가중치 |
| `search.chunk-soft-limit` / `search.chunk-hard-limit` | `1200` / `2000` | 조각 코드 포인트 상한 |
| `search.embedding-model` | `bge-m3` | 로컬 임베딩 모델 |
| `search.embedding-dimension` | `1024` | 양수 벡터 차원 |
| `search.embedding-batch` | `8` | 양수 배치 크기 |
| `search.ollama-url` | `http://127.0.0.1:11434` | 로컬 Ollama HTTP(S) 주소 |

기본 BM25는 `k1=1.2, b=0.3`이다. 고정한 로컬 평가 20질의에서 두 Nori 모드와 일곱 설정을 비교해 골랐다. 기존 `1.2/0.75` 대비 Hit@5는 17/20을 유지했고 MRR@5는 0.6142에서 0.6667로 높아졌다. 두 모드의 최고 점수가 같아 DISCARD를 유지했다. 이 수치는 해당 평가 세트의 결과이며, 비교할 때 자료·정답·다른 설정을 고정해 점수를 함께 기록한다.

### Nori

기본 기술어 사전은 `src/main/resources/nori-user-dictionary.txt`다.

```text
임시저장 임시 저장
스프링부트 스프링 부트
```

- `DISCARD`는 복합어 대신 분해 토큰을 쓴다. `임시저장`은 `임시`, `저장`이 된다.
- `MIXED`는 원래 복합어와 분해 토큰을 함께 쓴다.
- `NONE`은 복합어를 유지한다.
- 사용자 파일을 지정하면 기본 사전을 대체한다. 빈 UTF-8 파일을 지정하면 사용자 사전을 끄고 Nori 기본 사전만 쓴다. 없는 파일·잘못된 사전·잘못된 모드는 분명한 오류로 거부한다.
- `KoreanAnalyzer`의 기본 불용 품사와 영문 소문자 처리를 재사용한다. 별도 필터 체인을 중복 구현하지 않는다.

### 벡터와 RRF

`index`는 노트 제목·소제목 경로·본문을 줄바꿈으로 이어 임베딩하고, 조각별 `KnnFloatVectorField`에 COSINE 벡터를 저장한다. Ollama는 로컬 `127.0.0.1`, `localhost`, `::1`만 허용하며 프록시·리다이렉트를 쓰지 않는다. 연결 제한은 5초, 본문을 포함한 응답 제한은 120초다. 오류에 입력·응답 원문을 넣지 않는다.

벡터 검색은 후보 조각 `k*5`개, 최대 100개에서 노트별 최고 조각 하나를 남긴다. 후보 내 조각 중복이 많으면 결과가 k개보다 적을 수 있다. 회사 제외 조건은 KNN 필터 안에서 적용한다.

하이브리드는 BM25 노트 목록과 벡터 노트 목록을 각각 최대 100노트 범위에서 받아 RRF를 계산한다. 각 목록에서 중복 노트를 제거하고 1부터 시작하는 순위마다 `1 / (60 + rank)`를 더한다. 합산 점수 내림차순, 동점은 경로 오름차순이며 최대 k개를 반환한다. 표시할 제목·소제목·스니펫은 BM25에 있으면 그 조각을, 없으면 벡터 조각을 쓴다. 회사 필터는 양쪽 검색 내부에 유지한다. 벡터 오류가 나면 하이브리드도 오류로 끝난다.

## 설정 변경과 색인 이전

Lucene commit user data에 `schema`, `analysis` fingerprint, 벡터 모델·차원, 마지막 전체 재색인 사유(`reindex`), 빈 노트의 `empty:` 해시를 기록한다. 사전 fingerprint는 경로가 아니라 내용과 Nori 모드를 SHA-256으로 계산한다.

| 변경 | 동작 |
|---|---|
| Nori 모드·사전 내용 | 노트 해시가 같아도 전체 재색인 |
| 임베딩 모델·차원 | 벡터 포함 색인을 전체 재색인 |
| 벡터 포함/`--no-vector` 전환 | schema 2/1을 전환하며 전체 재색인 |
| schema 또는 필요한 설정 메타데이터 누락 | 다음 `index`에서 안전하게 전체 재색인 |
| BM25 k1·b·검색 필드 가중치 | 재색인 없이 검색에 적용 |
| 사전 경로만 바뀌고 내용이 같음 | 전체 재색인 없음 |

검색은 분석기 fingerprint가 다르거나 누락되면 재색인을 안내하며 거부한다. schema 2에서는 모델·차원도 일치해야 한다. schema 1에서 vector·hybrid를 요청하면 `--no-vector` 없이 색인하라는 오류를 낸다. 이전 schema 1/2 색인은 `index`로 메타데이터를 채워야 한다.

전체 재색인은 벡터 필드 정의도 새로 만들어 차원 변경을 허용한다. 기존 노트를 읽지 못한 상태에서 설정을 바꾸면 전환을 중단한다. 임베딩·색인 작업이 실패하면 새 변경을 커밋하지 않고 이전 커밋과 메타데이터를 보존한다. 같은 설정·해시로 재실행하면 조각을 갱신하거나 다시 임베딩하지 않는다. 빈 노트도 해시를 추적한다.

조각 상한을 바꾸어 이미 색인된 노트의 분할까지 바꾸려면 별도 빈 색인 폴더에서 전체 색인한다. 현재 설정 fingerprint는 분석기·임베딩 모델·차원만 기록한다.

## 평가 TOML과 지표

```toml
[[query]]
id = "q1"
text = "임시저장"
company = false
answers = ["acme/p01.md", "xyz/p01.md"]

[[query]]
id = "q2"
text = "스프링부트"
company = true
answers = ["acme/p02.md"]
```

`company`는 해당 질의에서 회사 노트를 포함할지 정하는 불리언이다. 정답은 볼트 안의 `.md` 상대 경로다. NFC로 정규화해 비교하며 원본 정답은 바꾸지 않는다. 빈 정답·중복 ID·잘못된 타입, 절대 경로·상위 이동·비밀 접두 경로·`.git`·`.obsidian`·`.trash`를 거부한다. ID는 1~64자의 문자·숫자·밑줄·하이픈만 허용한다. 파일은 최대 4 MiB, 질의는 최대 10,000개, 질의 본문은 최대 8,192자, 질의별 정답은 최대 1,000개다.

평가 파일에 이미 있는 `category`, `source`, `answer_candidates`는 계산에 사용하지 않는 메타데이터로 받아들인다. 출처나 후보가 가리키는 파일을 열지 않고 확정된 `answers`만 사용한다. 평가 파일과 사용자 사전은 입력 경로와 실제 경로 모두 비밀 접두를 검사해 심볼릭 링크 별칭도 거부한다.

`EvalRunnerImpl`은 사용자가 작성한 계산 흐름을 이어받는다. 각 질의의 상위 k개에서 정답을 처음 찾은 순위를 계산한 뒤 평균을 낸다.

- `recallAt5`는 호환용 이름이며 실제 의미는 **Hit@k**다. 정답이 여러 개여도 하나 이상 상위 k개에 있으면 1, 없으면 0이다. 정답 전체를 얼마나 찾았는지 나타내는 일반적인 recall과 다르다.
- `MRR@k`는 첫 정답 순위의 역수 평균이다. 1등은 1, 2등은 0.5이며 상위 k개 밖의 정답은 0이다.
- k는 양수다. 검색기가 k보다 많이 반환해도 k까지만 계산한다. 짧은·빈 결과를 허용하고 질의가 없으면 두 평균이 0이다. 입력·반환 목록은 복사해 외부 변경을 막는다.
- 사용자 합성 질의 네 개의 기대값은 Hit@5 `0.75`, MRR@5 `0.425`다. 실제 볼트에서 측정한 품질 점수가 아니다.

평가 CLI 출력과 로그에는 모드·집계 점수·질의 ID별 적중·역순위만 표시한다. 로그에는 시각, Nori 모드·사전 fingerprint, BM25·필드 가중치, 모델·차원도 남긴다. 실제 질의 본문·정답 경로·검색 경로는 출력하거나 기록하지 않는다. 실패한 평가는 부분 점수 로그를 쓰지 않는다.

TOML 읽기는 패키지 자원의 작은 `eval_set.py`를 임시 폴더에 복사해 `python3 -I`로 실행한다. Java는 길이가 붙은 UTF-8 결과만 읽는다. 파서 표준 출력·오류는 버리고 10초 제한을 두며 종료·실패 시 프로세스와 임시 파일을 정리한다. Java에 범용 TOML 파서를 만들지 않는다.

## 로컬 REST

```sh
curl 'http://127.0.0.1:8791/health'
curl --get 'http://127.0.0.1:8791/search' --data-urlencode 'q=임시저장'
curl --get 'http://127.0.0.1:8791/search' --data-urlencode 'q=스프링부트' --data 'k=5&mode=hybrid&includeCompany=false'
```

`GET /health`는 프로세스가 요청을 받으면 `{"status":"ok"}`를 반환한다. 색인 유무나 Ollama 품질을 검사하는 명령은 아니다.

`GET /search`는 `q` 필수, `k=5`, `mode=hybrid`, `includeCompany=false`가 기본이다. `includeCompany`는 서버를 `--allow-company`로 켠 경우에만 `true`가 통하며, 기본 서버에서는 유효한 `includeCompany=true` 요청을 검색 전에 403으로 거부한다. `k`는 1~100, 질의는 최대 2,048 UTF-16 문자, 전체 인자 문자열은 최대 16,384자다. 중복·알 수 없는 인자·잘못된 불리언·잘못된 인코딩·너무 긴 입력은 400이다. 응답은 `{"hits":[...]}`이며 각 원소에 `path`, `noteTitle`, `heading`, `score`, `snippet`, `sensitivity`를 담는다.

읽기 전용이며 비로컬 bind·Host·Origin을 차단한다. Origin이 있으면 HTTP 로컬 호스트와 현재 포트만 허용한다. CORS 허용 헤더는 보내지 않는다. GET 이외는 405, 없는 경로는 404, 색인·임베딩 실패는 503, 예상하지 못한 내부 실패는 500이다. 내부 오류 메시지나 실제 질의·응답·노트 내용을 서버 로그에 남기지 않으며 오류 응답에도 원문을 넣지 않는다. 응답은 `no-store`다.

## 읽기·분할과 코드 흐름

`VaultReader`는 볼트 아래 UTF-8 `.md` 일반 파일만 읽고 링크를 따라가지 않는다. 비밀 접두와 `.git`·`.obsidian`·`.trash`는 내용 읽기 전에 제외한다. 경로·텍스트를 NFC로 맞추고 머리말을 포함한 전체 내용의 해시를 계산한다. 머리말을 읽을 수 없는 노트는 회사 노트로 취급해 기본 검색에서 제외한다.

`Chunker`는 `## ` 구역과 문단, 코드 포인트 상한으로 나눈다. 코드 울타리 안의 제목은 무시하고 빈 조각은 만들지 않는다. `NoteIndexer`는 해시와 설정을 비교하고 새 조각·벡터를 모두 준비한 뒤 커밋한다. `SearchService`는 같은 분석기·회사 필터를 써 노트별 결과를 만들고 RRF로 결합한다. `EvalRunnerImpl`은 검색 함수를 받아 계산만 하며 파일 읽기·로그 쓰기는 별도 책임이다. `CommandRouter`가 CLI를, `LocalSearchServer`가 HTTP 입력과 서버 수명을 담당한다.
