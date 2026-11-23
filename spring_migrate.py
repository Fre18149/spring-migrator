#!/usr/bin/env python3
"""
spring-migrate - staged, local-only Spring Boot 2.7 -> 3.5 -> 4.0 migration tool.

Everything runs on YOUR machine. The tool never uploads code anywhere. The only
network traffic is Maven/Gradle downloading OpenRewrite recipes and dependencies
from whatever repositories your build is already configured to use (pass
--build-args "-s settings.xml" to use your corporate mirror).

Commands
  scan     read-only pre-flight analysis -> Markdown/JSON report
  migrate  staged migration on a new git branch, one commit per stage
  fix      run only the built-in post-fixers (javax->jakarta in resources,
           spring.factories -> AutoConfiguration.imports, CI/Docker Java bump)

Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "1.1.0"

IGNORE_DIRS = {".git", "target", "build", "node_modules", ".gradle", ".idea", ".mvn", "out", ".settings"}
MAX_FILE_BYTES = 2_000_000

# --------------------------------------------------------------------------- #
# Recipes (OpenRewrite). Names verified against docs.openrewrite.org.
# --------------------------------------------------------------------------- #
R_SPRING = "org.openrewrite.recipe:rewrite-spring"
R_MIGRATE = "org.openrewrite.recipe:rewrite-migrate-java"
R_TESTING = "org.openrewrite.recipe:rewrite-testing-frameworks"

RECIPE_2_7 = "org.openrewrite.java.spring.boot2.UpgradeSpringBoot_2_7"
RECIPE_3_5 = "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_5"
RECIPE_4_0 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
RECIPE_JUNIT5 = "org.openrewrite.java.testing.junit5.JUnit4to5Migration"
RECIPE_JAVA = {17: "org.openrewrite.java.migrate.UpgradeToJava17", 21: "org.openrewrite.java.migrate.UpgradeToJava21"}


# --------------------------------------------------------------------------- #
# File helpers
# --------------------------------------------------------------------------- #
def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size <= MAX_FILE_BYTES:
                    yield p
            except OSError:
                continue


def read(p: Path) -> str | None:
    try:
        return p.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def matches(p: Path, globs) -> bool:
    return any(fnmatch.fnmatch(p.name, g) for g in globs)


# --------------------------------------------------------------------------- #
# Scan rules
# --------------------------------------------------------------------------- #
BUILD = ("pom.xml", "build.gradle", "build.gradle.kts", "*.gradle", "*.gradle.kts", "libs.versions.toml")
SRC = ("*.java", "*.kt", "*.groovy")
CFG = ("*.properties", "*.yml", "*.yaml")


@dataclass
class Rule:
    id: str
    title: str
    severity: str          # blocker | high | medium | info
    kind: str              # auto (tool/OpenRewrite handles it) | manual | behavior
    globs: tuple
    pattern: str
    advice: str
    rx: re.Pattern = field(init=False, repr=False)

    def __post_init__(self):
        self.rx = re.compile(self.pattern)


EE = r"(?:servlet|persistence|validation|transaction(?!\.xa)|ws\.rs|xml\.bind|mail|jms|inject|json|websocket|el|enterprise|activation|interceptor|jws)"
RULES = [
    Rule("javax-imports", "javax.* Jakarta EE imports", "high", "auto", SRC,
         r"\bjavax\.(?:" + EE[3:-1] + r"|annotation\.(?:PostConstruct|PreDestroy|Resource|Priority|Generated|security))",
         "Renamed to jakarta.* by OpenRewrite; leftovers in strings/resources are caught by the post-fixer."),
    Rule("javax-deps", "javax.* artifacts in build files", "medium", "auto", BUILD,
         r"javax\.(?:servlet|persistence|validation|transaction|xml\.bind|mail|jms)|javax\.annotation-api|javax\.annotation:|jaxb-api",
         "Replaced by jakarta equivalents; verify any pinned versions manually."),
    Rule("websecurity-adapter", "WebSecurityConfigurerAdapter (removed in Spring Security 6)", "high", "auto", SRC,
         r"WebSecurityConfigurerAdapter",
         "Converted to SecurityFilterChain beans by OpenRewrite. Review security behaviour with tests."),
    Rule("security-dsl", "Security 6 DSL (antMatchers/mvcMatchers/authorizeRequests)", "medium", "auto", SRC,
         r"\.(?:antMatchers|mvcMatchers|regexMatchers|authorizeRequests)\(",
         "Moved to requestMatchers/authorizeHttpRequests. mvcMatchers vs antMatchers semantics can differ - test authz."),
    Rule("global-method-security", "@EnableGlobalMethodSecurity", "medium", "auto", SRC,
         r"@EnableGlobalMethodSecurity", "Replaced by @EnableMethodSecurity."),
    Rule("spring-factories-autoconfig", "EnableAutoConfiguration in spring.factories (ignored in Boot 3)", "high", "auto",
         ("spring.factories",), r"EnableAutoConfiguration",
         "Converted to META-INF/spring/...AutoConfiguration.imports by the post-fixer."),
    Rule("constructor-binding", "@ConstructorBinding on classes", "medium", "auto", SRC, r"@ConstructorBinding",
         "Only needed on multi-constructor classes in Boot 3; OpenRewrite removes redundant ones."),
    Rule("junit4", "JUnit 4 (junit-vintage removed from starter-test in Boot 3)", "high", "auto", SRC + BUILD,
         r"org\.junit\.(?:Test|Before|After|BeforeClass|AfterClass|Rule|Assert|runner)|@RunWith|junit:junit|junit-vintage|<artifactId>junit</artifactId>",
         "migrate auto-adds JUnit4to5Migration when this is detected; otherwise tests silently stop running."),
    Rule("mockbean", "@MockBean / @SpyBean (deprecated in Boot 3.4, removed path in 4.x)", "medium", "auto", SRC,
         r"@(?:MockBean|SpyBean)\b", "Replaced by @MockitoBean/@MockitoSpyBean by the Boot 4 recipe."),
    Rule("spring-props", "Renamed/removed Spring Boot properties", "medium", "auto", CFG,
         r"^\s*(?:spring\.redis\.|spring\.profiles\s*[=:]|spring\.profiles\.(?!active|include|group|default)|server\.max-http-header-size|"
         r"spring\.config\.use-legacy-processing|management\.metrics\.export\.|spring\.data\.cassandra\.(?!.*\.)|spring\.elasticsearch\.rest)",
         "OpenRewrite migrates .properties/.yml keys; verify with spring-boot-properties-migrator (see report)."),
    Rule("springfox", "Springfox / Swagger 2 (incompatible with Boot 3)", "blocker", "manual", BUILD + SRC,
         r"springfox|@EnableSwagger2",
         "Replace with springdoc-openapi-starter-webmvc-ui (OpenAPI 3) and migrate annotations - NOT automatic."),
    Rule("netflix-oss", "Netflix Ribbon/Hystrix/Zuul (removed from Spring Cloud)", "blocker", "manual", BUILD + SRC,
         r"netflix-(?:ribbon|hystrix|zuul)|starter-(?:hystrix|ribbon|zuul)|@EnableZuulProxy|@EnableHystrix|@EnableCircuitBreaker",
         "Replace with Spring Cloud LoadBalancer / Resilience4j / Spring Cloud Gateway."),
    Rule("sleuth", "Spring Cloud Sleuth / Brave (removed)", "high", "manual", BUILD + SRC,
         r"spring-cloud-(?:starter-)?sleuth|@NewSpan|brave\.",
         "Replace with Micrometer Tracing (+ bridge for Brave or OpenTelemetry)."),
    Rule("legacy-oauth2", "Legacy spring-security-oauth2 (Authorization/Resource server)", "blocker", "manual", BUILD + SRC,
         r"org\.springframework\.security\.oauth:|spring-security-oauth2-autoconfigure|@EnableAuthorizationServer|@EnableResourceServer",
         "Move to spring-security-oauth2-resource-server / spring-authorization-server. Needs design work."),
    Rule("powermock", "PowerMock (no Jakarta / modern JDK support)", "high", "manual", BUILD + SRC, r"powermock",
         "Rewrite tests with Mockito (mockStatic/mockConstruction) - NOT automatic."),
    Rule("httpclient4", "Apache HttpClient 4.x (Spring 6 requires HttpClient 5)", "medium", "manual", BUILD + SRC,
         r"<artifactId>httpclient</artifactId>|org\.apache\.http\.|HttpComponentsClientHttpRequestFactory",
         "Move to org.apache.httpcomponents.client5:httpclient5 and org.apache.hc.* APIs."),
    Rule("hibernate-gen-auto", "GenerationType.AUTO / bare @GeneratedValue (Hibernate 6 changes strategy)", "high", "behavior", SRC,
         r"GenerationType\.AUTO|@GeneratedValue(?!\s*\()",
         "Hibernate 6 maps AUTO to per-entity sequences (<entity>_seq) on sequence-capable DBs -> can break existing schemas. "
         "Pin the strategy explicitly or set hibernate.id.db_structure_naming_strategy=legacy."),
    Rule("hibernate-types", "Hibernate 5 type mappings (@TypeDef, @Type(type=...), hibernate-types)", "high", "manual", SRC + BUILD,
         r"@TypeDefs?\b|@Type\s*\(\s*type\s*=|hibernate-types",
         "Removed/changed in Hibernate 6; use @JdbcTypeCode / AttributeConverter / hypersistence-utils."),
    Rule("hibernate-dialect", "Explicit Hibernate dialect / legacy generator props", "medium", "manual", CFG,
         r"dialect\s*[=:]|new[_-]generator[_-]mappings|hibernate\.ejb\.",
         "Hibernate 6 auto-detects the dialect; old names (PostgreSQL95Dialect, MySQL5Dialect, Oracle12cDialect...) were removed."),
    Rule("httpstatus", "ResponseEntity.getStatusCode() now returns HttpStatusCode", "medium", "manual", SRC,
         r"getRawStatusCode\(\)|HttpStatus\s+\w+\s*=\s*[^;]*\.getStatusCode\(\)",
         "Use HttpStatusCode, or HttpStatus.resolve(code.value())."),
    Rule("webmvc-adapter", "WebMvcConfigurerAdapter (removed in Spring 6)", "high", "manual", SRC, r"WebMvcConfigurerAdapter",
         "Implement WebMvcConfigurer directly."),
    Rule("httptrace", "Actuator httptrace (renamed to httpexchanges)", "medium", "manual", SRC + CFG,
         r"httptrace|HttpTraceRepository", "Use HttpExchangeRepository / endpoint id 'httpexchanges'."),
    Rule("listenablefuture", "ListenableFuture (removed in Spring Framework 7 / Boot 4)", "medium", "manual", SRC,
         r"\bListenableFuture\b|ListenableFutureCallback", "Switch to CompletableFuture."),
]

RULES += [
    Rule("openjpa-deps", "OpenJPA dependencies / build plugins", "high", "auto", BUILD,
         r"org\.apache\.openjpa|openjpa-maven-plugin|<artifactId>openjpa[\w-]*</artifactId>|openjpa\.version",
         "Removed by the OpenJPA->Hibernate stage; spring-boot-starter-data-jpa (Hibernate) is added if missing."),
    Rule("openjpa-persistence-xml", "OpenJPA provider/properties in persistence.xml", "high", "auto", ("persistence.xml",),
         r"org\.apache\.openjpa|openjpa\.",
         "Provider switched to Hibernate; known openjpa.* properties mapped, the rest commented out with a TODO."),
    Rule("openjpa-spring-adapter", "OpenJPA persistence provider / Spring adapter wiring", "high", "auto", SRC + CFG + ("*.xml",),
         r"\bOpenJpa(?:VendorAdapter|Dialect)\b|\bPersistenceProviderImpl\b|org\.apache\.openjpa\.persistence\.PersistenceProviderImpl",
         "PersistenceProviderImpl -> HibernatePersistenceProvider, OpenJpa* adapters -> Hibernate equivalents."),
    Rule("openjpa-api", "OpenJPA-specific API / annotations in code", "high", "manual", SRC,
         r"org\.apache\.openjpa|\bOpenJPA\w+",
         "No mechanical equivalent - see the symbol inventory below for the closest JPA/Hibernate replacement."),
    Rule("openjpa-props-code", "openjpa.* properties set from code or application config", "medium", "manual", SRC + CFG,
         r"[\"']openjpa\.|^\s*openjpa\.",
         "Map to hibernate.* / javax.persistence.* equivalents by hand (see report); Hibernate ignores unknown keys silently."),
    Rule("openjpa-agent", "OpenJPA enhancer / javaagent in scripts or images", "medium", "manual",
         ("Dockerfile", "Dockerfile.*", "*.sh", "*.cmd", "*.bat", "*.service", "*.env", "Jenkinsfile"),
         r"javaagent[^\n]*openjpa|openjpa[-\w.]*\.jar|PCEnhancer", "Hibernate needs no enhancement agent - remove it."),
    Rule("jpql-jdbc-params", "JDBC-style '?' parameters in JPQL (rejected by Hibernate 5.3+)", "high", "manual", SRC,
         r"(?:createQuery|createNamedQuery|@NamedQuery)\b[^\n]*\"[^\"]*\?(?!\d)",
         "Use positional ?1 / named :param parameters."),
]

CHECKLIST = [
    "Trailing slashes: Spring 6 no longer matches /users/ to @GetMapping(\"/users\"). Check clients, gateways and tests.",
    "Hibernate 6: `ORDER BY`/HQL parsing is stricter; native-query result mapping and sequence naming changed. Run integration tests against your real DB engine (Testcontainers).",
    "Run once with `spring-boot-properties-migrator` on the classpath (runtime scope) to surface leftover property renames, then remove it.",
    "Actuator/Micrometer: metric and tag names changed in places; verify dashboards and alerts.",
    "Third-party libraries not managed by Boot (internal starters, Lombok, MapStruct, Querydsl, Flyway/Liquibase plugins, OpenAPI generators): confirm Jakarta-compatible versions.",
    "Container/runtime: base images, JAVA_HOME in CI, app servers (Tomcat 10.1+/Jetty 12+ required for Jakarta EE 10).",
    "Spring Boot 4.0 modularised the starters and autoconfiguration; if you hit missing beans after the 4.0 stage, check the official 4.0 migration guide.",
]


@dataclass
class Finding:
    rule: Rule
    hits: list  # (relpath, line_no, snippet)


def detect_build(root: Path) -> dict:
    info = {"tool": None, "boot_version": None, "java_version": None, "modules": 0, "wrapper": None, "notes": []}
    poms = [p for p in walk(root) if p.name == "pom.xml"]
    gradles = [p for p in walk(root) if p.name in ("build.gradle", "build.gradle.kts")]
    info["modules"] = len(poms) or len(gradles)
    if poms:
        info["tool"] = "maven"
    elif gradles:
        info["tool"] = "gradle"
    for p in poms + gradles:
        t = read(p) or ""
        if not info["boot_version"]:
            m = re.search(r"<artifactId>spring-boot-starter-parent</artifactId>\s*<version>([^<]+)</version>", t) \
                or re.search(r"<artifactId>spring-boot-dependencies</artifactId>\s*<version>([^<]+)</version>", t) \
                or re.search(r"org\.springframework\.boot['\"]\)?\s*version\s*\(?['\"]([\d.]+[\w.\-]*)['\"]", t) \
                or re.search(r"springBootVersion\s*=\s*['\"]([^'\"]+)['\"]", t)
            if m:
                info["boot_version"] = m.group(1)
        if not info["java_version"]:
            m = re.search(r"<java\.version>([^<]+)</java\.version>", t) \
                or re.search(r"<maven\.compiler\.(?:release|source)>([^<]+)</maven\.compiler\.(?:release|source)>", t) \
                or re.search(r"sourceCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?([\d._]+)", t) \
                or re.search(r"JavaLanguageVersion\.of\((\d+)\)", t)
            if m:
                info["java_version"] = m.group(1).replace("_", ".")
    for w in ("mvnw", "gradlew"):
        if (root / w).exists():
            info["wrapper"] = w
    mw = root / ".mvn/wrapper/maven-wrapper.properties"
    if mw.exists():
        m = re.search(r"apache-maven-([\d.]+)-bin", read(mw) or "")
        if m:
            v = tuple(int(x) for x in m.group(1).split(".")[:3] if x.isdigit())
            if v < (3, 6, 3):
                info["notes"].append(f"Maven wrapper is {m.group(1)}; Spring Boot 3.x needs Maven 3.6.3+.")
    gw = root / "gradle/wrapper/gradle-wrapper.properties"
    if gw.exists():
        m = re.search(r"gradle-([\d.]+)-", read(gw) or "")
        if m:
            info["notes"].append(f"Gradle wrapper is {m.group(1)}; check Boot 3.5/4.0 minimum Gradle versions in the release notes.")
    return info


def scan(root: Path) -> dict:
    findings: dict[str, Finding] = {r.id: Finding(r, []) for r in RULES}
    files = list(walk(root))
    for p in files:
        applicable = [r for r in RULES if matches(p, r.globs)]
        if not applicable:
            continue
        text = read(p)
        if text is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for r in applicable:
                if r.rx.search(line):
                    findings[r.id].hits.append((rel(p, root), i, line.strip()[:140]))
    info = detect_build(root)
    return {"info": info, "findings": findings, "files": len(files), "root": root}


SEV_ORDER = {"blocker": 0, "high": 1, "medium": 2, "info": 3}
KIND_LABEL = {"auto": "automatic", "manual": "MANUAL", "behavior": "BEHAVIOUR CHANGE"}


def render_scan(result: dict, title="Spring migration pre-flight scan") -> str:
    info, fnd = result["info"], result["findings"]
    out = [f"# {title}", ""]
    out += [f"- Build tool: **{info['tool'] or 'unknown'}** ({info['modules']} module(s))",
            f"- Spring Boot: **{info['boot_version'] or 'not detected'}**",
            f"- Java (source level): **{info['java_version'] or 'not detected'}**",
            f"- Files scanned: {result['files']}", ""]
    for n in info["notes"]:
        out.append(f"> {n}")
    hits = [f for f in fnd.values() if f.hits]
    hits.sort(key=lambda f: (SEV_ORDER[f.rule.severity], f.rule.id))
    manual = [f for f in hits if f.rule.kind != "auto"]
    auto = [f for f in hits if f.rule.kind == "auto"]
    out += ["", f"## Summary: {len(manual)} item(s) need human work, {len(auto)} handled automatically", ""]

    def block(f: Finding):
        r = f.rule
        files = sorted({h[0] for h in f.hits})
        s = [f"### [{r.severity.upper()}] {r.title} - {KIND_LABEL[r.kind]}",
             f"{len(f.hits)} occurrence(s) in {len(files)} file(s). {r.advice}", ""]
        for path, ln, snip in f.hits[:8]:
            s.append(f"- `{path}:{ln}` `{snip}`")
        if len(f.hits) > 8:
            s.append(f"- ... and {len(f.hits) - 8} more")
        s.append("")
        return s

    if manual:
        out += ["## Needs human attention", ""]
        for f in manual:
            out += block(f)
    if auto:
        out += ["## Handled automatically by `migrate`", ""]
        for f in auto:
            out += block(f)
    if openjpa_present(result):
        out += openjpa_section(result["root"])
    out += ["## Always review (cannot be detected statically)", ""]
    out += [f"- {c}" for c in CHECKLIST]
    return "\n".join(out) + "\n"


def scan_counts(result: dict) -> dict:
    return {k: len(v.hits) for k, v in result["findings"].items() if v.hits}


# --------------------------------------------------------------------------- #
# Post-fixers (pure Python, idempotent)
# --------------------------------------------------------------------------- #
JAVAX_RX = re.compile(
    r"\bjavax\.(?P<pkg>servlet|persistence|validation|transaction(?!\.xa)|ws\.rs|xml\.bind|mail|jms|inject|json|websocket|el|"
    r"enterprise|activation|interceptor|jws|annotation\.(?:PostConstruct|PreDestroy|Resource|Resources|Priority|Generated|ManagedBean|security))"
)
JAVAX_FILES = ("*.java", "*.kt", "*.xml", "*.properties", "*.yml", "*.yaml", "*.factories", "*.imports", "*.tld", "*.jsp", "*.xhtml", "*.sql")


def fix_javax_residuals(root: Path) -> list[str]:
    changes = []
    for p in walk(root):
        in_services = "META-INF/services" in p.as_posix()
        if not (matches(p, JAVAX_FILES) or in_services):
            continue
        if p.name == "pom.xml":
            continue  # dependency coordinates are handled by OpenRewrite, versions need human eyes
        text = read(p)
        if not text or "javax." not in text:
            continue
        new, n = JAVAX_RX.subn(lambda m: "jakarta." + m.group("pkg"), text)
        if n:
            p.write_text(new, encoding="utf-8")
            changes.append(f"{rel(p, root)}: {n} javax->jakarta replacement(s)")
    return changes


def fix_spring_factories(root: Path) -> list[str]:
    changes = []
    key_rx = re.compile(r"^\s*org\.springframework\.boot\.autoconfigure\.EnableAutoConfiguration\s*[=:]\s*(.*)$")
    for f in walk(root):
        if f.name != "spring.factories" or f.parent.name != "META-INF":
            continue
        lines = (read(f) or "").splitlines()
        kept, classes, i = [], [], 0
        while i < len(lines):
            m = key_rx.match(lines[i])
            if not m:
                kept.append(lines[i]); i += 1; continue
            value = m.group(1)
            while value.rstrip().endswith("\\") and i + 1 < len(lines):
                value = value.rstrip()[:-1] + lines[i + 1].strip()
                i += 1
            i += 1
            classes += [c.strip() for c in value.split(",") if c.strip()]
        if not classes:
            continue
        imports = f.parent / "spring" / "org.springframework.boot.autoconfigure.AutoConfiguration.imports"
        imports.parent.mkdir(parents=True, exist_ok=True)
        existing = [l.strip() for l in (read(imports) or "").splitlines() if l.strip()]
        merged = existing + [c for c in classes if c not in existing]
        imports.write_text("\n".join(merged) + "\n", encoding="utf-8")
        remaining = [l for l in kept if l.strip() and not l.strip().startswith("#")]
        if remaining:
            f.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        else:
            f.unlink()
        changes.append(f"{rel(f, root)}: moved {len(classes)} auto-configuration(s) to {rel(imports, root)}")
    return changes


def fix_ci_java(root: Path, java: int) -> tuple[list[str], list[str]]:
    changes, todo = [], []
    old = r"(?:8|1\.8|11)"
    for p in walk(root):
        path = p.as_posix()
        text = read(p)
        if text is None:
            continue
        new = text
        if ".github/workflows/" in path and p.suffix in (".yml", ".yaml"):
            new = re.sub(rf"(java-version:\s*)(['\"]?){old}\2(\s*(?:#.*)?)$", rf"\g<1>\g<2>{java}\g<2>\g<3>", new, flags=re.M)
        elif p.name == ".java-version":
            new = re.sub(rf"^\s*{old}(\.\d+)*\s*$", f"{java}\n", new)
        elif p.name == "Dockerfile" or p.name.startswith("Dockerfile.") or p.suffix == ".dockerfile":
            out = []
            for line in new.splitlines():
                if line.lstrip().upper().startswith("FROM"):
                    if "openjdk" in line.lower():
                        todo.append(f"{rel(p, root)}: `{line.strip()}` uses the deprecated openjdk image - switch to eclipse-temurin:{java}")
                    else:
                        line = re.sub(r"(?i)((?:jdk|jre|temurin|corretto|zulu|liberica|semeru|sapmachine)[-:]?)(8|11)(?!\d|\.\d)",
                                      rf"\g<1>{java}", line)
                out.append(line)
            new = "\n".join(out) + ("\n" if text.endswith("\n") else "")
        elif p.name in (".sdkmanrc", ".tool-versions", "Jenkinsfile", "azure-pipelines.yml", ".gitlab-ci.yml", "bitbucket-pipelines.yml", ".travis.yml"):
            if re.search(r"(?i)(java|jdk|openjdk)\W{0,12}(8|11)\b|temurin-(8|11)", text):
                todo.append(f"{rel(p, root)}: pins an old JDK - update to {java} by hand")
        if new != text:
            p.write_text(new, encoding="utf-8")
            changes.append(f"{rel(p, root)}: Java version -> {java}")
    return changes, todo


def run_fixers(root: Path, java: int) -> tuple[list[str], list[str]]:
    changes = fix_spring_factories(root) + fix_javax_residuals(root)
    ci, todo = fix_ci_java(root, java)
    return changes + ci, todo


# --------------------------------------------------------------------------- #
# OpenJPA -> Hibernate
# --------------------------------------------------------------------------- #
OPENJPA_STAGE = "1b-openjpa-hibernate"
HIBERNATE_PROVIDER = "org.hibernate.jpa.HibernatePersistenceProvider"

OPENJPA_SUGGEST = {
    "PersistenceProviderImpl": "converted automatically -> org.hibernate.jpa.HibernatePersistenceProvider",
    "OpenJPAEntityManager": "EntityManager.unwrap(org.hibernate.Session.class)",
    "OpenJPAEntityManagerSPI": "EntityManager.unwrap(org.hibernate.Session.class)",
    "OpenJPAEntityManagerFactory": "EntityManagerFactory.unwrap(org.hibernate.SessionFactory.class)",
    "OpenJPAEntityManagerFactorySPI": "EntityManagerFactory.unwrap(org.hibernate.SessionFactory.class)",
    "OpenJPAPersistence": "standard Persistence / EntityManager.unwrap(...)",
    "OpenJPAQuery": "Query.unwrap(org.hibernate.query.Query.class)",
    "FetchPlan": "JPA EntityGraph or JOIN FETCH",
    "DataCache": "@Cacheable + explicit Hibernate 2nd-level cache provider",
    "QueryResultCache": "Hibernate query cache (hint org.hibernate.cacheable)",
    "ReadOnly": "@org.hibernate.annotations.Immutable (semantics differ)",
    "Persistent": "standard @Basic / @ElementCollection / @OneToMany ...",
    "PersistentCollection": "@ElementCollection / @OneToMany",
    "PersistentMap": "@ElementCollection + @MapKeyColumn",
    "ElementDependent": "cascade = REMOVE / orphanRemoval = true",
    "Dependent": "cascade = REMOVE / orphanRemoval = true",
    "KeyDependent": "cascade = REMOVE on map values",
    "Externalizer": "AttributeConverter (@Convert)",
    "Factory": "AttributeConverter (@Convert)",
    "ExternalValues": "AttributeConverter / enum mapping",
    "Index": "@Table(indexes = @javax.persistence.Index(...))",
    "ForeignKey": "@JoinColumn(foreignKey = @javax.persistence.ForeignKey(...))",
    "Strategy": "no direct equivalent (custom UserType / AttributeConverter)",
    "ContainerTable": "@CollectionTable",
    "ElementJoinColumn": "@JoinColumn on the collection mapping",
    "LRS": "no equivalent; use paging queries instead of large-result-set collections",
    "FetchAttribute": "EntityGraph attribute nodes", "FetchGroup": "EntityGraph", "LoadFetchGroup": "EntityGraph",
    "Type": "@org.hibernate.annotations.Type / AttributeConverter",
    "InstanceCallbacks": "JPA @PostLoad/@PrePersist callbacks",
}

OPENJPA_CHECKLIST = [
    "**ID generation (data risk):** OpenJPA's AUTO/TABLE strategies use its own `OPENJPA_SEQUENCE_TABLE`; Hibernate uses `hibernate_sequence` "
    "(or per-entity sequences after the Hibernate 6 stage). Starting from 1 will collide with existing rows. For every entity listed "
    "in the ID table, define an explicit generator or initialise the new sequence/table to max(id)+1 (e.g. with Flyway/Liquibase) before go-live.",
    "**Schema mapping:** OpenJPA's default table/column naming and type mapping differ from Hibernate's (booleans, `byte[]`/LOBs, enums, "
    "`Date` vs `Timestamp`, `char`). Run the migrated app against a copy of the production schema with `spring.jpa.hibernate.ddl-auto=validate` "
    "(or `hibernate.hbm2ddl.auto=validate`) and fix every mismatch.",
    "**Lazy loading / detachment:** OpenJPA's enhancement keeps entities usable after the transaction; Hibernate proxies throw "
    "`LazyInitializationException` outside a session, and `@Basic(fetch=LAZY)` is ignored without bytecode enhancement. Check service/DTO boundaries.",
    "**Collections:** Hibernate `List` mappings without `@OrderColumn` behave as bags (duplicates with JOIN FETCH, no stable order). Add `ORDER BY`/`@OrderBy`.",
    "**Queries:** Hibernate is stricter about JPQL (parameter style, implicit joins, `SELECT` aliasing). Run every named/dynamic query, "
    "and any `FetchPlan`/query-hint usage needs a Hibernate rewrite.",
    "**Caching:** OpenJPA DataCache/QueryCache settings were dropped. Re-introduce caching deliberately via a Hibernate cache provider if you relied on it.",
    "**Transactions & locking:** verify optimistic-lock (`@Version`) columns and pessimistic lock timeouts; flush behaviour before queries differs slightly.",
    "**Test against your real database engine** (Testcontainers), not only H2.",
]

_PROP_RX = re.compile(r'^(?P<ind>[ \t]*)<property\s+name\s*=\s*"(?P<n>openjpa\.[^"]+)"\s+value\s*=\s*"(?P<v>[^"]*)"\s*/>[ \t]*$', re.M)
_STD = {"openjpa.ConnectionURL": "javax.persistence.jdbc.url", "openjpa.ConnectionDriverName": "javax.persistence.jdbc.driver",
        "openjpa.ConnectionUserName": "javax.persistence.jdbc.user", "openjpa.ConnectionPassword": "javax.persistence.jdbc.password",
        "openjpa.jdbc.Schema": "hibernate.default_schema"}


def map_openjpa_property(name: str, value: str):
    """-> (list of (key, value) to emit, note or None)."""
    if name in _STD:
        return [(_STD[name], value)], None
    if name == "openjpa.jdbc.SynchronizeMappings":
        if value.startswith("buildSchema"):
            return [("hibernate.hbm2ddl.auto", "update")], "SynchronizeMappings -> hbm2ddl.auto=update; REVIEW: never use 'update' against production"
        if value.startswith("validate"):
            return [("hibernate.hbm2ddl.auto", "validate")], None
        return [], "SynchronizeMappings has no mapping for this value"
    if name == "openjpa.jdbc.DBDictionary":
        return [], "dropped: Hibernate auto-detects the dialect (set hibernate.dialect only if detection is wrong)"
    if name in ("openjpa.DataCache", "openjpa.QueryCache", "openjpa.RemoteCommitProvider", "openjpa.jdbc.QuerySQLCache"):
        return [], "dropped: second-level caching must be configured explicitly in Hibernate"
    if name == "openjpa.Log":
        return [], "dropped: use logging.level.org.hibernate.SQL=DEBUG (and show_sql) instead"
    if name in ("openjpa.RuntimeUnenhancedClasses", "openjpa.DynamicEnhancementAgent", "openjpa.Multithreaded",
                "openjpa.ConnectionRetainMode", "openjpa.TransactionMode", "openjpa.jdbc.TransactionIsolation"):
        return [], "dropped: not applicable to Hibernate"
    return [], "no automatic mapping - translate by hand"


def _first_tag(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
    return m.group(1).strip() if m else ""


def _strip_blocks(text: str, tag: str, should_remove) -> tuple[str, int]:
    rx = re.compile(rf"[ \t]*<{tag}>(?:(?!<{tag}>).)*?</{tag}>[ \t]*\r?\n?", re.S)
    n = 0

    def sub(m):
        nonlocal n
        if should_remove(m.group(0)):
            n += 1
            return ""
        return m.group(0)
    return rx.sub(sub, text), n


def _insert_dependency(text: str, dep_xml: str) -> str:
    spans = [m.span() for m in re.finditer(r"<(dependencyManagement|build|profiles|reporting)>.*?</\1>", text, re.S)]
    cands = [m for m in re.finditer(r"</dependencies>", text) if not any(a <= m.start() < b for a, b in spans)]
    if cands:
        pos = cands[-1].start()
        line_start = text.rfind("\n", 0, pos) + 1
        return text[:line_start] + dep_xml + text[line_start:]
    block = "  <dependencies>\n" + dep_xml + "  </dependencies>\n"
    m = re.search(r"[ \t]*<build>", text) or re.search(r"</project>", text)
    return text[:m.start()] + block + text[m.start():]


STARTER_JPA = ("    <dependency>\n      <groupId>org.springframework.boot</groupId>\n"
               "      <artifactId>spring-boot-starter-data-jpa</artifactId>\n    </dependency>\n")


def openjpa_persistence_xml(root: Path) -> tuple[list[str], list[str]]:
    changes, todo = [], []
    for f in walk(root):
        if f.name != "persistence.xml":
            continue
        text = read(f) or ""
        if "openjpa" not in text.lower():
            continue
        new = re.sub(r"<provider>\s*org\.apache\.openjpa[^<]*</provider>", f"<provider>{HIBERNATE_PROVIDER}</provider>", text)
        mapped = 0

        def sub(m):
            nonlocal mapped
            keep, note = map_openjpa_property(m.group("n"), m.group("v"))
            ind = m.group("ind")
            out = [f'{ind}<property name="{k}" value="{v}"/>' for k, v in keep]
            if note:
                safe = f"{m.group('n')}={m.group('v')}".replace("--", "- -")
                out.append(f"{ind}<!-- TODO(spring-migrate): OpenJPA {safe} : {note} -->")
                todo.append(f"{rel(f, root)}: {m.group('n')}={m.group('v')}: {note}")
            mapped += 1
            return "\n".join(out)
        new = _PROP_RX.sub(sub, new)
        if new != text:
            f.write_text(new, encoding="utf-8")
            changes.append(f"{rel(f, root)}: provider -> Hibernate, {mapped} openjpa.* propert(ies) translated/commented")
        if re.search(r'name\s*=\s*"openjpa\.', new):
            todo.append(f"{rel(f, root)}: contains openjpa.* properties in a non-standard layout - translate by hand")
    return changes, todo


def openjpa_build_files(root: Path) -> tuple[list[str], list[str]]:
    changes, todo = [], []
    for f in walk(root):
        text = read(f)
        if not text or "openjpa" not in text.lower():
            continue
        if f.name == "pom.xml":
            new, nd = _strip_blocks(text, "dependency", lambda b: "org.apache.openjpa" in b.split("<exclusions>")[0] and
                                    "<groupId>org.apache.openjpa</groupId>" in b.split("<exclusions>")[0])
            new, npl = _strip_blocks(new, "plugin", lambda b: "openjpa" in _first_tag(b, "artifactId").lower())
            new = re.sub(r"[ \t]*<(?:openjpa\.version|openjpa-version)>[^<]*</[^>]+>[ \t]*\r?\n?", "", new)
            if not (nd or npl):
                if "openjpa" in new.lower():
                    todo.append(f"{rel(f, root)}: mentions openjpa outside a dependency/plugin block - review by hand")
                continue
            if not re.search(r"spring-boot-starter-data-jpa|<artifactId>hibernate-core</artifactId>", new):
                new = _insert_dependency(new, STARTER_JPA)
                changes.append(f"{rel(f, root)}: added spring-boot-starter-data-jpa (Hibernate)")
            f.write_text(new, encoding="utf-8")
            changes.append(f"{rel(f, root)}: removed {nd} OpenJPA dependenc(ies), {npl} plugin(s)")
        elif f.name in ("build.gradle", "build.gradle.kts") or f.suffix in (".gradle", ".kts"):
            lines = text.splitlines()
            kept = [l for l in lines if "openjpa" not in l.lower()]
            if len(kept) != len(lines):
                body = "\n".join(kept) + "\n"
                if "spring-boot-starter-data-jpa" not in body and "hibernate-core" not in body:
                    q = '"' if f.suffix == ".kts" else "'"
                    body = re.sub(r"(dependencies\s*\{[ \t]*\n)", rf"\1    implementation({q}org.springframework.boot:spring-boot-starter-data-jpa{q})\n"
                                  if f.suffix == ".kts" else rf"\1    implementation {q}org.springframework.boot:spring-boot-starter-data-jpa{q}\n", body, count=1)
                    changes.append(f"{rel(f, root)}: added spring-boot-starter-data-jpa")
                f.write_text(body, encoding="utf-8")
                changes.append(f"{rel(f, root)}: removed {len(lines) - len(kept)} OpenJPA line(s)")
    # hibernate exclusions on the JPA starter
    for f in walk(root):
        if f.name != "pom.xml":
            continue
        text = read(f) or ""
        if "data-jpa" not in text or "<exclusion>" not in text:
            continue

        def clean(m):
            b = m.group(0)
            if "data-jpa" not in b:
                return b
            b2, k = _strip_blocks(b, "exclusion", lambda e: re.search(r"<groupId>org\.hibernate(?:\.javax\.persistence)?</groupId>", e) is not None)
            if k:
                b2 = re.sub(r"[ \t]*<exclusions>\s*</exclusions>[ \t]*\r?\n?", "", b2)
            return b2
        new = re.sub(r"<dependency>(?:(?!<dependency>).)*?</dependency>", clean, text, flags=re.S)
        if new != text:
            f.write_text(new, encoding="utf-8")
            changes.append(f"{rel(f, root)}: removed Hibernate exclusions from the JPA starter")
    return changes, todo


def openjpa_spring_adapters(root: Path) -> list[str]:
    changes = []
    for f in walk(root):
        if f.suffix not in (".java", ".kt", ".xml", ".properties", ".yml", ".yaml"):
            continue
        text = read(f)
        if not text or not re.search(r"OpenJpa|PersistenceProviderImpl", text):
            continue
        new = re.sub(r"\bOpenJpaVendorAdapter\b", "HibernateJpaVendorAdapter", text)
        new = re.sub(r"\bOpenJpaDialect\b", "HibernateJpaDialect", new)
        new = new.replace("org.apache.openjpa.persistence.PersistenceProviderImpl", HIBERNATE_PROVIDER)
        if "org.hibernate.jpa.HibernatePersistenceProvider" in new and f.suffix in (".java", ".kt"):
            # simple name left over after the import was rewritten above
            new = re.sub(r"\bPersistenceProviderImpl\b", "HibernatePersistenceProvider", new)
        if new != text:
            f.write_text(new, encoding="utf-8")
            changes.append(f"{rel(f, root)}: OpenJPA provider/adapter -> Hibernate equivalent")
    return changes


def openjpa_naming_defaults(root: Path) -> list[str]:
    """Boot's JPA auto-config applies snake_case naming; OpenJPA-created schemas use the identifiers as written."""
    changes = []
    for f in walk(root):
        if f.name == "application.properties" and "src/main/resources" in f.as_posix():
            text = read(f) or ""
            if "naming.physical-strategy" in text:
                continue
            add = ("\n# --- added by spring-migrate (OpenJPA -> Hibernate): keep identifiers as written so the existing schema still matches.\n"
                   "# Only affects Spring Boot's JPA auto-configuration. Remove if you want Boot's snake_case defaults.\n"
                   "spring.jpa.hibernate.naming.physical-strategy=org.hibernate.boot.model.naming.PhysicalNamingStrategyStandardImpl\n"
                   "spring.jpa.hibernate.naming.implicit-strategy=org.hibernate.boot.model.naming.ImplicitNamingStrategyJpaCompliantImpl\n")
            f.write_text(text.rstrip("\n") + "\n" + add, encoding="utf-8")
            changes.append(f"{rel(f, root)}: added identifier-naming defaults")
    return changes


def migrate_openjpa(root: Path) -> tuple[list[str], list[str]]:
    c1, t1 = openjpa_persistence_xml(root)
    c2, t2 = openjpa_build_files(root)
    c3 = openjpa_spring_adapters(root)
    c4 = openjpa_naming_defaults(root)
    return c1 + c2 + c3 + c4, t1 + t2


def openjpa_present(result: dict) -> bool:
    f = result["findings"]
    return any(f[k].hits for k in ("openjpa-deps", "openjpa-persistence-xml", "openjpa-spring-adapter", "openjpa-api"))


def openjpa_inventory(root: Path) -> dict:
    sym: dict[str, list] = {}
    imp = re.compile(r"import\s+(?:static\s+)?org\.apache\.openjpa[\w.]*?\.(\w+|\*)\s*;")
    word = re.compile(r"\b(OpenJPA\w+)\b")
    for p in walk(root):
        if p.suffix not in (".java", ".kt", ".groovy"):
            continue
        text = read(p) or ""
        for i, line in enumerate(text.splitlines(), 1):
            for m in list(imp.finditer(line)) + ([] if line.lstrip().startswith("import") else list(word.finditer(line))):
                sym.setdefault(m.group(1), []).append((rel(p, root), i))
    return sym


def idgen_inventory(root: Path) -> list[tuple]:
    rows = []
    cls_rx = re.compile(r"\bclass\s+(\w+)")
    gv_rx = re.compile(r"@GeneratedValue(?:\s*\(([^)]*)\))?")
    for p in walk(root):
        if p.suffix != ".java":
            continue
        text = read(p) or ""
        if "@GeneratedValue" not in text:
            continue
        cm = cls_rx.search(text)
        for i, line in enumerate(text.splitlines(), 1):
            m = gv_rx.search(line)
            if m:
                args = m.group(1) or ""
                st = re.search(r"GenerationType\.(\w+)|strategy\s*=\s*(\w+)", args)
                gen = re.search(r'generator\s*=\s*"([^"]+)"', args)
                rows.append((cm.group(1) if cm else p.stem, (st.group(1) or st.group(2)) if st else "AUTO (default)",
                             gen.group(1) if gen else "-", f"{rel(p, root)}:{i}"))
    return rows


def openjpa_section(root: Path) -> list[str]:
    out = ["## OpenJPA -> Hibernate", ""]
    inv = openjpa_inventory(root)
    if inv:
        out += ["### OpenJPA API still referenced in code (manual)", "", "| Symbol | Uses | Closest replacement |", "|---|---|---|"]
        for name, hits in sorted(inv.items(), key=lambda kv: -len(kv[1])):
            out.append(f"| `{name}` | {len(hits)} (e.g. `{hits[0][0]}:{hits[0][1]}`) | {OPENJPA_SUGGEST.get(name, 'no mechanical equivalent - rewrite with JPA / Hibernate API')} |")
        out.append("")
    ids = idgen_inventory(root)
    if ids:
        out += ["### Entities with generated IDs (check sequences/tables before go-live)", "", "| Entity | Strategy | Generator | Location |", "|---|---|---|---|"]
        out += [f"| {a} | {b} | {c} | `{d}` |" for a, b, c, d in ids[:60]]
        out.append("")
    out += ["### What to verify"] + [""] + [f"- {c}" for c in OPENJPA_CHECKLIST] + [""]
    return out


# --------------------------------------------------------------------------- #
# Process / git helpers
# --------------------------------------------------------------------------- #
class MigrationError(Exception):
    pass


def say(msg: str):
    print(msg, flush=True)


def build_env(java_home: str | None) -> dict:
    env = os.environ.copy()
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = str(Path(java_home) / "bin") + os.pathsep + env["PATH"]
    return env


def run(cmd: list[str], cwd: Path, log: Path, env: dict) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as fh:
        fh.write(f"\n$ {' '.join(shlex.quote(c) for c in cmd)}\n".encode())
        fh.flush()
        try:
            return subprocess.call(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT)
        except FileNotFoundError as e:
            raise MigrationError(f"Command not found: {cmd[0]} ({e})")


def show_errors(log: Path, limit: int = 8):
    """Print the first few ERROR lines of a build log so the user sees the cause immediately."""
    try:
        lines = [l for l in log.read_text(errors="replace").splitlines() if "ERROR" in l or "FAILURE" in l or "What went wrong" in l]
    except OSError:
        return
    for l in lines[:limit]:
        say(f"    {l.strip()[:200]}")
    say(f"    (full log: {log})")


DOWNLOAD_FAIL_RX = re.compile(r"Failed to download|Unable to download|Could not (?:resolve|transfer)|Could not GET|status code: ?(?:401|403|429)|429 Too Many|Connection (?:reset|refused)", re.I)


def rewrite_had_download_failures(log: Path) -> list[str]:
    """OpenRewrite exits 0 even when it could not fetch artifacts and therefore skipped edits. Treat that as failure."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return []
    # only look at the output of the most recent invocation
    last = text.rsplit("\n$ ", 1)[-1]
    return [l.strip()[:200] for l in last.splitlines() if DOWNLOAD_FAIL_RX.search(l)][:5]


def git(root: Path, *args: str, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise MigrationError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def java_major(env: dict) -> int | None:
    try:
        r = subprocess.run(["java", "-version"], capture_output=True, text=True, env=env)
    except FileNotFoundError:
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', r.stderr)
    if not m:
        return None
    return int(m.group(2)) if m.group(1) == "1" else int(m.group(1))


# --------------------------------------------------------------------------- #
# Build-tool adapters
# --------------------------------------------------------------------------- #
class Builder:
    def __init__(self, root: Path, tool: str, env: dict, log_dir: Path, extra: list[str], plugin_version: str | None):
        self.root, self.tool, self.env, self.log_dir, self.extra = root, tool, env, log_dir, extra
        self.plugin_version = plugin_version
        if tool == "maven":
            self.exe = str(root / "mvnw") if (root / "mvnw").exists() else "mvn"
        else:
            self.exe = str(root / "gradlew") if (root / "gradlew").exists() else "gradle"

    def rewrite(self, stage: str, recipes: list[str], artifacts: list[str], dry: bool) -> int:
        log = self.log_dir / f"{stage}-rewrite.log"
        if self.tool == "maven":
            plugin = "org.openrewrite.maven:rewrite-maven-plugin" + (f":{self.plugin_version}" if self.plugin_version else "")
            goal = f"{plugin}:{'dryRun' if dry else 'run'}"
            cmd = [self.exe, "-B", "-ntp", "-U", goal,
                   f"-Drewrite.recipeArtifactCoordinates={','.join(artifacts)}",
                   f"-Drewrite.activeRecipes={','.join(recipes)}",
                   "-Drewrite.exportDatatables=false", "-Drewrite.failOnInvalidActiveRecipes=true"] + self.extra
            return run(cmd, self.root, log, self.env)
        init = Path(tempfile.mkdtemp(prefix="spring-migrate-")) / "rewrite.init.gradle"
        deps = "\n".join(f'    rewrite("{a}")' for a in artifacts)
        active = "\n".join(f'    activeRecipe("{r}")' for r in recipes)
        init.write_text(f"""initscript {{
  repositories {{ gradlePluginPortal() }}
  dependencies {{ classpath("org.openrewrite:plugin:latest.release") }}
}}
rootProject {{
  plugins.apply(org.openrewrite.gradle.RewritePlugin)
  dependencies {{
{deps}
  }}
  rewrite {{
{active}
    setExportDatatables(false)
  }}
  afterEvaluate {{
    if (repositories.isEmpty()) {{ repositories {{ mavenCentral() }} }}
  }}
}}
""", encoding="utf-8")
        cmd = [self.exe, "--no-daemon", "--init-script", str(init), "rewriteDryRun" if dry else "rewriteRun"] + self.extra
        return run(cmd, self.root, log, self.env)

    def verify(self, stage: str) -> int:
        log = self.log_dir / f"{stage}-build.log"
        cmd = [self.exe, "-B", "-ntp", "verify"] if self.tool == "maven" else [self.exe, "build"]
        return run(cmd + self.extra, self.root, log, self.env)


# --------------------------------------------------------------------------- #
# migrate
# --------------------------------------------------------------------------- #
@dataclass
class Stage:
    key: str
    title: str
    recipes: list
    artifacts: list
    java_fix: bool = False


def plan_stages(target: str, java: int, junit4: bool, spring_v: str, extra_recipes: list[str], openjpa: bool = False) -> list[Stage]:
    sp, mj, tf = f"{R_SPRING}:{spring_v}", f"{R_MIGRATE}:RELEASE", f"{R_TESTING}:RELEASE"
    arts = [sp] + ([tf] if junit4 else [])
    stages = [Stage("1-boot-2.7-latest", "Spring Boot 2.7.13 -> latest 2.7.x (safe checkpoint)", [RECIPE_2_7], [sp])]
    if openjpa:
        stages.append(Stage(OPENJPA_STAGE, "OpenJPA -> Hibernate (on Boot 2.7 / Hibernate 5.6, before the Spring 6 jump)", [], []))
    r3 = [RECIPE_3_5, RECIPE_JAVA[java]] + ([RECIPE_JUNIT5] if junit4 else [])
    stages.append(Stage("2-boot-3.5", f"Spring Boot 3.5 / Spring 6 / Jakarta EE 10 / Java {java}", r3, arts + [mj], java_fix=True))
    if target == "4.0":
        stages.append(Stage("3-boot-4.0", "Spring Boot 4.0 / Spring Framework 7", [RECIPE_4_0], arts, java_fix=True))
    if extra_recipes:
        stages[-1].recipes += extra_recipes
    return stages


def cmd_migrate(a) -> int:
    root = Path(a.path).resolve()
    if not root.is_dir():
        raise MigrationError(f"Not a directory: {root}")
    env = build_env(a.java_home)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir = Path(a.report_dir).resolve() if a.report_dir else root.parent / f"{root.name}-migration-report-{stamp}"
    log_dir = report_dir / "logs"
    report_dir.mkdir(parents=True, exist_ok=True)

    # ---- pre-flight ------------------------------------------------------
    say(f"[preflight] project: {root}\n[preflight] report dir: {report_dir}")
    jm = java_major(env)
    if jm is None:
        raise MigrationError("`java` not found. Install JDK 17+ (or pass --java-home).")
    if jm < 17:
        raise MigrationError(f"Running on JDK {jm}. Spring Boot 3+ needs JDK 17+; install it or pass --java-home.")
    say(f"[preflight] JDK {jm}")
    if not a.no_git:
        if git(root, "rev-parse", "--is-inside-work-tree", check=False).strip() != "true":
            raise MigrationError("Not a git repository. Run `git init && git add -A && git commit` first, or pass --no-git (not recommended).")
        if git(root, "status", "--porcelain").strip() and not a.allow_dirty:
            raise MigrationError("Working tree has uncommitted changes. Commit/stash them or pass --allow-dirty.")
    pre = scan(root)
    info = pre["info"]
    if not info["tool"]:
        raise MigrationError("No pom.xml / build.gradle found in the project root.")
    say(f"[preflight] {info['tool']} project, Spring Boot {info['boot_version']}, Java {info['java_version']}")
    bv = info["boot_version"] or "2.7"
    try:
        major, minor = int(bv.split(".")[0]), int(bv.split(".")[1])
    except (ValueError, IndexError):
        major, minor = 2, 7
    if major >= 4:
        raise MigrationError(f"Project is already on Spring Boot {bv}; nothing to do.")
    resuming = major == 3
    (report_dir / "scan-before.md").write_text(render_scan(pre, "Pre-migration scan"), encoding="utf-8")
    blockers = [f for f in pre["findings"].values() if f.hits and f.rule.severity == "blocker"]
    if blockers and not a.force:
        say("\n[preflight] BLOCKERS that automation cannot fix:")
        for f in blockers:
            say(f"  - {f.rule.title} ({len(f.hits)} hit(s))")
        say(f"See {report_dir/'scan-before.md'}. Fix them first (they will not compile/start on Boot 3) or re-run with --force.")
        return 2

    junit4 = bool(pre["findings"]["junit4"].hits)
    use_openjpa = (openjpa_present(pre) or a.openjpa) and not a.no_openjpa
    if use_openjpa:
        say("[preflight] OpenJPA detected -> adding an OpenJPA -> Hibernate stage")
    stages = plan_stages(a.target, a.java, junit4, a.rewrite_spring_version, a.extra_recipe or [], use_openjpa)
    builder = Builder(root, info["tool"], env, log_dir, shlex.split(a.build_args or ""), a.rewrite_plugin_version)

    if resuming:
        stages = [st for st in stages if not (st.key[0] == "1" or (st.key.startswith("2-") and minor >= 5))]
        if not stages:
            say(f"[preflight] already on Spring Boot {bv}; target {a.target} reached.")
            return 0
        say(f"[preflight] Boot {bv} detected -> resuming with stage(s): {', '.join(st.key for st in stages)}")

    # ---- dry run ---------------------------------------------------------
    if a.dry_run:
        say("[dry-run] running the final-stage recipes against current code; nothing is modified.")
        recipes = [r for s in stages[1:] for r in s.recipes]
        arts = sorted({x for s in stages for x in s.artifacts})
        code = builder.rewrite("dry-run", recipes, arts, dry=True)
        patches = list(root.rglob("rewrite.patch"))
        for p in patches:
            shutil.copy(p, report_dir / "rewrite.patch")
        say(f"[dry-run] rewrite exit code {code}. Patch copied to {report_dir/'rewrite.patch'}" if patches
            else f"[dry-run] exit code {code}; no patch produced (see logs in {log_dir}).")
        return code

    # ---- branch ----------------------------------------------------------
    branch = f"migrate/spring-boot-{a.target}"
    created_branch = False
    if not a.no_git:
        current = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current.startswith("migrate/"):
            branch = current
            say(f"[git] continuing on branch {branch}")
        else:
            git(root, "checkout", "-b", branch)
            created_branch = True
            say(f"[git] created branch {branch}")

    # ---- baseline --------------------------------------------------------
    results = []
    if not a.skip_tests and not a.skip_baseline and not resuming and (a.no_git or created_branch):
        say("[baseline] build + tests on the ORIGINAL code (so you can tell old failures from new ones)")
        t0 = time.time()
        code = builder.verify("0-baseline")
        results.append(("0-baseline", "Baseline build+tests on 2.7", code == 0, time.time() - t0, ""))
        if code != 0:
            say("[baseline] FAILED on the ORIGINAL code:")
            show_errors(log_dir / "0-baseline-build.log")
            if not a.force:
                say("[baseline] OpenRewrite needs a compilable project, so stopping. Fix the build first (JDK mismatch? missing "
                    "settings.xml -> use --build-args?), or re-run with --skip-baseline / --force.")
                if created_branch:
                    git(root, "checkout", "-q", "-")
                    git(root, "branch", "-q", "-D", branch)
                return 2
        else:
            say("[baseline] OK")

    # ---- stages ----------------------------------------------------------
    todo_all: list[str] = []
    failed_stage = None
    for st in stages:
        say(f"\n[stage {st.key}] {st.title}")
        t0 = time.time()
        changes = []
        if st.key == OPENJPA_STAGE:
            changes, todo = migrate_openjpa(root)
            todo_all += todo
            for c in changes:
                say(f"  openjpa: {c}")
            for t in todo:
                say(f"  TODO {t}")
            code, dl = 0, []
        else:
            code, dl = 1, []
            for attempt in range(1, a.retries + 2):
                code = builder.rewrite(st.key, st.recipes, st.artifacts, dry=False)
                dl = rewrite_had_download_failures(log_dir / f"{st.key}-rewrite.log") if code == 0 else []
                if code == 0 and not dl:
                    break
                if not a.no_git and not a.allow_dirty:   # tree was clean when we started -> safe to discard partial edits
                    git(root, "reset", "-q", "--hard", "HEAD")
                    git(root, "clean", "-fdq")
                if attempt <= a.retries and (dl or code != 0):
                    say(f"[stage {st.key}] attempt {attempt} failed ({'repository/download problem' if dl else 'exit ' + str(code)}); retrying in 30s")
                    time.sleep(30)
            if code != 0 or dl:
                results.append((st.key, st.title, False, time.time() - t0, "OpenRewrite failed - see rewrite log"))
                failed_stage = st.key
                if dl:
                    say(f"[stage {st.key}] OpenRewrite could not download artifacts, so it would have skipped edits silently. "
                        f"Nothing was committed for this stage. Check network/proxy, rate limits, or pass your mirror via "
                        f"--build-args \"-s settings.xml\":")
                    for l in dl:
                        say(f"    {l}")
                    say(f"    (full log: {log_dir / (st.key + '-rewrite.log')})")
                else:
                    say(f"[stage {st.key}] OpenRewrite failed (exit {code}):")
                    show_errors(log_dir / f"{st.key}-rewrite.log")
                break
        if st.java_fix:
            changes, todo = run_fixers(root, a.java)
            todo_all += todo
            for c in changes:
                say(f"  fixer: {c}")
        note = f"{len(changes)} post-fix change(s)"
        if not a.no_git and git(root, "status", "--porcelain").strip():
            git(root, "add", "-A")
            msg = f"chore(migration): {st.title}\n\nAutomated by spring-migrate {__version__} (OpenRewrite: {', '.join(st.recipes)})"
            r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, capture_output=True, text=True)
            if r.returncode != 0:
                raise MigrationError(f"git commit failed (is user.name/user.email set?): {r.stderr.strip()}")
            say(f"[git] committed stage {st.key}")
        ok = True
        if not a.skip_tests:
            say(f"[stage {st.key}] build + tests")
            ok = builder.verify(st.key) == 0
            say(f"[stage {st.key}] {'build OK' if ok else 'BUILD/TESTS FAILED'}")
            if not ok:
                show_errors(log_dir / f"{st.key}-build.log")
        results.append((st.key, st.title, ok, time.time() - t0, note))
        if not ok:
            failed_stage = st.key
            break

    # ---- report ----------------------------------------------------------
    post = scan(root)
    (report_dir / "scan-after.md").write_text(render_scan(post, "Post-migration scan"), encoding="utf-8")
    write_report(report_dir, root, a, pre, post, results, todo_all, failed_stage, branch if not a.no_git else None)
    say(f"\n[done] report: {report_dir/'MIGRATION_REPORT.md'}")
    if failed_stage:
        say(f"[done] stopped at stage {failed_stage}. Automated changes are committed; fix the build on top of them, "
            f"then re-run `spring-migrate migrate` for the remaining stages (it detects the current version).")
        return 1
    return 0


def write_report(report_dir, root, a, pre, post, results, todo, failed_stage, branch):
    before, after = scan_counts(pre), scan_counts(post)
    rules = {r.id: r for r in RULES}
    L = ["# Migration report", "",
         f"- Project: `{root}`", f"- Target: Spring Boot {a.target}, Java {a.java}",
         f"- Branch: `{branch}`" if branch else "- Branch: (git disabled)",
         f"- Result: **{'STOPPED at ' + failed_stage if failed_stage else 'all stages completed'}**", "",
         "## Stages", "", "| Stage | Status | Time | Notes |", "|---|---|---|---|"]
    for key, title, ok, secs, note in results:
        L.append(f"| {title} | {'OK' if ok else 'FAILED'} | {secs:0.0f}s | {note} |")
    L += ["", "## Findings before -> after", "", "| Finding | Kind | Before | After |", "|---|---|---|---|"]
    for k in sorted(set(before) | set(after), key=lambda k: (SEV_ORDER[rules[k].severity], k)):
        L.append(f"| {rules[k].title} | {KIND_LABEL[rules[k].kind]} | {before.get(k, 0)} | {after.get(k, 0)} |")
    left = [k for k in after if rules[k].kind != "auto"]
    L += ["", "## Manual follow-ups", ""]
    L += [f"- **{rules[k].title}** ({after[k]} hit(s)): {rules[k].advice}" for k in left] or ["- none detected by the scanner"]
    L += [f"- {t}" for t in todo]
    if openjpa_present(pre):
        L += [""] + openjpa_section(root)
    L += ["", "## Always review", ""] + [f"- {c}" for c in CHECKLIST]
    L += ["", "See `scan-after.md` for file/line locations and `logs/` for full build output.", ""]
    (report_dir / "MIGRATION_REPORT.md").write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_scan(a) -> int:
    root = Path(a.path).resolve()
    res = scan(root)
    if a.json:
        payload = {"info": res["info"], "findings": {k: {"severity": v.rule.severity, "kind": v.rule.kind, "title": v.rule.title,
                   "count": len(v.hits), "hits": v.hits[:50]} for k, v in res["findings"].items() if v.hits}}
        text = json.dumps(payload, indent=2)
    else:
        text = render_scan(res)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        say(f"written {a.out}")
    else:
        print(text)
    return 0


def cmd_fix(a) -> int:
    root = Path(a.path).resolve()
    changes, todo = run_fixers(root, a.java)
    for c in changes:
        say(c)
    for t in todo:
        say(f"TODO {t}")
    say(f"{len(changes)} change(s)")
    return 0


def cmd_openjpa(a) -> int:
    root = Path(a.path).resolve()
    changes, todo = migrate_openjpa(root)
    for c in changes:
        say(c)
    for t in todo:
        say(f"TODO {t}")
    say(f"{len(changes)} change(s). Review with `git diff`, then build.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="spring-migrate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="read-only analysis")
    s.add_argument("path")
    s.add_argument("--out", help="write report to this file")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_scan)

    f = sub.add_parser("fix", help="run only the post-fixers")
    f.add_argument("path")
    f.add_argument("--java", type=int, choices=(17, 21), default=17)
    f.set_defaults(fn=cmd_fix)

    o = sub.add_parser("openjpa", help="run only the OpenJPA -> Hibernate conversion (no build, no git)")
    o.add_argument("path")
    o.set_defaults(fn=cmd_openjpa)

    m = sub.add_parser("migrate", help="staged migration on a new git branch")
    m.add_argument("path")
    m.add_argument("--target", choices=("3.5", "4.0"), default="4.0", help="final Boot line (default 4.0; 3.5 is end-of-life for OSS)")
    m.add_argument("--java", type=int, choices=(17, 21), default=17, help="target Java level (default 17)")
    m.add_argument("--java-home", help="JDK 17+ to run the build with")
    m.add_argument("--dry-run", action="store_true", help="produce rewrite.patch only; modify nothing")
    m.add_argument("--skip-tests", action="store_true", help="skip build/test gates between stages")
    m.add_argument("--skip-baseline", action="store_true", help="skip build+tests on the original code")
    m.add_argument("--allow-dirty", action="store_true")
    m.add_argument("--no-git", action="store_true", help="do not branch/commit (not recommended)")
    m.add_argument("--force", action="store_true", help="continue even if scan finds blockers")
    m.add_argument("--openjpa", action="store_true", help="force the OpenJPA -> Hibernate stage even if detection finds nothing")
    m.add_argument("--no-openjpa", action="store_true", help="skip the OpenJPA -> Hibernate stage")
    m.add_argument("--retries", type=int, default=2, help="retries per stage on repository/download failures (default 2)")
    m.add_argument("--build-args", help='extra args for mvn/gradle, e.g. "-s /path/settings.xml -Pci"')
    m.add_argument("--report-dir", help="where to write report/logs (default: next to the project)")
    m.add_argument("--rewrite-spring-version", default="RELEASE", help="rewrite-spring version (pin for reproducible runs)")
    m.add_argument("--rewrite-plugin-version", help="rewrite-maven-plugin version (default: latest)")
    m.add_argument("--extra-recipe", action="append", help="additional OpenRewrite recipe name to run in the last stage (repeatable)")
    m.set_defaults(fn=cmd_migrate)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
