# spring-migrate

Staged, **local-only** migration of a Spring Boot **2.7.x** project to **3.5** and then **4.0**.
It drives [OpenRewrite](https://docs.openrewrite.org) (the standard engine for Spring upgrades), adds a pre-flight
scanner, extra fixers for things OpenRewrite leaves behind, and build/test gates between stages.

**Your code never leaves your machine.** Nothing is uploaded; the only network traffic is Maven/Gradle downloading
OpenRewrite recipes and dependencies from the repositories your build already uses.

## Requirements
Python 3.9+ (stdlib only) - JDK 17+ (21 is fine) - Maven (or `./mvnw`) or Gradle (or `./gradlew`) - git.

## Recommended workflow
```bash
# 1. Read-only analysis. Start here.
python3 spring_migrate.py scan /path/to/project --out scan.md

# 2. Preview the whole change as a patch (modifies nothing)
python3 spring_migrate.py migrate /path/to/project --dry-run

# 3. Real run: new branch, one commit per stage, build+tests after each stage
python3 spring_migrate.py migrate /path/to/project --target 4.0 \
        --build-args "-s /path/to/corporate-settings.xml"      # optional: your Artifactory/Nexus mirror
```
The project must be a git repo with a clean tree. Run it on a copy/clone first.

## What `migrate` does
| Stage | Action |
|---|---|
| preflight | JDK check, clean-tree check, scan; **stops on blockers** (Springfox, Ribbon/Hystrix/Zuul, legacy OAuth2 server) unless `--force` |
| baseline | build + tests on the original code, so old failures are not blamed on the migration (stops if it fails; `--skip-baseline`) |
| 1 | 2.7.13 -> latest 2.7.x (safe checkpoint) |
| 1b | **OpenJPA -> Hibernate** (only when OpenJPA is detected, see below) |
| 2 | Boot 3.5: Spring 6, Jakarta EE 10, Security 6, Hibernate 6, Java 17/21, JUnit 4->5 (auto when detected) |
| 3 | Boot 4.0: Spring 7, modular starters, test annotations (`--target 3.5` stops after stage 2) |

After every stage the automated changes are **committed first**, then build+tests run. If a stage fails, your manual
fixes show up as a separate diff on top. Re-run the same command to **resume** - it detects the current Boot version.

Built-in fixers (idempotent, run after stages 2 and 3): leftover `javax.*` -> `jakarta.*` in resources/strings;
`spring.factories` `EnableAutoConfiguration` -> `AutoConfiguration.imports`; Java version in GitHub Actions,
`.java-version` and Dockerfile tags (`postgres:11` etc. are untouched; deprecated `openjdk` images are only reported).

Output (next to the project, or `--report-dir`): `MIGRATION_REPORT.md`, `scan-before.md`, `scan-after.md`, `logs/`.

## OpenJPA -> Hibernate
OpenRewrite has no recipe for this, so the tool does it itself, as its own stage **on Boot 2.7 (Hibernate 5.6, still `javax`)**
*before* the Spring 6 jump. That way a persistence regression is caught on its own commit with a green/red test gate,
not mixed into the Jakarta/Hibernate 6 upgrade. Run it alone with `spring_migrate.py openjpa PATH` (no build, no git).

Automatic:
* `persistence.xml`: provider -> Hibernate; `openjpa.ConnectionURL/Driver/User/Password`, `jdbc.Schema`, `jdbc.SynchronizeMappings`
  mapped to `javax.persistence.*` / `hibernate.*`; every other `openjpa.*` property is commented out with a `TODO(spring-migrate)`
  line that keeps the original value (dialect, cache, log, enhancement settings are dropped on purpose and listed in the report)
* pom/Gradle: OpenJPA dependencies, the enhancer plugin and `openjpa.version` removed; `spring-boot-starter-data-jpa` added
  if there is no Hibernate; Hibernate exclusions on the JPA starter removed
* `PersistenceProviderImpl` / `OpenJpaVendorAdapter` / `OpenJpaDialect` in code or XML -> Hibernate equivalents
* `application.properties`: identifier naming kept as-written (`PhysicalNamingStrategyStandardImpl`) so Boot's snake_case default
  does not break an OpenJPA-created schema (only matters when Boot's JPA auto-config is used)

Manual (inventoried with file:line and the closest replacement): `OpenJPAEntityManager`/`OpenJPAQuery`/`FetchPlan`,
`@DataCache`, `@ReadOnly`, `@Persistent`, `@Externalizer`, `@Dependent`... A leftover reference makes the stage's build fail, which
stops the run so you can fix it on top of the committed automatic changes and re-run.

The biggest risks are **not** compile errors but data behaviour; the report lists every `@GeneratedValue` entity and a checklist:
ID sequences (OpenJPA's `OPENJPA_SEQUENCE_TABLE` vs `hibernate_sequence` -> duplicate keys), schema/type mapping
(validate against a copy of production with `ddl-auto=validate`), lazy loading/detachment, collection ordering, JPQL strictness
(`?` parameters), caching, locking.

## Safety nets
* OpenRewrite exits 0 even when it could not download artifacts and silently skips edits (seen with rate limiting / a
  wrong mirror). The tool detects this, discards the partial edits, retries (`--retries`) and then fails loudly.
* Never touches the working tree unless it is clean (or `--allow-dirty`); always works on `migrate/spring-boot-<target>`.

## What it cannot do (needs a human)
Springfox -> springdoc, Sleuth -> Micrometer Tracing, Netflix OSS -> LoadBalancer/Resilience4j/Gateway, legacy
spring-security-oauth2, PowerMock, Hibernate 5 type mappings, HttpClient 4 -> 5, `GenerationType.AUTO` sequence naming
(can break existing schemas), trailing-slash matching, internal/third-party libraries without Jakarta versions.
The scanner lists each with file:line; the report ends with a checklist of things only tests can reveal.

## Boot 4.1
Boot 3.5 reached OSS end-of-life on 2026-06-30 and 4.0 follows at the end of 2026; 4.1 is the line with the longest
support. OpenRewrite has no 4.1 recipe yet (rewrite-spring 6.37 only ships `UpgradeSpringBoot_4_0`), so after stage 3
bump the parent to the latest 4.1.x by hand and read the 4.1 release notes. Use `--extra-recipe` for your own recipes.

## Verified / not verified
Verified end-to-end (`tests/e2e.sh`) with Maven on JDK 21:
* `examples/legacy-demo`: 2.7.13 -> 3.5.16 -> 4.0.10 (JPA, Security, MVC, validation, JUnit 4, spring.factories, Docker/CI)
* `examples/legacy-openjpa`: a project that really runs on OpenJPA 3.2.2 (enhancer plugin, `persistence.xml`, test persists and
  queries an entity) -> Hibernate 5.6 -> Boot 3.5 / Hibernate 6 -> Boot 4.0, with the same test green after every stage.
  Not covered by tests: OpenJPA-specific API/annotation rewrites (reported, not automated), non-H2 databases, Gradle.
**The Gradle path is implemented (init-script) but untested**; multi-module and Kotlin projects are untested.
Treat the result as a very good first 80-90%, not a finished migration: review the diff and run your integration tests.
