# search

볼트 검색 엔진(Lucene·Nori, BM25). 계획은 볼트 `🗂️ Projects/ai-hub/261024-04-search-engine-plan.md`와 `261024-07-search-62-plan.md`에 있다.

## 빌드

- JDK 21, Gradle 9.8.2(Spring Boot 4.1.2는 Gradle 8.14 이상이 필요하다).
- 공개 저장소 검사가 바이너리를 거부하므로 Gradle Wrapper(`gradle-wrapper.jar`)는 넣지 않았다. 설치된 Gradle 9.x로 직접 부른다.

```
cd search
JAVA_HOME=$(/usr/libexec/java_home -v 21) gradle --offline test
```

의존성은 `~/.gradle`에 미리 받아 두었다. 처음 한 번만 `--offline` 없이 받는다.
