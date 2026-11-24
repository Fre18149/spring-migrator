# spring-migrate

Staged, **local-only** migration of a Spring Boot **2.7.x** project to **3.5**, **4.0** and **4.1**.
It drives [OpenRewrite](https://docs.openrewrite.org) (the standard engine for Spring upgrades), and adds what OpenRewrite
does not do: codemods for removed libraries (Netflix OSS, legacy spring-security-oauth2, OpenJPA), behaviour-preserving
fixes (ID generation, trailing slashes, token formats), repairs for things the recipes get wrong, and build/test gates
between stages.

**Your code never leaves your machine.** Nothing is uploaded. Network traffic is Maven/Gradle downloading recipes and
dependencies from the repositories your build already uses, plus version lookups (`maven-metadata.xml` on Maven Central,
the SDKMAN version list) to pick the latest patch releases. `--offline` skips the lookups and uses built-in versions.

## Requirements
Python 3.9+ (stdlib only) - JDK 17+ (21 is fine) - Maven (or `./mvnw`) or Gradle (or `./gradlew`) - git.

## Recommended workflow
```bash
# 1. Read-only analysis. Start here.
python3 spring_migrate.py scan /path/to/project --out scan.md

# 2. Preview the OpenRewrite part as a patch (modifies nothing)
python3 spring_migrate.py migrate /path/to/project --dry-run

# 3. Real run: new branch, one commit per stage, build+tests after each stage
python3 spring_migrate.py migrate /path/to/project \
        --build-args "-s /path/to/corporate-settings.xml"      # optional: your Artifactory/Nexus mirror
```
The project must be a git repo with a clean tree. Run it on a copy/clone first. The default target is **4.1**
(`--target 3.5|4.0|4.1`).

## Updating an already migrated branch
Every migration commits `.spring-migrate/state.json` (tool version, target, Java, Boot line reached, the options that
matter - no paths or credentials). When a newer spring-migrate fixes something, apply it to the existing branch
instead of migrating again:
```bash
python3 spring_migrate.py update /path/to/migrated-project --build-args "-s settings.xml"
```
`update` prints what changed since the version the branch was migrated with (`CHANGELOG` in the script), runs this
version's post-fixers for the Boot line the branch is on (they are idempotent: what is already right stays as it is),
formats the changed files, commits them as one `spring-migrate update <old> -> <new>` commit, and runs the build gate
(with the compile autofix). Branches migrated before 2.1.0 have no state file; Boot and Java are then read from the
POMs. It also works on a branch whose stage stopped with a failing build. In the dashboard: **Apply tool updates** on
a finished job (runs in the same copy), or *Update a migrated branch* in the new-job dialog.

## Web dashboard
```bash
python3 spring_migrate_web.py            # then open http://127.0.0.1:8765
python3 spring_migrate_web.py --parallel 2 --port 9000
```
A local web app around the same tool (stdlib only, no install). It runs `spring_migrate.py` for you and shows:
* **Live progress**: a timeline of the steps (pre-flight, baseline, company dependencies, each stage with duration
  and number of tool changes, retries) and the streaming log, filterable to steps/changes or errors/TODOs
* **Many projects**: paste several paths, browse folders, or "find projects in a folder" (Maven/Gradle builds found
  3 levels deep, with their Boot version); each project becomes a job in a queue. Jobs run **one after the other**
  by default, or up to 4 **at the same time** (selector in the header; parallel builds share `~/.m2`)
* **Every option** of `migrate` (generated from the CLI, so it always matches), plus *Scan only* and *Company
  dependency plan* jobs. The last options are remembered in the browser
* **Work on a copy** (default): the project is copied into the dashboard's state folder, so your checkout is never
  touched. If the copy is not a git repository of its own, or has uncommitted changes, the dashboard commits a
  baseline in the copy
* **Cancel** (stops Maven/Gradle too), **Re-run / resume** (a stopped migration continues from the Boot version it
  reached, in the same copy), **Delete**
* The **report**, the **dependency plan** and the **scan** rendered in the page, and the exact command line to
  reproduce a job in a terminal

Jobs, logs and reports are kept in `~/.spring-migrate-web` (`--state-dir`) and survive restarts. The server binds to
`127.0.0.1` and refuses cross-site requests; it runs commands on your machine, so `--host 0.0.0.0` prints an access
token that every request must carry. Dependency questions (`--interactive`) need a terminal; in the dashboard,
review the plan, edit `dependency-plan.json` and pass it with the *dependency-plan* option.

## What `migrate` does
| Stage | Action |
|---|---|
| preflight | JDK check, clean-tree check, scan. Stops only on findings that are still manual *and* blockers (`--force` to continue) |
| baseline | build + tests on the original code, so old failures are not blamed on the migration (`--skip-baseline`) |
| 1 | 2.7.13 -> latest 2.7.x (safe checkpoint) |
| 1b | **OpenJPA -> Hibernate** on Boot 2.7 / Hibernate 5.6 (only when OpenJPA is detected) |
| 2 | legacy-library codemods, then Boot 3.5: Spring 6, Jakarta EE 10, Security 6, Hibernate 6, Java 17/21, JUnit 4->5 |
| 3 | Boot 4.0: Spring 7, modular starters, Security 7 |
| 4 | Boot 4.1: version bump to the latest 4.1.x + `SpringBootProperties_4_1` (OpenRewrite has no 4.1 upgrade recipe) |

After every stage the changes are **committed first**, then build+tests run **with `spring-boot-properties-migrator`
on the classpath**. Renames it reports are applied to your `application*.properties|yml`, committed, and the build is
re-run without it. If a stage fails, your fixes show up as a separate diff on top; re-run the same command to
**resume** (it detects the current Boot version). After the last stage the runtime classpath is scanned for jars declared in your
build that still reference only `javax.*` EE types (typically internal libraries); they are listed in the report.

## What is automated
**By OpenRewrite** (`UpgradeSpringBoot_3_5` already contains these): Springfox -> springdoc (annotations, Docket ->
`springdoc.group-configs`), Sleuth -> Micrometer Tracing, PowerMock -> Mockito, HttpClient 4 -> 5, Hibernate 5 type
mappings -> hypersistence-utils, version-specific dialects, `WebMvcConfigurerAdapter`, `ListenableFuture`,
`getRawStatusCode`, Spring Cloud 2025, Security DSL, `javax` -> `jakarta`, property renames.

**By this tool**, before the 3.5 recipes (result still compiles on 2.7, because the rewrite plugin compiles first):
* **Hystrix -> Resilience4j**: `@HystrixCommand` -> `@CircuitBreaker` (commandKey/method name, fallback gets the
  `Throwable` parameter Resilience4j requires), `@HystrixProperty`/`hystrix.command.*` settings ->
  `resilience4j.*`, `ignoreExceptions`, Feign circuit breaker flag; starter swap (+ Spring Cloud BOM if missing)
* **Ribbon -> Spring Cloud LoadBalancer**: `@RibbonClient(s)`, `RandomRule`/`RoundRobinRule` beans ->
  `ReactorLoadBalancer` beans, `<client>.ribbon.listOfServers` -> `spring.cloud.discovery.client.simple.instances`
* **Zuul -> Spring Cloud Gateway MVC** (servlet, like Zuul 1): `zuul.routes.*` (path/url/serviceId, `zuul.prefix`) ->
  `spring.cloud.gateway.server.webmvc.routes` with `StripPrefix` and Zuul's default sensitive-header stripping
* **Legacy OAuth2 resource server** -> `oauth2ResourceServer` `SecurityFilterChain` (your rules kept): JWK set URI,
  PEM public key, HMAC key (generates a `JwtDecoder`), `token-info-uri` -> opaque-token introspection, `resourceId` ->
  `audiences`, a `JwtAuthenticationConverter` that keeps the legacy `authorities` claim, `#oauth2.hasScope(...)` ->
  `hasAuthority('SCOPE_...')`
* **Legacy OAuth2 client**: `@EnableOAuth2Sso` -> `oauth2Login()`, `security.oauth2.client.*` ->
  `spring.security.oauth2.client.registration/provider.*`
* **ID generation**: implicit `@GeneratedValue` (AUTO/SEQUENCE without generator) pinned to the Hibernate 5 behaviour
  (`hibernate_sequence`, increment 1; IDENTITY for MySQL-family with `new_generator_mappings=false`), so Hibernate 6 does
  not switch to `<entity>_seq` tables and collide with existing rows
* JDBC-style `?` JPQL parameters numbered (`createQuery`, `@Query`, `@NamedQuery`, `orm.xml`; native queries untouched)
* Stale pins of Boot-managed versions removed (`mockito.version`, `hibernate.version`, `lombok.version`, ...)

After the 3.5 recipes:
* **Legacy authorization server -> Spring Authorization Server**: in-memory clients -> `RegisteredClient`s (secrets,
  grants, scopes, redirect URIs, token lifetimes with the legacy defaults of 12 h / 30 days, autoApprove -> consent,
  opaque vs JWT format), the **same signing key** (HMAC key or keystore) so existing resource servers keep verifying
  tokens, a `client_id` claim, and the **legacy endpoint paths** `/oauth/token`, `/oauth/authorize`,
  `/oauth/check_token`; a login chain when the app had none
* Trailing slashes: a `TrailingSlashCompatibilityConfig` (`UrlHandlerFilter`, ordered **before** Spring Security, so
  `/admin/` is checked as `/admin`) keeps Spring 5 matching (`--no-trailing-slash-compat` to skip)
* Fixes for OpenRewrite output: quoted values written into `.properties` (they become part of the value), the unused
  springdoc `Info` method (-> `OpenAPI` bean), `/v2/api-docs` security rules, HttpClient 5's now-checked
  `ParseException`, `spring-boot-starter-aop` dropped by the 4.0 recipe without its replacement
  `spring-boot-starter-aspectj` (would silently disable `@Aspect`/Resilience4j annotations), Authorization Server
  configurer package moved in Spring Security 7
* httptrace -> httpexchanges (classes, `management.trace.http.*`, endpoint exposure)
* Hibernate 5 settings: version-specific dialects -> generic, `new_generator_mappings` removed
* springdoc kept consistent: OpenRewrite moves springdoc code to 2.x packages already in the Boot 2.7 stage and only
  upgrades springdoc dependencies that carry their own version. The tool keeps code and dependency on the same line
  after every stage (1.x on Boot 2.7, 2.x on 3.5, 3.x on 4.x): artifact renames (`springdoc-openapi-ui` ->
  `springdoc-openapi-starter-webmvc-ui`, merged modules removed) also in the root's `<dependencyManagement>`,
  version properties, and a pin in the root when the version comes from a company parent/BOM
* Spring Cloud release train aligned to each Boot line (3.5 -> 2025.0, 4.0/4.1 -> 2025.1, latest patch)
* Containers and CI: deprecated `openjdk`/`adoptopenjdk` images -> `eclipse-temurin`, `maven:*-openjdk-11` images,
  Jenkins `jdk` tools, GitLab/Travis/Azure/Bitbucket/CircleCI pins, `.sdkmanrc` (resolved via SDKMAN), `.tool-versions`,
  GitHub Actions, `.java-version`, Dockerfile tags
* Maven wrapper < 3.6.3 and Gradle wrapper below the Boot minimum bumped
* Third-party: MapStruct < 1.5.5, Lombok pinned in `annotationProcessorPaths`, Querydsl `jakarta` classifier

## Multi-module builds
Point the tool at the **root** (aggregator) project; all modules are migrated together, in one branch, with one build
gate per stage over the whole reactor:
* OpenRewrite runs over every module; the codemods work per module (Hystrix in one module only changes that module's
  POM, config and code; Resilience4j/AOP starters go only where the code needs them).
* Versions managed in the root's `<dependencyManagement>` are cleaned up when a library is replaced, and a BOM that a
  module needs (e.g. Spring Cloud) is added once, to the top of the build, instead of to each module.
* Every Spring Boot application module gets its own trailing-slash filter, but only web modules.
* The build's own modules (the root as `<parent>` of the children, sibling modules as dependencies) are never treated
  as libraries to upgrade from your repository; a company parent or BOM above the root still is.
* If the children only inherit Boot through the root, the Boot version is read from the modules that resolve it.

If your company parent POM is a project of its own, with the applications in separate repositories: migrate the
parent first and release it. Then migrate the applications (several at once in the dashboard): the dependency check
finds the released parent version that is built for each Boot line and moves them to it.

## Company parents, BOMs and internal libraries (Artifactory / Nexus)
OpenRewrite only upgrades `spring-boot-starter-parent` and libraries it knows. A **company parent POM** that inherits
from Boot, an internal **BOM**, or **internal starters** built against Boot 2 are left alone. The last case is the worst:
a Boot 2 starter registered via `spring.factories` is silently ignored by Boot 3, so nothing fails to compile and its
beans just vanish.

`migrate` handles this automatically (Maven):
1. It resolves through **your own Maven and settings**, so pass your Artifactory settings as usual:
   `--build-args "-s /path/to/settings.xml"`. Mirrors, credentials and encrypted passwords work because Maven does the
   downloading. The Boot version is read through the parent chain when it is not in your POM.
2. For every parent, imported BOM and explicitly versioned dependency (Spring itself excluded), it downloads the
   version list and the newest release of each major, and classifies each by **what it was built against**: the Boot
   version in its parent chain, the Spring version of its dependencies, `javax` vs `jakarta` references in the bytecode,
   and whether auto-configuration is registered via `spring.factories` or `AutoConfiguration.imports`.
3. It picks, per stage, the newest version built for that Boot line (never past `--target`). The versions are applied
   after that stage's recipes and before its build gate, including versions held in a `<properties>` entry.
4. The plan is written to `dependency-plan.json` in the report directory and shown in the report. Libraries with no
   Spring coupling are left alone; libraries with **no compatible release** are listed as a TODO (they need a new
   release of that library).

**OpenJPA and Hibernate in your libraries.** OpenJPA and Hibernate themselves are not planned here (the OpenJPA stage
removes OpenJPA, Boot manages Hibernate), but libraries that use them are:
* a version that still needs OpenJPA (an `org.apache.openjpa` dependency, OpenJPA API calls, or entities enhanced by the
  OpenJPA enhancer) is never chosen, because the migration removes OpenJPA;
* if OpenJPA only comes in through such a library, the OpenJPA -> Hibernate stage is added automatically, and the plan
  gets a **"Boot 2.7 + Hibernate"** column: the library's javax/Hibernate 5 build, applied in that stage so the switch
  is tested on its own commit before the Jakarta jump;
* the Hibernate version a library was built for is shown; a library built for Hibernate 5/6 that calls Hibernate APIs
  is flagged on the Boot 4 lines (Boot 4 ships Hibernate 7).

**When the new parent/BOM manages less.** A newer company parent often drops versions the old one managed (typically
javax-era tools). Before a parent/BOM is moved, the tool records the build's effective versions; afterwards it asks
Maven which versions are now missing and pins the previous one where it is used (in the dependency, or inside the
plugin's `<dependencies>`), listed as a TODO in the report.

**JAXB / JAX-WS code generation** moves to the Jakarta tool chain on Boot 3+: `org.jvnet.jaxb2_commons:jaxb2-basics*`
-> `org.jvnet.jaxb:jaxb-plugins(-runtime)` 4.x (`jaxb2-value-constructor` is part of it), `maven-jaxb2-plugin` ->
`org.jvnet.jaxb:jaxb-maven-plugin` 4.x, `jaxws-maven-plugin` -> `com.sun.xml.ws:jaxws-maven-plugin` 4.x; Java code
`org.jvnet.jaxb2_commons.*` -> `org.jvnet.jaxb.*` (`ToString2` -> `ToString`, ...); `.xjb` bindings to the Jakarta
namespace (version 3.0; the XJC extension namespace `http://java.sun.com/xml/ns/jaxb/xjc` stays), JAX-WS binding files to `https://jakarta.ee/xml/ns/jaxws`.

To choose yourself:
```bash
# detect and review (or answer per dependency with --interactive), then edit the "upgrades" per Boot line
python3 spring_migrate.py deps /path/to/project --build-args "-s settings.xml" --out plan.json [--interactive]
python3 spring_migrate.py migrate /path/to/project --build-args "-s settings.xml" --dependency-plan plan.json
```
`migrate --interactive` asks the same questions inline. `--internal-group com.acme` marks your groupIds in the report
(they are also guessed from the project's groupId and from non-public groups); `--no-dependency-detection` turns it off.

Plan entries look like this (Gradle projects: write it by hand; it is applied to `group:artifact:version` strings):
```json
{"target": "4.1", "dependencies": [
  {"coordinate": "com.acme.platform:acme-parent", "kind": "parent", "current": "1.4.0",
   "upgrades": {"3.5": "2.1.0", "4.0": "3.0.0", "4.1": "3.0.0"}}]}
```
(`"2.7-hibernate"` is the key for the OpenJPA stage.)

## OpenJPA -> Hibernate
OpenRewrite has no recipe for this, so the tool does it itself, as its own stage on Boot 2.7 (Hibernate 5.6, still
`javax`) *before* the Spring 6 jump, so a persistence regression is caught on its own commit. Run it alone with
`spring_migrate.py openjpa PATH` (no build, no git).

* `persistence.xml`: provider -> Hibernate; known `openjpa.*` properties mapped, the rest commented out with the reason
* pom/Gradle: OpenJPA dependencies, enhancer plugin, `openjpa.version` removed; `spring-boot-starter-data-jpa` added
* `PersistenceProviderImpl` / `OpenJpaVendorAdapter` / `OpenJpaDialect` -> Hibernate equivalents
* **API**: `OpenJPAPersistence.cast(...)` -> `unwrap(Session/Query/SessionFactory.class)`; `OpenJPAEntityManager`,
  `OpenJPAQuery`, `OpenJPAEntityManagerFactory` types; `getObjectId` (-> `PersistenceUnitUtil.getIdentifier`, which like
  OpenJPA works on detached entities), `evictAll`, `persistAll/removeAll/detachAll/refreshAll`
* **Annotations**: `@DataCache` -> `@Cacheable`, `@FetchGroup(s)` -> `@NamedEntityGraph(s)`, `@Persistent` -> `@Basic`,
  `@PersistentCollection/Map` -> `@ElementCollection`, `@ReadOnly` -> `@Column(updatable = false)`, OpenJPA `@Index` ->
  `@Table(indexes = ...)`, `@Dependent/@ElementDependent` -> `orphanRemoval`, `@Externalizer`/`@Factory` -> a generated
  `AttributeConverter`, `@ForeignKey` removed
* `openjpa.*` properties set in code or `application.properties` translated; the enhancer `-javaagent` removed from
  Dockerfiles/scripts
* **IDs**: implicit `@GeneratedValue` pinned to OpenJPA's own `OPENJPA_SEQUENCE_TABLE` via `@TableGenerator` with the
  `pooled-lo` optimizer, so new ids continue after the existing ones instead of restarting at 1
* Identifier naming kept as written (`PhysicalNamingStrategyStandardImpl`)

## Frontend builds and other non-Java plugins
OpenRewrite's Maven goal first builds the project up to `process-test-classes`, which also runs every plugin bound
to those phases. For frontend-maven-plugin that means `npm install`, which can fail where npm cannot reach its
registry. The recipes only need Java sources and dependencies, so:
* frontend-maven-plugin (npm/yarn/pnpm/bun, node install, webpack/gulp/...) is **skipped during the OpenRewrite step
  by default**; the build gates still build the frontend like your normal build (`--rewrite-frontend` to keep it in
  the OpenRewrite step too)
* `--skip-frontend` skips it in every build (baseline and gates as well) for environments where npm cannot work
* `--rewrite-args "-Dexec.skip -Ddocker.skip ..."` passes extra arguments to the OpenRewrite step only, for other
  plugins that fail outside your CI
* annotation processors still built for javax crash javac ("Fatal error compiling: NoClassDefFoundError:
  javax/persistence/Entity"): Querydsl `querydsl-apt:jpa` -> `jakarta` classifier, Hibernate's metamodel generator
  (`org.hibernate` -> `org.hibernate.orm`, `hibernate-processor` on Boot 4) and `javax.persistence-api` in
  `<annotationProcessorPaths>` / plugin dependencies are moved to the Jakarta versions Boot manages
* `--rewrite-no-fork` does not build before rewriting at all (`rewrite:runNoFork`); code that uses generated sources
  then gets less precise type information

When a build fails, the tool prints the lines that explain it (npm errors, compiler errors, failing tests) before
Maven's generic ones.

## Formatting and Checkstyle
Before each stage is committed, the tool runs **`spotless:apply`** (Gradle: `spotlessApply`) on the files that stage
changed (`-DspotlessFiles=...`), with the Spotless configuration your build already has, also when it comes from a
company parent. Code the migration did not touch is never reformatted. If the build has no Spotless, this is noticed
once and skipped. `--format-goal` runs another formatter instead (e.g. `com.spotify.fmt:fmt-maven-plugin:format`,
`net.revelc.code:impsort-maven-plugin:sort`), `--no-format` turns it off.

The OpenRewrite step runs without your quality gates (`spotless.check.skip`, `checkstyle.skip`, PMD, SpotBugs, fmt,
formatter, license, enforcer, JaCoCo): it only needs compiled code, and the tool's own edits are formatted just before
the stage commit. The build gate after each stage runs all of them. OpenRewrite is retried only after
repository/download problems, and a failed OpenRewrite step leaves a clean working tree to re-run from.

The tool's own code changes insert imports in sorted position within their package group. When a build gate fails
on Checkstyle, the summary shows the violations with file:line and rule.

## What still needs a human
The tool does every mechanical step; these are design decisions or need runtime knowledge. The scanner lists each
with file:line, the report repeats them, and a leftover reference fails the stage's build gate:
* `ZuulFilter` implementations and programmatic Hystrix (`HystrixCommand` subclasses, `HystrixRequestContext`)
* custom Ribbon `IRule`/`IPing`/`ServerList`, `OAuth2RestTemplate`, OpenJPA `FetchPlan`
* the OAuth2 `password` and `implicit` grants (removed from OAuth 2.1; the clients are migrated without them),
  JDBC client/token stores, `TokenEnhancer`s
* internal or third-party jars without a Jakarta version (detected after the last stage)
* behaviour only tests can show: Resilience4j does not time out synchronous calls the way Hystrix did; ID sequences
  and schema mapping against your real database; lazy loading; the end-of-report checklist

## Verified
`tests/e2e.sh` (Maven, JDK 21) migrates six Boot 2.7 sample projects to **Boot 4.1.3**. Each has tests that are
green on the original code and must stay green after every stage, and the script checks that every commit came from
the tool (no manual fixes):
* `examples/legacy-demo`: JPA, Security, MVC, validation, JUnit 4, `spring.factories`, Actuator httptrace, Docker/CI
* `examples/legacy-openjpa`: runs on real OpenJPA 3.2.2 (enhancer, `persistence.xml`, OpenJPA API and annotations,
  `@Externalizer`, `@ReadOnly`, positional parameters) -> Hibernate 5.6 -> 6 -> 7
* `examples/legacy-cloud`: Springfox, Hystrix (fallback test), Ribbon settings, Sleuth, legacy OAuth2 resource server
  (401 tests), HttpClient 4, trailing-slash tests (including security on `/path/`), stale Mockito pin, Jenkinsfile,
  `openjdk` images, Maven wrapper 3.6.0
* `examples/legacy-authserver`: `@EnableAuthorizationServer` with HMAC-signed JWTs; a token is fetched from
  `/oauth/token` before and after migration
* `examples/legacy-multimodule`: root POM with `<dependencyManagement>`, a JPA library module, a web app using it
  and a worker app with Hystrix (version managed in the root); tests cross module boundaries
* `examples/legacy-company`: company parent (`acme-parent` 1.4.0 on Boot 2.7) and an internal javax /
  `spring.factories` starter, served from a fake Artifactory (`tests/build-company-repo.sh` builds a file-based Maven
  repo plus `settings.xml`); the test checks the starter's filter is still auto-configured after every stage. Its
  `acme-persistence` 1.0.0 is built on OpenJPA: the run adds the OpenJPA stage, switches it to the Hibernate 5 build
  there, then to the Hibernate 6/7 builds

`tests/test_codemods.py` and `tests/test_web.py` (`python3 -m unittest discover -s tests`) unit-test the codemods the samples do not exercise
(Zuul, Ribbon rules, opaque/HMAC/SSO OAuth2, YAML editing, ID pinning modes, dialects, CI images, wrappers, ...).

Not verified: the Gradle path (implemented, untested; dependency detection is Maven-only), Gradle multi-project builds, Kotlin projects, non-H2 databases, PowerMock
(cannot run on JDK 21 at all, so no green baseline was possible here; the OpenRewrite recipe handles it).
Review the diff and run your integration tests against your real database before going live.
