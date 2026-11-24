#!/usr/bin/env python3
"""
spring-migrate - staged, local-only Spring Boot 2.7 -> 3.5 -> 4.0 -> 4.1 migration tool.

Everything runs on YOUR machine. The tool never uploads code anywhere. The only
network traffic is Maven/Gradle downloading OpenRewrite recipes and dependencies
from whatever repositories your build is already configured to use (pass
--build-args "-s settings.xml" to use your corporate mirror).

Commands
  scan     read-only pre-flight analysis -> Markdown/JSON report
  migrate  staged migration on a new git branch, one commit per stage
  legacy   run only the legacy-library codemods (Netflix OSS, legacy OAuth2
           resource/authorization server, id pinning, JPQL params, stale pins)
  openjpa  run only the OpenJPA -> Hibernate conversion
  fix      run only the built-in post-fixers (javax->jakarta in resources,
           spring.factories, CI/Docker/wrappers, httptrace, dialects, ...)

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

__version__ = "2.0.0"

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


_ENCODING: dict[str, str] = {}


def decode_text(data: bytes) -> tuple[str, str]:
    """UTF-8 when valid, else ISO-8859-1 (the classic encoding of .properties files; lossless for every byte)."""
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def read(p: Path) -> str | None:
    try:
        text, enc = decode_text(p.read_bytes())
    except OSError:
        return None
    _ENCODING[str(p)] = enc
    return text


def write_file(p: Path, text: str):
    """Write in the encoding the file was read with, so umlauts in ISO-8859-1 files survive an edit."""
    enc = _ENCODING.get(str(p), "utf-8")
    try:
        data = text.encode(enc)
    except UnicodeEncodeError:
        if p.suffix == ".properties":   # keep the file's encoding; escape what it cannot hold, as .properties allows
            data = "".join(c if ord(c) < 256 else f"\\u{ord(c):04x}" for c in text).encode(enc)
        else:
            data = text.encode("utf-8")
    p.write_bytes(data)


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
    Rule("springfox", "Springfox / Swagger 2 (incompatible with Boot 3)", "high", "auto", BUILD + SRC,
         r"springfox|@EnableSwagger2",
         "OpenRewrite SpringFoxToSpringDoc + post-fixer (OpenAPI info bean, /v3/api-docs security paths, unquoted properties)."),
    Rule("netflix-oss", "Netflix Ribbon/Hystrix/Zuul (removed from Spring Cloud)", "high", "auto", BUILD + SRC,
         r"netflix-(?:ribbon|hystrix|zuul)|starter-(?:hystrix|ribbon|zuul)|@EnableZuulProxy|@EnableHystrix|@EnableCircuitBreaker",
         "Hystrix -> Resilience4j @CircuitBreaker (+ settings), Ribbon -> LoadBalancer (listOfServers, Random/RoundRobin rules), Zuul routes -> Gateway MVC routes."),
    Rule("sleuth", "Spring Cloud Sleuth / Brave (removed)", "high", "auto", BUILD + SRC,
         r"spring-cloud-(?:starter-)?sleuth|@NewSpan|brave\.",
         "OpenRewrite MigrateCloudSleuthToMicrometerTracing (part of UpgradeSpringBoot_3_5)."),
    Rule("legacy-oauth2", "Legacy spring-security-oauth2 (Authorization/Resource server)", "high", "auto", BUILD + SRC,
         r"org\.springframework\.security\.oauth:|spring-security-oauth2-autoconfigure|@EnableAuthorizationServer|@EnableResourceServer",
         "Resource server -> oauth2ResourceServer (jwt/opaque/HMAC, audiences, authorities claim kept); client props -> spring.security.oauth2.client; authorization server -> Spring Authorization Server."),
    Rule("powermock", "PowerMock (no Jakarta / modern JDK support)", "high", "auto", BUILD + SRC, r"powermock",
         "OpenRewrite ReplacePowerMockito (part of UpgradeSpringBoot_3_5); remaining Whitebox/whenNew edge cases fail the test gate."),
    Rule("httpclient4", "Apache HttpClient 4.x (Spring 6 requires HttpClient 5)", "medium", "auto", BUILD + SRC,
         r"<artifactId>httpclient</artifactId>|org\.apache\.http\.|HttpComponentsClientHttpRequestFactory",
         "OpenRewrite UpgradeApacheHttpClient_5 (part of UpgradeSpringBoot_3_5)."),
    Rule("hibernate-gen-auto", "GenerationType.AUTO / bare @GeneratedValue (Hibernate 6 changes strategy)", "high", "auto", SRC,
         r"GenerationType\.AUTO|@GeneratedValue(?!\s*\()",
         "Hibernate 6 maps AUTO to per-entity sequences (<entity>_seq) on sequence-capable DBs -> can break existing schemas. "
         "Pinned to the Hibernate 5 sequence (hibernate_sequence, increment 1) or IDENTITY, so Hibernate 6 keeps using the existing schema."),
    Rule("hibernate-types", "Hibernate 5 type mappings (@TypeDef, @Type(type=...), hibernate-types)", "high", "auto", SRC + BUILD,
         r"@TypeDefs?\b|@Type\s*\(\s*type\s*=|hibernate-types",
         "OpenRewrite MigrateToHypersistenceUtilsHibernate6x (part of UpgradeSpringBoot_3_5)."),
    Rule("hibernate-dialect", "Explicit Hibernate dialect / legacy generator props", "medium", "auto", CFG,
         r"dialect\s*[=:]|new[_-]generator[_-]mappings|hibernate\.ejb\.",
         "Version-specific dialects -> generic ones; new_generator_mappings removed (ids are pinned instead)."),
    Rule("httpstatus", "ResponseEntity.getStatusCode() now returns HttpStatusCode", "medium", "auto", SRC,
         r"getRawStatusCode\(\)|HttpStatus\s+\w+\s*=\s*[^;]*\.getStatusCode\(\)",
         "OpenRewrite getRawStatusCode/HttpStatusCode recipes (part of UpgradeSpringBoot_3_5)."),
    Rule("webmvc-adapter", "WebMvcConfigurerAdapter (removed in Spring 6)", "high", "auto", SRC, r"WebMvcConfigurerAdapter",
         "OpenRewrite MigrateWebMvcConfigurerAdapter."),
    Rule("httptrace", "Actuator httptrace (renamed to httpexchanges)", "medium", "auto", SRC + CFG,
         r"httptrace|HttpTraceRepository", "Use HttpExchangeRepository / endpoint id 'httpexchanges'."),
    Rule("listenablefuture", "ListenableFuture (removed in Spring Framework 7 / Boot 4)", "medium", "auto", SRC,
         r"\bListenableFuture\b|ListenableFutureCallback", "Switch to CompletableFuture."),
]

RULES += [
    Rule("openjpa-deps", "OpenJPA dependencies / build plugins", "high", "auto", BUILD,
         r"org\.apache\.openjpa|openjpa-maven-plugin|<artifactId>openjpa[\w-]*</artifactId>|openjpa\.version",
         "OpenRewrite ListenableToCompletableFuture."),
    Rule("openjpa-persistence-xml", "OpenJPA provider/properties in persistence.xml", "high", "auto", ("persistence.xml",),
         r"org\.apache\.openjpa|openjpa\.",
         "Provider switched to Hibernate; known openjpa.* properties mapped, the rest commented out with a TODO."),
    Rule("openjpa-spring-adapter", "OpenJPA persistence provider / Spring adapter wiring", "high", "auto", SRC + CFG + ("*.xml",),
         r"\bOpenJpa(?:VendorAdapter|Dialect)\b|\bPersistenceProviderImpl\b|org\.apache\.openjpa\.persistence\.PersistenceProviderImpl",
         "PersistenceProviderImpl -> HibernatePersistenceProvider, OpenJpa* adapters -> Hibernate equivalents."),
    Rule("openjpa-api", "OpenJPA-specific API / annotations in code", "high", "auto", SRC,
         r"org\.apache\.openjpa|\bOpenJPA\w+",
         "OpenJPAPersistence.cast/OpenJPAEntityManager/Query/Factory -> Hibernate unwrap; @DataCache/@FetchGroup/@Persistent*/@ReadOnly/@Index/@Externalizer/@Dependent... -> JPA. FetchPlan usage is reported as residual."),
    Rule("openjpa-props-code", "openjpa.* properties set from code or application config", "medium", "auto", SRC + CFG,
         r"[\"']openjpa\.|^\s*openjpa\.",
         "Translated to javax.persistence.*/hibernate.* (or commented with the reason)."),
    Rule("openjpa-agent", "OpenJPA enhancer / javaagent in scripts or images", "medium", "auto",
         ("Dockerfile", "Dockerfile.*", "*.sh", "*.cmd", "*.bat", "*.service", "*.env", "Jenkinsfile"),
         r"javaagent[^\n]*openjpa|openjpa[-\w.]*\.jar|PCEnhancer", "Hibernate needs no enhancement agent - remove it."),
    Rule("jpql-jdbc-params", "JDBC-style '?' parameters in JPQL (rejected by Hibernate 5.3+)", "high", "auto", SRC,
         r"(?:createQuery|createNamedQuery|@NamedQuery)\b[^\n]*\"[^\"]*\?(?!\d)",
         "Numbered automatically (?1, ?2, ...) in createQuery/@Query/@NamedQuery/orm.xml."),
]

RULES += [
    # What is still genuinely manual after the codemods: the scanner reports these from the post-migration code.
    Rule("zuul-filter", "ZuulFilter implementations", "high", "manual", SRC, r"extends\s+ZuulFilter|RequestContext\.getCurrentContext",
         "Port to a Spring Cloud Gateway MVC filter function (BeforeFilterFunctions / HandlerFilterFunction)."),
    Rule("hystrix-api", "Programmatic Hystrix API", "high", "manual", SRC, r"extends\s+Hystrix(?:Observable)?Command|HystrixRequestContext",
         "Use CircuitBreakerFactory.create(name).run(supplier, fallback)."),
    Rule("ribbon-custom", "Custom Ribbon rules / server lists", "medium", "manual", SRC,
         r"implements\s+IRule|extends\s+AbstractLoadBalancerRule|IPing\b|ServerList<",
         "Implement ReactorServiceInstanceLoadBalancer / ServiceInstanceListSupplier."),
    Rule("oauth2-rest-template", "OAuth2RestTemplate (legacy client)", "medium", "manual", SRC, r"\bOAuth2RestTemplate\b|ClientCredentialsResourceDetails",
         "Use RestClient with OAuth2ClientHttpRequestInterceptor + OAuth2AuthorizedClientManager."),
    Rule("openjpa-fetchplan", "OpenJPA FetchPlan / FetchConfiguration", "medium", "manual", SRC, r"\b(?:JDBC)?FetchPlan\b|FetchConfiguration",
         "Use an EntityGraph hint (jakarta.persistence.fetchgraph) or JOIN FETCH."),
]

CHECKLIST = [
    "Trailing slashes: a TrailingSlashCompatibilityConfig filter keeps /users/ working; remove it once clients no longer send trailing slashes.",
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
            write_file(p, new)
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
        write_file(imports, "\n".join(merged) + "\n")
        remaining = [l for l in kept if l.strip() and not l.strip().startswith("#")]
        if remaining:
            write_file(f, "\n".join(kept).rstrip() + "\n")
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
            write_file(p, new)
            changes.append(f"{rel(p, root)}: Java version -> {java}")
    return changes, todo


def run_fixers(root: Path, java: int, boot_line: str = "3.5", base_rev: str | None = None,
               trailing_slash: bool = False, stage_rev: str | None = None, boot_version: str | None = None,
               extra: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Post-fixers after a stage's OpenRewrite recipes. Idempotent."""
    changes = fix_spring_factories(root) + fix_javax_residuals(root)
    cc, todo = fix_containers_and_ci(root, java)
    ci, todo2 = fix_ci_java(root, java)
    changes += cc + ci
    changes += fix_quoted_properties(root, base_rev)
    changes += fix_springdoc_leftovers(root)
    changes += fix_springdoc(root, boot_line)
    changes += fix_hibernate6_types(root)
    jc, jt = fix_jaxb_tooling(root)
    changes += jc
    todo_extra = jt
    if boot_version:
        dc, dt = fix_downgraded_boot_managed(root, boot_version, local_repository(extra or [], os.environ.copy()))
        changes += dc
        todo_extra += dt
    changes += fix_httptrace(root)
    changes += fix_httpclient5_checked(root)
    changes += fix_hibernate_props(root)
    changes += fix_build_wrappers(root, boot_line)
    changes += fix_spring_cloud_train(root, boot_line)
    changes += fix_third_party(root)
    changes += fix_temporary_versions(root)
    if boot_line.startswith("4"):
        changes += fix_boot4_aspectj(root, stage_rev)
        changes += fix_sas_configurer_package(root)
    if trailing_slash:
        changes += fix_trailing_slash(root)
    return changes, todo + todo2 + todo_extra


def run_legacy_codemods(root: Path, phase: str = "pre") -> tuple[list[str], list[str]]:
    """Replacements for libraries that do not exist on Boot 3.
    pre:  on the Boot 2.7 code, before the 3.5 recipes (the result must still compile on 2.7 - the rewrite plugin compiles first).
    post: after the 3.5 recipes, for generated code that only compiles on Boot 3 (Spring Authorization Server)."""
    changes, todo = [], []
    fns = {"pre": (cm_stale_version_pins, cm_pin_generated_ids, cm_jpql_positional, cm_netflix, cm_legacy_oauth2, cm_openjpa_agent,
                   cm_springdoc_inline_versions),
           "post": (cm_authorization_server,)}[phase] if phase != "all" else \
        (cm_stale_version_pins, cm_pin_generated_ids, cm_jpql_positional, cm_netflix, cm_authorization_server, cm_legacy_oauth2, cm_openjpa_agent)
    for fn in fns:
        c, t = fn(root)
        changes += c
        todo += t
    return changes, todo


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
            write_file(f, new)
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
            write_file(f, new)
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
                write_file(f, body)
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
            write_file(f, new)
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
            write_file(f, new)
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
            write_file(f, text.rstrip("\n") + "\n" + add)
            changes.append(f"{rel(f, root)}: added identifier-naming defaults")
    return changes


def migrate_openjpa(root: Path) -> tuple[list[str], list[str]]:
    c1, t1 = openjpa_persistence_xml(root)
    c2, t2 = openjpa_build_files(root)
    c3 = openjpa_spring_adapters(root)
    c4 = openjpa_naming_defaults(root)
    changes, todo = c1 + c2 + c3 + c4, t1 + t2
    for fn in (cm_openjpa_api, cm_openjpa_annotations, cm_openjpa_code_props, cm_openjpa_agent, cm_jpql_positional,
               lambda r: cm_pin_generated_ids(r, "openjpa")):
        c, t = fn(root)
        changes += c
        todo += t
    if any("OPENJPA_SEQUENCE_TABLE" in c for c in changes):
        changes += openjpa_pooled_lo(root)
    return changes, todo


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
# Source editing helpers (Java text, build files, Spring config files)
# --------------------------------------------------------------------------- #
def write_if_changed(p: Path, old: str, new: str) -> bool:
    if new != old:
        write_file(p, new)
        return True
    return False


def skip_java_literal(text: str, i: int) -> int:
    """i points at a quote; return the index just after the literal (handles text blocks)."""
    q = text[i]
    if q == '"' and text.startswith('"""', i):
        j = text.find('"""', i + 3)
        while j != -1 and text[j - 1] == "\\":
            j = text.find('"""', j + 1)
        return len(text) if j == -1 else j + 3
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == q or text[j] == "\n":
            return j + 1
        j += 1
    return j


def balanced(text: str, i: int, open_ch="(", close_ch=")") -> int:
    """i points at open_ch; return index of the matching close_ch (string/char literals and comments skipped), or -1."""
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c in "\"'":
            j = skip_java_literal(text, j)
            continue
        if text.startswith("//", j):
            j = text.find("\n", j)
            if j == -1:
                return -1
            continue
        if text.startswith("/*", j):
            j = text.find("*/", j)
            if j == -1:
                return -1
            j += 2
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def split_top_level(s: str, sep=",") -> list[str]:
    parts, depth, cur, j = [], 0, [], 0
    while j < len(s):
        c = s[j]
        if c in "\"'":
            k = skip_java_literal(s, j)
            cur.append(s[j:k]); j = k
            continue
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(c)
        j += 1
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts]


def annotation_spans(text: str, name: str):
    """Yield (start, end, args or None) for every @name / @name(...) annotation (simple or qualified name)."""
    for m in re.finditer(r"@(?:[\w.]+\.)?" + re.escape(name) + r"\b(?!\.)", text):
        j = m.end()
        k = j
        while k < len(text) and text[k] in " \t":
            k += 1
        if k < len(text) and text[k] == "(":
            e = balanced(text, k)
            if e == -1:
                continue
            yield m.start(), e + 1, text[k + 1:e]
        else:
            yield m.start(), j, None


def ann_attr(args: str | None, key: str) -> str | None:
    """Value of key=... in annotation args (raw text); 'value' also matches a bare single argument."""
    if args is None:
        return None
    for part in split_top_level(args):
        m = re.match(r"(\w+)\s*=\s*(.*)$", part, re.S)
        if m and m.group(1) == key:
            return m.group(2).strip()
        if not m and key == "value":
            return part.strip()
    return None


def unquote_java(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def java_imports(text: str) -> list[str]:
    return re.findall(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", text, re.M)


def add_import(text: str, fqcn: str) -> str:
    simple = fqcn.rsplit(".", 1)[-1]
    pkg = fqcn.rsplit(".", 1)[0]
    imps = java_imports(text)
    if fqcn in imps or f"{pkg}.*" in imps:
        return text
    if any(i.rsplit(".", 1)[-1] == simple for i in imps):
        return text  # a different class with that simple name is imported; caller should use the FQCN
    m = list(re.finditer(r"^[ \t]*import\s+(static\s+)?([\w.*]+)\s*;[ \t]*\n", text, re.M))
    line = f"import {fqcn};\n"
    plain = [x for x in m if not x.group(1)]
    if plain:
        # keep the existing order (checkstyle ImportOrder/CustomImportOrder, spotless importOrder): insert before the first
        # larger import of the same top-level package group, else after the last smaller one of that group
        top = fqcn.split(".")[0]
        same = [x for x in plain if x.group(2).split(".")[0] == top]
        names = [x.group(2) for x in plain]
        if names == sorted(names) or same:
            group = same or plain
            nxt = next((x for x in group if x.group(2) > fqcn), None)
            if nxt is not None:
                return text[:nxt.start()] + line + text[nxt.start():]
            return text[:group[-1].end()] + line + text[group[-1].end():]
        return text[:plain[-1].end()] + line + text[plain[-1].end():]
    if m:
        return text[:m[-1].end()] + line + text[m[-1].end():]
    pm = re.search(r"^\s*package\s+[\w.]+\s*;[ \t]*\n", text, re.M)
    if pm:
        return text[:pm.end()] + "\n" + line + text[pm.end():]
    return line + text


def remove_import(text: str, pattern: str) -> str:
    """Remove imports whose name matches the regex pattern (anchored)."""
    return re.sub(r"^[ \t]*import\s+(?:static\s+)?(?:" + pattern + r")\s*;[ \t]*\r?\n", "", text, flags=re.M)


def remove_annotation(text: str, name: str) -> tuple[str, int]:
    n = 0
    for s, e, _ in sorted(annotation_spans(text, name), reverse=True):
        ls = text.rfind("\n", 0, s) + 1
        le = text.find("\n", e)
        le = len(text) if le == -1 else le
        if text[ls:s].strip() == "" and text[e:le].strip() == "":
            text = text[:ls] + text[le + 1:]
        else:
            text = text[:s] + text[e:].lstrip(" ")
        n += 1
    return text, n


def uses_simple_name(text: str, simple: str) -> bool:
    body = re.sub(r"^\s*import\s+[^;]+;", "", text, flags=re.M)
    return re.search(r"\b" + re.escape(simple) + r"\b", body) is not None


def drop_unused_imports(text: str, fqcns) -> str:
    for f in fqcns:
        if not uses_simple_name(text, f.rsplit(".", 1)[-1]):
            text = remove_import(text, re.escape(f))
    return text


def persistence_pkg(text: str) -> str:
    return "jakarta.persistence" if "jakarta.persistence" in text else "javax.persistence"


def main_package(root: Path) -> tuple[str | None, Path | None]:
    """Package and source dir of the @SpringBootApplication class."""
    for p in walk(root):
        if p.suffix != ".java" or "/src/main/java/" not in p.as_posix():
            continue
        t = read(p) or ""
        if "@SpringBootApplication" in t:
            m = re.search(r"^\s*package\s+([\w.]+)\s*;", t, re.M)
            return (m.group(1) if m else None), p.parent
    return None, None


def java_sources(root: Path, main_only=False):
    for p in walk(root):
        if p.suffix == ".java" and (not main_only or "/src/main/" in p.as_posix()):
            yield p


# ---- build files ------------------------------------------------------------
def build_files(root: Path):
    for p in walk(root):
        if p.name == "pom.xml" or p.name in ("build.gradle", "build.gradle.kts"):
            yield p


def pom_dependency_blocks(text: str):
    """(start, end, groupId, artifactId) for each <dependency> outside dependencyManagement/plugins."""
    excl = [m.span() for m in re.finditer(r"<(dependencyManagement|plugins|pluginManagement|exclusions)>.*?</\1>", text, re.S)]
    for m in re.finditer(r"[ \t]*<dependency>(?:(?!<dependency>).)*?</dependency>[ \t]*\r?\n?", text, re.S):
        if any(a <= m.start() < b for a, b in excl):
            continue
        b = m.group(0).split("<exclusions>")[0]
        yield m.start(), m.end(), _first_tag(b, "groupId"), _first_tag(b, "artifactId")


def build_has_artifact(text: str, artifact_rx: str) -> bool:
    return re.search(r"<artifactId>" + artifact_rx + r"</artifactId>|['\":]" + artifact_rx + r"['\":]", text) is not None


def build_remove_deps(p: Path, text: str, pred) -> tuple[str, list[str]]:
    """pred(groupId, artifactId) -> bool. Returns new text and removed coordinates."""
    removed = []
    if p.name == "pom.xml":
        for s, e, g, a in sorted(pom_dependency_blocks(text), reverse=True):
            if pred(g, a):
                text = text[:s] + text[e:]
                removed.append(f"{g}:{a}")
        # multi-module builds pin versions in the root's <dependencyManagement>; drop those entries as well
        def strip_managed(m):
            block, _ = _strip_blocks(m.group(0), "dependency", lambda b: "<scope>import</scope>" not in b and
                                     pred(_first_tag(b.split("<exclusions>")[0], "groupId"), _first_tag(b.split("<exclusions>")[0], "artifactId")))
            return block
        text = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", strip_managed, text, flags=re.S)
        return text, removed
    out = []
    for line in text.splitlines(keepends=True):
        m = re.search(r"['\"]([\w.\-]+):([\w.\-]+)(?::[^'\"]*)?['\"]", line)
        if m and re.match(r"\s*(implementation|api|compile|runtimeOnly|compileOnly|testImplementation|testRuntimeOnly|annotationProcessor|runtime)\b", line) and pred(m.group(1), m.group(2)):
            removed.append(f"{m.group(1)}:{m.group(2)}")
            continue
        out.append(line)
    return "".join(out), removed


def pom_coords(text: str) -> tuple[str, str]:
    """(groupId, artifactId) of a POM itself (groupId inherited from <parent> when absent)."""
    own = re.sub(r"<(parent|dependencies|dependencyManagement|build|profiles|plugins|reporting)>.*?</\1>", "", text, flags=re.S)
    pm = re.search(r"<parent>(.*?)</parent>", text, re.S)
    return (_first_tag(own, "groupId") or (_first_tag(pm.group(1), "groupId") if pm else "")), _first_tag(own, "artifactId")


def reactor_coords(root: Path) -> set[tuple[str, str]]:
    """Modules of the build itself (they are never upgraded from a repository)."""
    return {pom_coords(read(p) or "") for p in walk(root) if p.name == "pom.xml"}


def reactor_ancestors(root: Path, pom: Path) -> list[Path]:
    """pom, then its parents that are part of this build (via <relativePath>, default ../pom.xml)."""
    chain, cur = [pom], pom
    coords = reactor_coords(root)
    for _ in range(10):
        t = read(cur) or ""
        pm = re.search(r"<parent>(.*?)</parent>", t, re.S)
        if not pm:
            break
        pg, pa = _first_tag(pm.group(1), "groupId"), _first_tag(pm.group(1), "artifactId")
        if (pg, pa) not in coords:
            break
        relp = re.search(r"<relativePath>([^<]*)</relativePath>", pm.group(1))
        cand = (cur.parent / (relp.group(1) if relp and relp.group(1) else "../pom.xml")).resolve()
        if cand.is_dir():
            cand = cand / "pom.xml"
        if not cand.exists() or not str(cand).startswith(str(root.resolve())) or cand in chain:
            match = next((p for p in walk(root) if p.name == "pom.xml" and pom_coords(read(p) or "") == (pg, pa)), None)
            if not match or match in chain:
                break
            cand = match
        chain.append(cand)
        cur = cand
    return chain


def reactor_text(root: Path, bf: Path) -> str:
    """A module's build file plus the in-build parents it inherits from."""
    if bf.name != "pom.xml":
        return (read(bf) or "") + (read(root / "build.gradle") or "") + (read(root / "build.gradle.kts") or "")
    return "\n".join(read(p) or "" for p in reactor_ancestors(root, bf))


def build_add_dep(p: Path, text: str, group: str, artifact: str, version: str | None = None, scope: str | None = None) -> str:
    if build_has_artifact(text, re.escape(artifact)):
        return text
    if p.name == "pom.xml":
        x = f"    <dependency>\n      <groupId>{group}</groupId>\n      <artifactId>{artifact}</artifactId>\n"
        if version:
            x += f"      <version>{version}</version>\n"
        if scope:
            x += f"      <scope>{scope}</scope>\n"
        x += "    </dependency>\n"
        return _insert_dependency(text, x)
    conf = {"runtime": "runtimeOnly", "test": "testImplementation", None: "implementation"}.get(scope, "implementation")
    coord = f"{group}:{artifact}" + (f":{version}" if version else "")
    line = f'    {conf}("{coord}")\n' if p.suffix == ".kts" else f"    {conf} '{coord}'\n"
    m = re.search(r"^dependencies\s*\{[ \t]*\n", text, re.M)
    if m:
        return text[:m.end()] + line + text[m.end():]
    return text.rstrip("\n") + "\n\ndependencies {\n" + line + "}\n"


def _top_level(text: str, tag: str) -> re.Match | None:
    """First <tag> that is a direct child of <project> (not inside dependencyManagement/build/profiles/...)."""
    nested = [m.span() for m in re.finditer(r"<(dependencyManagement|build|profiles|reporting|parent|pluginManagement)>.*?</\1>", text, re.S)]
    for m in re.finditer(r"[ \t]*<" + tag + r">", text):
        if not any(a <= m.start() < b for a, b in nested):
            return m
    return None


def pom_set_property(text: str, name: str, value: str) -> str:
    rx = re.compile(r"(<" + re.escape(name) + r">)[^<]*(</" + re.escape(name) + r">)")
    if rx.search(text):
        return rx.sub(lambda m: m.group(1) + value + m.group(2), text, count=1)
    m = _top_level(text, "properties")
    if m:
        end = text.index(">", m.end() - 1) + 1
        return text[:end] + f"\n    <{name}>{value}</{name}>" + text[end:]
    anchor = _top_level(text, "dependencyManagement") or _top_level(text, "dependencies") or _top_level(text, "build")
    pos = anchor.start() if anchor else text.rindex("</project>")
    return text[:pos] + f"  <properties>\n    <{name}>{value}</{name}>\n  </properties>\n" + text[pos:]


def pom_ensure_bom(text: str, group: str, artifact: str, version_expr: str) -> str:
    if re.search(r"<artifactId>" + re.escape(artifact) + r"</artifactId>", text):
        return text
    dep = (f"      <dependency>\n        <groupId>{group}</groupId>\n        <artifactId>{artifact}</artifactId>\n"
           f"        <version>{version_expr}</version>\n        <type>pom</type>\n        <scope>import</scope>\n      </dependency>\n")
    m = re.search(r"<dependencyManagement>\s*<dependencies>[ \t]*\n?", text)
    if m:
        return text[:m.end()] + ("" if text[m.end() - 1] == "\n" else "\n") + dep + text[m.end():]
    block = f"  <dependencyManagement>\n    <dependencies>\n{dep}    </dependencies>\n  </dependencyManagement>\n"
    anchor = _top_level(text, "dependencies") or _top_level(text, "build")
    pos = anchor.start() if anchor else text.rindex("</project>")
    return text[:pos] + block + text[pos:]


# ---- Spring config files (application*.properties / *.yml) ------------------
class ConfigFile:
    """Minimal editor for Spring Boot .properties and simple block-style YAML.
    Keys are flattened Spring-style (a.b.c, list[0]). Writes add flat keys, which Boot accepts in YAML too."""

    def __init__(self, path: Path):
        self.path = path
        self.yaml = path.suffix in (".yml", ".yaml")
        self.original = read(path) or "" if path.exists() else ""
        self.lines = self.original.splitlines()
        self.appended: dict[int, list[str]] = {}

    # parsing -> list of dicts {key, value, line, doc}
    def entries(self) -> list[dict]:
        return self._yaml_entries() if self.yaml else self._props_entries()

    def _props_entries(self):
        out, i = [], 0
        while i < len(self.lines):
            line = self.lines[i]
            s = line.strip()
            start = i
            if not s or s[0] in "#!":
                i += 1
                continue
            while line.rstrip().endswith("\\") and i + 1 < len(self.lines):
                i += 1
                line = line.rstrip()[:-1] + self.lines[i].lstrip()
            m = re.match(r"\s*((?:\\.|[^=:\s])+)\s*[=:\s]\s*(.*)$", line)
            if m:
                out.append({"key": m.group(1), "value": m.group(2).strip(), "line": start, "end": i, "doc": 0})
            i += 1
        return out

    def _yaml_entries(self):
        out, stack, doc = [], [], 0          # stack: (indent, keypart, kind)
        counters: dict[tuple, int] = {}
        key_rx = re.compile(r"('[^']*'|\"[^\"]*\"|[^#'\"\s][^:#]*?):(?:\s+(.*))?$")

        def path(extra=""):
            s = ""
            for _, k, _ in stack:
                s = s + k if k.startswith("[") else (s + "." + k if s else k)
            if extra:
                s = s + extra if extra.startswith("[") else (s + "." + extra if s else extra)
            return s
        for i, raw in enumerate(self.lines):
            if raw.strip() in ("---", "..."):
                doc += 1; stack = []; counters = {}
                continue
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            ind = len(raw) - len(raw.lstrip())
            body = raw.strip()
            if body == "-" or body.startswith("- "):
                while stack and (stack[-1][0] > ind or (stack[-1][0] == ind and stack[-1][2] == "item")):
                    stack.pop()
                parent = path()
                idx = counters.get((doc, parent), -1) + 1
                counters[(doc, parent)] = idx
                stack.append((ind, f"[{idx}]", "item"))
                rest = body[1:].strip()
                km = key_rx.match(rest) if rest and rest[0] not in "'\"[{" else None
                if km:
                    k = km.group(1).strip("'\"")
                    v = self._yval(km.group(2))
                    stack.append((ind + 2, k, "key"))
                    if v is not None:
                        out.append({"key": path(), "value": v, "line": i, "end": i, "doc": doc})
                else:
                    v = self._yval(rest)
                    if v is not None:
                        out.append({"key": path(), "value": v, "line": i, "end": i, "doc": doc})
                continue
            km = key_rx.match(body)
            if not km:
                continue
            while stack and stack[-1][0] >= ind:
                stack.pop()
            k = km.group(1).strip("'\"")
            v = self._yval(km.group(2))
            stack.append((ind, k, "key"))
            if v is not None:
                out.append({"key": path(), "value": v, "line": i, "end": i, "doc": doc})
        return out

    @staticmethod
    def _yval(v):
        if v is None:
            return None
        v = v.strip()
        if not v or v in ("|", ">", "|-", ">-"):
            return None
        if v[0] in "'\"":
            q = v[0]
            e = v.find(q, 1)
            return v[1:e] if e > 0 else v[1:]
        return re.sub(r"\s+#.*$", "", v)

    def get(self, key: str):
        for e in self.entries():
            if e["key"] == key:
                return e["value"]
        return None

    def keys_matching(self, rx: str) -> list[dict]:
        r = re.compile(rx)
        return [e for e in self.entries() if r.match(e["key"])]

    def remove(self, key: str) -> int | None:
        """Remove a leaf; returns its document index or None."""
        for e in self.entries():
            if e["key"] == key:
                self._delete_lines(e["line"], e["end"])
                return e["doc"]
        return None

    def _delete_lines(self, a: int, b: int):
        del self.lines[a:b + 1]
        if self.yaml:
            self._prune_empty_parents()

    def _prune_empty_parents(self):
        changed = True
        while changed:
            changed = False
            for i, raw in enumerate(self.lines):
                b = raw.strip()
                if not b or b.startswith("#") or b == "---" or not re.match(r"[^-][^:]*:\s*$", b):
                    continue
                ind = len(raw) - len(raw.lstrip())
                j = i + 1
                while j < len(self.lines) and (not self.lines[j].strip() or self.lines[j].lstrip().startswith("#")):
                    j += 1
                nxt = self.lines[j] if j < len(self.lines) else None
                nind = None if nxt is None else len(nxt) - len(nxt.lstrip())
                if nxt is None or nxt.strip() == "---" or nind < ind or (nind == ind and not nxt.lstrip().startswith("-")):
                    del self.lines[i]
                    changed = True
                    break

    def put(self, key: str, value: str, doc: int = 0, comment: str | None = None):
        if self.get(key) is not None:
            self.remove(key)
        if self.yaml:
            v = value
            if v == "" or re.search(r"^[\s*&!|>'\"%@`,\[\]{}#?:-]|: | #|\s$", v):
                v = json.dumps(v)
            line = f"{key}: {v}"
            if comment:
                line = f"# {comment}\n{line}"
        else:
            line = f"{key}={value}"
            if comment:
                line = f"# {comment}\n{line}"
        self.appended.setdefault(doc, []).append(line)

    def comment_out(self, key: str, note: str) -> bool:
        for e in self.entries():
            if e["key"] == key:
                if self.yaml:
                    self._delete_lines(e["line"], e["end"])
                    self.appended.setdefault(e["doc"], []).append(f"# TODO(spring-migrate) removed {key}={e['value']}: {note}")
                else:
                    for i in range(e["line"], e["end"] + 1):
                        self.lines[i] = "# " + self.lines[i]
                    self.lines.insert(e["line"], f"# TODO(spring-migrate): {note}")
                return True
        return False

    def text(self) -> str:
        lines = list(self.lines)
        if self.appended:
            # insert appended lines at the end of their document
            doc_ends, doc = {}, 0
            for i, l in enumerate(lines):
                if l.strip() == "---":
                    doc_ends[doc] = i
                    doc += 1
            doc_ends.setdefault(doc, len(lines))
            for d in sorted(self.appended, reverse=True):
                at = doc_ends.get(d, len(lines))
                while at > 0 and not lines[at - 1].strip():
                    at -= 1
                lines[at:at] = self.appended[d]
        s = "\n".join(lines)
        return s + "\n" if s else ""

    def save(self) -> bool:
        new = self.text()
        if new != (self.original if self.original.endswith("\n") or not self.original else self.original + "\n"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_file(self.path, new)
            return True
        return False


def config_files(root: Path, include_test=True) -> list[Path]:
    out = []
    for p in walk(root):
        pp = p.as_posix()
        if not re.match(r"(application|bootstrap)([-.][\w-]+)?\.(properties|ya?ml)$", p.name):
            continue
        if "/src/main/resources" in pp or (include_test and "/src/test/resources" in pp) or p.parent == root or p.parent.name == "config":
            out.append(p)
    return out


def main_config(root: Path, module_dir: Path | None = None) -> ConfigFile:
    base = (module_dir or root) / "src/main/resources"
    for n in ("application.properties", "application.yml", "application.yaml"):
        if (base / n).exists():
            return ConfigFile(base / n)
    return ConfigFile(base / "application.properties")


def module_of(p: Path, root: Path) -> Path:
    """Directory of the build module that contains p."""
    d = p.parent
    while d != root and d != d.parent:
        if (d / "pom.xml").exists() or (d / "build.gradle").exists() or (d / "build.gradle.kts").exists():
            return d
        d = d.parent
    return root


def module_build_file(mod: Path) -> Path | None:
    for n in ("pom.xml", "build.gradle", "build.gradle.kts"):
        if (mod / n).exists():
            return mod / n
    return None


def edit_build(mod: Path, fn) -> bool:
    """fn(path, text) -> new text for the module's build file."""
    bf = module_build_file(mod)
    if not bf:
        return False
    t = read(bf) or ""
    return write_if_changed(bf, t, fn(bf, t))


# ---- versions from Maven Central (with offline fallbacks) -------------------
_VERSION_CACHE: dict[str, list[str]] = {}
OFFLINE = False


def central_versions(group: str, artifact: str) -> list[str]:
    key = f"{group}:{artifact}"
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    vs: list[str] = []
    if not OFFLINE:
        import urllib.request
        url = f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}/maven-metadata.xml"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                vs = re.findall(r"<version>([^<]+)</version>", r.read().decode("utf-8", "replace"))
        except Exception:
            vs = []
    _VERSION_CACHE[key] = vs
    return vs


def vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)[:4])


def latest_patch(group: str, artifact: str, line: str, fallback: str) -> str:
    """Latest GA version that starts with `line.` (e.g. 4.1 -> 4.1.3)."""
    cands = [v for v in central_versions(group, artifact)
             if v.startswith(line + ".") and re.fullmatch(r"[\d.]+", v)]
    return max(cands, key=vtuple) if cands else fallback


BOOT_FALLBACK = {"3.5": "3.5.16", "4.0": "4.0.10", "4.1": "4.1.3"}
# Release train -> Boot lines it accepts (read from spring-cloud-commons' CompatibilityVerifierProperties).
CLOUD_TRAIN = {"3.5": ("2025.0", "2025.0.3"), "4.0": ("2025.1", "2025.1.5"), "4.1": ("2025.1", "2025.1.5")}


def boot_version_for(line: str) -> str:
    return latest_patch("org.springframework.boot", "spring-boot-dependencies", line, BOOT_FALLBACK[line])


def cloud_version_for(boot_line: str) -> str:
    train, fb = CLOUD_TRAIN[boot_line]
    return latest_patch("org.springframework.cloud", "spring-cloud-dependencies", train, fb)

# --------------------------------------------------------------------------- #
# Codemods for things OpenRewrite does not migrate (run before the Boot 3.5 recipes)
# Each returns (changes, todo). They are idempotent: running twice changes nothing.
# --------------------------------------------------------------------------- #
# Boot-managed version properties that legacy projects commonly override. A Boot 2.x-era pin left in place
# overrides the version Boot 3/4 needs (e.g. mockito.version=4.x breaks on Boot 3.5).
BOOT_MANAGED_PROPS = (
    "mockito.version", "byte-buddy.version", "hibernate.version", "hibernate-validator.version", "jackson-bom.version",
    "jackson.version", "junit-jupiter.version", "spring-framework.version", "spring.version", "spring-security.version",
    "spring-data-bom.version", "spring-data-releasetrain.version", "tomcat.version", "jetty.version", "undertow.version",
    "netty.version", "reactor-bom.version", "micrometer.version", "flyway.version", "liquibase.version", "h2.version",
    "postgresql.version", "mysql.version", "mariadb.version", "lombok.version", "logback.version", "slf4j.version",
    "log4j2.version", "snakeyaml.version", "thymeleaf.version", "httpclient.version", "httpcore.version", "assertj.version",
    "hamcrest.version", "json-path.version", "kotlin.version", "kotlin-coroutines.version", "elasticsearch.version",
    "kafka.version", "spring-kafka.version", "spring-amqp.version", "rabbit-amqp-client.version", "caffeine.version",
    "ehcache3.version", "hazelcast.version", "jooq.version", "querydsl.version", "groovy.version", "jakarta-servlet.version",
    "javax-servlet.version", "selenium.version", "testcontainers.version", "awaitility.version", "jersey.version",
)


def cm_stale_version_pins(root: Path) -> tuple[list[str], list[str]]:
    changes = []
    for p in walk(root):
        t = read(p)
        if t is None:
            continue
        new = t
        if p.name == "pom.xml":
            for prop in BOOT_MANAGED_PROPS:
                new = re.sub(r"^[ \t]*<" + re.escape(prop) + r">[^<]*</" + re.escape(prop) + r">[ \t]*\r?\n", "", new, flags=re.M)
        elif p.name == "gradle.properties":
            for prop in BOOT_MANAGED_PROPS:
                new = re.sub(r"^\s*" + re.escape(prop) + r"\s*[=:].*\r?\n?", "", new, flags=re.M)
        elif p.name in ("build.gradle", "build.gradle.kts"):
            for prop in BOOT_MANAGED_PROPS:
                new = re.sub(r"^\s*(?:ext|extra)\s*\[\s*['\"]" + re.escape(prop) + r"['\"]\s*\]\s*=.*\r?\n?", "", new, flags=re.M)
        if write_if_changed(p, t, new):
            removed = [x for x in BOOT_MANAGED_PROPS if x in t and x not in new]
            changes.append(f"{rel(p, root)}: removed stale Boot-managed version pin(s): {', '.join(removed)}")
    return changes, []


# ---- ID generation ----------------------------------------------------------
_NUMERIC_ID = r"(?:java\.lang\.)?(?:Long|long|Integer|int|Short|short)|(?:java\.math\.)?(?:BigInteger|BigDecimal)"


def _datasource_kind(root: Path) -> str:
    urls = " ".join(e["value"] for f in config_files(root) for e in ConfigFile(f).entries() if "url" in e["key"].lower())
    for p in walk(root):
        if p.name == "persistence.xml":
            urls += " " + (read(p) or "")
    if re.search(r"jdbc:(mysql|mariadb|sqlserver|jtds|sybase)", urls):
        return "identity-db"
    return "sequence-db"


def _new_generator_mappings_off(root: Path) -> bool:
    for f in config_files(root):
        for e in ConfigFile(f).entries():
            if "new_generator_mappings" in e["key"] and e["value"].strip().lower() == "false":
                return True
    return False


def cm_pin_generated_ids(root: Path, mode: str = "hibernate5") -> tuple[list[str], list[str]]:
    """Make implicit id generation explicit so Hibernate 6 keeps using the existing sequence/table.
    mode 'hibernate5': reproduce Hibernate 5.6 (Boot 2.7) AUTO/SEQUENCE defaults (hibernate_sequence, increment 1).
    mode 'openjpa': reproduce OpenJPA's default table sequence (OPENJPA_SEQUENCE_TABLE)."""
    changes, todo = [], []
    identity = mode == "hibernate5" and _new_generator_mappings_off(root) and _datasource_kind(root) == "identity-db"
    for p in java_sources(root, main_only=True):
        t = read(p) or ""
        if "GeneratedValue" not in t:
            continue
        pkg = persistence_pkg(t)
        new, n = t, 0
        for s, e, args in sorted(annotation_spans(t, "GeneratedValue"), reverse=True):
            strategy = (ann_attr(args, "strategy") or "AUTO").split(".")[-1]
            if ann_attr(args, "generator") or strategy not in ("AUTO", "SEQUENCE"):
                continue
            tail = new[e:e + 400]
            stop = re.search(r"[;{]", tail)
            decl = tail[:stop.end()] if stop else tail
            decl = re.sub(r"@\w+(?:\s*\([^)]*\))?", " ", decl)
            if not re.search(r"\b(?:" + _NUMERIC_ID + r")\s+\w+\s*(?:[;=]|\(\s*\))", decl):
                continue
            ind = re.match(r"[ \t]*", new[new.rfind("\n", 0, s) + 1:]).group(0)
            if mode == "openjpa":
                rep = (f'@GeneratedValue(strategy = GenerationType.TABLE, generator = "openjpa_sequence")\n{ind}'
                       f'@TableGenerator(name = "openjpa_sequence", table = "OPENJPA_SEQUENCE_TABLE", pkColumnName = "ID", '
                       f'valueColumnName = "SEQUENCE_VALUE", pkColumnValue = "0", allocationSize = 50)')
                extra = [f"{pkg}.TableGenerator"]
            elif identity:
                rep = "@GeneratedValue(strategy = GenerationType.IDENTITY)"
                extra = []
            else:
                rep = (f'@GeneratedValue(strategy = GenerationType.SEQUENCE, generator = "hibernate_sequence")\n{ind}'
                       f'@SequenceGenerator(name = "hibernate_sequence", sequenceName = "hibernate_sequence", allocationSize = 1)')
                extra = [f"{pkg}.SequenceGenerator"]
            new = new[:s] + rep + new[e:]
            for imp in [f"{pkg}.GenerationType"] + extra:
                new = add_import(new, imp)
            n += 1
        if n and write_if_changed(p, t, new):
            what = {"openjpa": "OPENJPA_SEQUENCE_TABLE (OpenJPA default)", "hibernate5": "IDENTITY" if identity else "hibernate_sequence, increment 1 (Hibernate 5 default)"}[mode]
            changes.append(f"{rel(p, root)}: pinned {n} implicit @GeneratedValue to {what}")
    if changes and mode == "openjpa":
        todo.append("ID generation now reads OpenJPA's OPENJPA_SEQUENCE_TABLE through a Hibernate @TableGenerator (pooled-lo). "
                    "On PostgreSQL the ID column is SMALLINT while Hibernate binds pkColumnValue as a string: add `stringtype=unspecified` "
                    "to the JDBC URL or change the column type. Verify the next id against a copy of production.")
    return changes, todo


# ---- JPQL '?' parameters ------------------------------------------------------
def _number_qmarks(literal_body: str, counter: list) -> str:
    out, inq = [], False
    for i, c in enumerate(literal_body):
        if c == "'":
            inq = not inq
        if c == "?" and not inq and not (i + 1 < len(literal_body) and literal_body[i + 1].isdigit()):
            counter[0] += 1
            out.append(f"?{counter[0]}")
        else:
            out.append(c)
    return "".join(out)


def _renumber_args(args: str) -> str:
    if re.search(r"\?\d", args):
        return args  # already numbered (or mixed): leave alone
    counter, out, j = [0], [], 0
    while j < len(args):
        if args[j] == '"':
            k = skip_java_literal(args, j)
            lit = args[j:k]
            q = 3 if lit.startswith('"""') else 1
            out.append(lit[:q] + _number_qmarks(lit[q:-q], counter) + lit[-q:])
            j = k
            continue
        out.append(args[j]); j += 1
    return "".join(out)


def cm_jpql_positional(root: Path) -> tuple[list[str], list[str]]:
    changes = []
    for p in walk(root):
        t = read(p)
        if not t or "?" not in t:
            continue
        new = t
        if p.suffix in (".java", ".kt"):
            spans = []
            for m in re.finditer(r"\bcreateQuery\s*\(|@(?:NamedQuery|Query)\s*\(", t):
                k = t.index("(", m.start())
                e = balanced(t, k)
                if e == -1:
                    continue
                args = t[k + 1:e]
                if m.group(0).startswith("@Query") and re.search(r"nativeQuery\s*=\s*true", args):
                    continue
                if m.group(0).startswith("@NamedQuery"):
                    q = ann_attr(args, "query")
                    if q is None:
                        continue
                spans.append((k + 1, e))
            for a, b in sorted(spans, reverse=True):
                new = new[:a] + _renumber_args(new[a:b]) + new[b:]
        elif p.name == "orm.xml" or (p.suffix == ".xml" and "<named-query" in t):
            new = re.sub(r"(<named-query\b[^>]*>\s*<query>)(.*?)(</query>)",
                         lambda m: m.group(1) + (_number_qmarks(m.group(2), [0]) if not re.search(r"\?\d", m.group(2)) else m.group(2)) + m.group(3),
                         new, flags=re.S)
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: JDBC-style '?' JPQL parameters numbered (?1, ?2, ...)")
    return changes, []


# ---- Netflix OSS --------------------------------------------------------------
HYSTRIX_PROP_MAP = {
    "circuitBreaker.requestVolumeThreshold": ("circuitbreaker", "minimum-number-of-calls", ""),
    "circuitBreaker.errorThresholdPercentage": ("circuitbreaker", "failure-rate-threshold", ""),
    "circuitBreaker.sleepWindowInMilliseconds": ("circuitbreaker", "wait-duration-in-open-state", "ms"),
    "metrics.rollingStats.timeInMilliseconds": ("circuitbreaker", "sliding-window-size", "s"),
    "execution.isolation.thread.timeoutInMilliseconds": ("timelimiter", "timeout-duration", "ms"),
}


def _r4j_props(scope: str, name: str, prop: str, value: str):
    """-> list of (key, value) or None if unmapped. scope: 'instances' or 'configs'."""
    m = HYSTRIX_PROP_MAP.get(prop)
    if not m:
        return None
    kind, key, unit = m
    base = f"resilience4j.{kind}.{scope}.{name}"
    if unit == "ms":
        return [(f"{base}.{key}", f"{value}ms")]
    if unit == "s":
        try:
            secs = max(1, int(value) // 1000)
        except ValueError:
            return None
        return [(f"{base}.sliding-window-type", "TIME_BASED"), (f"{base}.{key}", str(secs))]
    return [(f"{base}.{key}", value)]


def _method_name_after(text: str, pos: int) -> str | None:
    for m in re.finditer(r"(@)?\b([A-Za-z_]\w*)\s*\(", text[pos:pos + 2000]):
        if m.group(1):
            k = pos + m.end() - 1
            e = balanced(text, k)
            continue
        if m.group(2) in ("if", "for", "while", "switch", "catch", "synchronized", "return", "new"):
            continue
        return m.group(2)
    return None


def _add_throwable_param(text: str, method: str) -> str:
    for m in re.finditer(r"(?<![.\w\"])(?:[\w<>\[\],.?]+\s+)" + re.escape(method) + r"\s*\(", text):
        k = m.end() - 1
        e = balanced(text, k)
        if e == -1:
            continue
        after = text[e + 1:e + 200].lstrip()
        if not (after.startswith("{") or after.startswith("throws")):
            continue  # a call, not a declaration
        params = text[k + 1:e]
        if re.search(r"\b(Throwable|Exception|\w+Exception)\s+\w+\s*$", params.strip()):
            return text
        new_params = (params.rstrip() + ", Throwable throwable") if params.strip() else "Throwable throwable"
        return text[:k + 1] + new_params + text[e:]
    return text


def cm_hystrix(root: Path) -> tuple[list[str], list[str]]:
    changes, todo, props, mods = [], [], [], set()
    for p in java_sources(root):
        t = read(p) or ""
        if "Hystrix" not in t and "EnableCircuitBreaker" not in t:
            continue
        new = t
        for ann in ("EnableHystrix", "EnableCircuitBreaker", "EnableHystrixDashboard"):
            new, _ = remove_annotation(new, ann)
        for s, e, args in sorted(annotation_spans(new, "HystrixCommand"), reverse=True):
            method = _method_name_after(new, e)
            name = unquote_java(ann_attr(args, "commandKey")) or method or "default"
            fb = unquote_java(ann_attr(args, "fallbackMethod"))
            for pm in re.finditer(r"@HystrixProperty\s*\(\s*name\s*=\s*\"([^\"]+)\"\s*,\s*value\s*=\s*\"([^\"]*)\"\s*\)", args or ""):
                mapped = _r4j_props("instances", name, pm.group(1), pm.group(2))
                if mapped:
                    props += [(k, v, module_of(p, root)) for k, v in mapped]
                else:
                    todo.append(f"{rel(p, root)}: Hystrix property {pm.group(1)}={pm.group(2)} on {method}() has no Resilience4j equivalent")
            ign = ann_attr(args, "ignoreExceptions")
            if ign:
                for i, c in enumerate(re.findall(r"([\w.]+)\.class", ign)):
                    fq = c if "." in c else next((x for x in java_imports(new) if x.endswith("." + c)), None)
                    if not fq and c in ("RuntimeException", "IllegalArgumentException", "IllegalStateException", "Exception"):
                        fq = "java.lang." + c
                    if not fq:
                        pkgm = re.search(r"^\s*package\s+([\w.]+)\s*;", new, re.M)
                        fq = f"{pkgm.group(1)}.{c}" if pkgm else c
                    props.append((f"resilience4j.circuitbreaker.instances.{name}.ignore-exceptions[{i}]", fq, module_of(p, root)))
            rep = f'@CircuitBreaker(name = "{name}"' + (f', fallbackMethod = "{fb}"' if fb else "") + ")"
            new = new[:s] + rep + new[e:]
            if fb:
                new = _add_throwable_param(new, fb)
            new = add_import(new, "io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker")
        new = remove_import(new, r"com\.netflix\.hystrix\.[\w.]+|org\.springframework\.cloud\.netflix\.hystrix\.[\w.]+|"
                                 r"org\.springframework\.cloud\.client\.circuitbreaker\.EnableCircuitBreaker")
        if write_if_changed(p, t, new):
            mods.add(module_of(p, root))
            changes.append(f"{rel(p, root)}: Hystrix -> Resilience4j (@CircuitBreaker, fallback signature)")
        if re.search(r"\bHystrix\w*\b", re.sub(r"^\s*import.*$", "", new, flags=re.M)):
            todo.append(f"{rel(p, root)}: still references Hystrix API (HystrixRequestContext, HystrixCommand subclasses...) - rewrite with CircuitBreakerFactory")
    # configuration files: hystrix.command.<key>.<prop>
    for f in config_files(root):
        c = ConfigFile(f)
        hit = False
        for e in c.keys_matching(r"hystrix\."):
            m = re.match(r"hystrix\.command\.([\w-]+)\.(.+)$", e["key"])
            mapped = None
            if m:
                scope = "configs" if m.group(1) == "default" else "instances"
                mapped = _r4j_props(scope, m.group(1), m.group(2), e["value"])
            doc = c.remove(e["key"])
            hit = True
            if mapped:
                for k, v in mapped:
                    c.put(k, v, doc=doc or 0)
            else:
                todo.append(f"{rel(f, root)}: dropped Hystrix setting {e['key']}={e['value']} (no Resilience4j equivalent)")
        for e in c.keys_matching(r"feign\.hystrix\.enabled$"):
            doc = c.remove(e["key"]); hit = True
            c.put("spring.cloud.openfeign.circuitbreaker.enabled", e["value"], doc=doc or 0)
        if hit and c.save():
            mods.add(module_of(f, root))
            changes.append(f"{rel(f, root)}: Hystrix settings -> resilience4j.*")
    if props:
        by_mod: dict[Path, list] = {}
        for k, v, m in props:
            by_mod.setdefault(m, []).append((k, v))
        for m, kv in by_mod.items():
            c = main_config(root, m)
            for k, v in kv:
                if c.get(k) is None:
                    c.put(k, v)
            if c.save():
                changes.append(f"{rel(c.path, root)}: @HystrixProperty settings -> resilience4j.*")
    # build files
    for bf in list(build_files(root)):
        t = read(bf) or ""
        new, removed = build_remove_deps(bf, t, lambda g, a: "hystrix" in (a or "").lower() or (g or "") == "com.netflix.hystrix")
        if not removed and write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: removed Hystrix from <dependencyManagement>")
            continue
        if removed or (module_of(bf, root) in mods and changes):
            new = build_add_dep(bf, new, "org.springframework.cloud", "spring-cloud-starter-circuitbreaker-resilience4j")
            new = build_add_dep(bf, new, "org.springframework.boot", "spring-boot-starter-aop")
            inherited = reactor_text(root, bf)
            if bf.name == "pom.xml" and not re.search(r"spring-cloud-dependencies|spring-cloud-starter-parent", new + inherited):
                top = reactor_ancestors(root, bf)[-1]
                if top == bf:
                    new = pom_set_property(new, "spring-cloud.version", cloud_version_for("3.5"))
                    new = pom_ensure_bom(new, "org.springframework.cloud", "spring-cloud-dependencies", "${spring-cloud.version}")
                else:
                    tt = read(top) or ""
                    tt2 = pom_ensure_bom(pom_set_property(tt, "spring-cloud.version", cloud_version_for("3.5")),
                                         "org.springframework.cloud", "spring-cloud-dependencies", "${spring-cloud.version}")
                    if write_if_changed(top, tt, tt2):
                        changes.append(f"{rel(top, root)}: added the Spring Cloud BOM (inherited by {rel(bf, root)})")
            if write_if_changed(bf, t, new):
                changes.append(f"{rel(bf, root)}: {', '.join(removed) or 'Hystrix'} -> spring-cloud-starter-circuitbreaker-resilience4j + spring-boot-starter-aop")
    if changes:
        todo.append("Hystrix -> Resilience4j: Resilience4j does not time out synchronous calls (Hystrix did, default 1s) and runs on the caller "
                    "thread (no thread-pool isolation). Add @TimeLimiter/@Bulkhead where you relied on that.")
    return changes, todo


def _ribbon_rule_bean(method: str, kind: str) -> str:
    cls = "RandomLoadBalancer" if kind == "Random" else "RoundRobinLoadBalancer"
    return (f"@Bean\n    public ReactorLoadBalancer<ServiceInstance> {method}(Environment environment, LoadBalancerClientFactory factory) {{\n"
            f"        String name = environment.getProperty(LoadBalancerClientFactory.PROPERTY_NAME);\n"
            f"        return new {cls}(factory.getLazyProvider(name, ServiceInstanceListSupplier.class), name);\n    }}")


def cm_ribbon(root: Path) -> tuple[list[str], list[str]]:
    changes, todo, mods = [], [], set()
    for p in java_sources(root):
        t = read(p) or ""
        if "Ribbon" not in t and "netflix.loadbalancer" not in t:
            continue
        new = t
        new = re.sub(r"@(?:[\w.]+\.)?RibbonClients\b", "@LoadBalancerClients", new)
        new = re.sub(r"@(?:[\w.]+\.)?RibbonClient\b", "@LoadBalancerClient", new)
        if "@LoadBalancerClient" in new:
            new = add_import(new, "org.springframework.cloud.loadbalancer.annotation.LoadBalancerClient")
            if "@LoadBalancerClients" in new:
                new = add_import(new, "org.springframework.cloud.loadbalancer.annotation.LoadBalancerClients")
        # IRule beans with a direct LoadBalancer equivalent
        rx = re.compile(r"@Bean\s+(?:public\s+|protected\s+)?IRule\s+(\w+)\s*\(\s*\)\s*\{\s*return\s+new\s+(Random|RoundRobin)Rule\s*\(\s*\)\s*;\s*\}")
        if rx.search(new):
            new = rx.sub(lambda m: _ribbon_rule_bean(m.group(1), m.group(2)), new)
            for imp in ("org.springframework.cloud.client.ServiceInstance", "org.springframework.cloud.loadbalancer.core.ReactorLoadBalancer",
                        "org.springframework.cloud.loadbalancer.core.RandomLoadBalancer", "org.springframework.cloud.loadbalancer.core.RoundRobinLoadBalancer",
                        "org.springframework.cloud.loadbalancer.core.ServiceInstanceListSupplier",
                        "org.springframework.cloud.loadbalancer.support.LoadBalancerClientFactory", "org.springframework.core.env.Environment"):
                new = add_import(new, imp)
            new = drop_unused_imports(new, ["org.springframework.cloud.loadbalancer.core.RandomLoadBalancer",
                                            "org.springframework.cloud.loadbalancer.core.RoundRobinLoadBalancer"])
        new = remove_import(new, r"org\.springframework\.cloud\.netflix\.ribbon\.[\w.]+|com\.netflix\.loadbalancer\.(?:Random|RoundRobin)Rule|com\.netflix\.loadbalancer\.IRule")
        if write_if_changed(p, t, new):
            mods.add(module_of(p, root))
            changes.append(f"{rel(p, root)}: Ribbon -> Spring Cloud LoadBalancer")
        if re.search(r"\b(IRule|IPing|ServerList|ILoadBalancer|com\.netflix\.loadbalancer)\b", re.sub(r"^\s*import.*$", "", new, flags=re.M)):
            todo.append(f"{rel(p, root)}: custom Ribbon IRule/IPing/ServerList - implement ReactorServiceInstanceLoadBalancer / ServiceInstanceListSupplier")
    for f in config_files(root):
        c = ConfigFile(f)
        hit = False
        for e in c.keys_matching(r"(?:[\w-]+\.)?ribbon\.|spring\.cloud\.loadbalancer\.ribbon\."):
            m = re.match(r"([\w-]+)\.ribbon\.listOfServers$", e["key"])
            doc = c.remove(e["key"]); hit = True
            if m:
                for i, srv in enumerate(x.strip() for x in e["value"].split(",") if x.strip()):
                    c.put(f"spring.cloud.discovery.client.simple.instances.{m.group(1)}[{i}].uri",
                          srv if "://" in srv else f"http://{srv}", doc=doc or 0)
            elif not re.search(r"ribbon\.(eureka\.enabled|enabled)$", e["key"]):
                todo.append(f"{rel(f, root)}: dropped Ribbon setting {e['key']}={e['value']} (configure spring.cloud.loadbalancer.* / client timeouts instead)")
        if hit and c.save():
            mods.add(module_of(f, root))
            changes.append(f"{rel(f, root)}: Ribbon settings -> spring.cloud.discovery.client.simple / removed")
    for bf in list(build_files(root)):
        t = read(bf) or ""
        new, removed = build_remove_deps(bf, t, lambda g, a: "ribbon" in (a or "").lower())
        if not removed and write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: removed Ribbon from <dependencyManagement>")
        if removed:
            new = build_add_dep(bf, new, "org.springframework.cloud", "spring-cloud-starter-loadbalancer")
            if write_if_changed(bf, t, new):
                changes.append(f"{rel(bf, root)}: {', '.join(removed)} -> spring-cloud-starter-loadbalancer")
    return changes, todo


def _strip_prefix_count(path: str) -> int:
    segs = [s for s in path.split("/") if s]
    n = 0
    for s in segs:
        if "*" in s or "{" in s:
            break
        n += 1
    return n


def cm_zuul(root: Path) -> tuple[list[str], list[str]]:
    changes, todo = [], []
    zuul_dep = False
    for bf in list(build_files(root)):
        t = read(bf) or ""
        new, removed = build_remove_deps(bf, t, lambda g, a: "zuul" in (a or "").lower())
        if not removed and write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: removed Zuul from <dependencyManagement>")
        if removed:
            zuul_dep = True
            new = build_add_dep(bf, new, "org.springframework.cloud", "spring-cloud-starter-gateway-server-webmvc",
                                version=latest_patch("org.springframework.cloud", "spring-cloud-starter-gateway-server-webmvc", "4.3", "4.3.0"))
            if write_if_changed(bf, t, new):
                changes.append(f"{rel(bf, root)}: {', '.join(removed)} -> spring-cloud-starter-gateway-server-webmvc (servlet, like Zuul 1)")
    for p in java_sources(root):
        t = read(p) or ""
        if "Zuul" not in t:
            continue
        new = t
        for ann in ("EnableZuulProxy", "EnableZuulServer"):
            new, _ = remove_annotation(new, ann)
        new = remove_import(new, r"org\.springframework\.cloud\.netflix\.zuul\.Enable\w+")
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: removed @EnableZuulProxy (Gateway MVC is auto-configured)")
        if re.search(r"\bZuulFilter\b|RequestContext\.getCurrentContext", new):
            todo.append(f"{rel(p, root)}: ZuulFilter - port to a Gateway MVC filter function (HandlerFilterFunction / BeforeFilterFunctions)")
    for f in config_files(root):
        c = ConfigFile(f)
        ent = c.keys_matching(r"zuul\.")
        if not ent:
            continue
        routes: dict[str, dict] = {}
        glob: dict[str, str] = {}
        doc = 0
        for e in ent:
            m = re.match(r"zuul\.routes\.([\w-]+)(?:\.([\w-]+))?$", e["key"])
            if m:
                r = routes.setdefault(m.group(1), {})
                r[(m.group(2) or "path").replace("-", "").lower()] = e["value"]
            else:
                glob[e["key"].replace("-", "").lower()] = e["value"]
            doc = c.remove(e["key"]) or doc
        prefix = glob.get("zuul.prefix", "").rstrip("/")
        global_strip = glob.get("zuul.stripprefix", "true").lower() != "false"
        sensitive = glob.get("zuul.sensitiveheaders")
        base = "spring.cloud.gateway.server.webmvc.routes"
        for i, (rid, r) in enumerate(routes.items()):
            path = prefix + (r.get("path") or f"/{rid}/**")
            uri = r.get("url") or f"lb://{r.get('serviceid') or rid}"
            c.put(f"{base}[{i}].id", rid, doc=doc)
            c.put(f"{base}[{i}].uri", uri, doc=doc)
            c.put(f"{base}[{i}].predicates[0]", f"Path={path}", doc=doc)
            filters = []
            strip = r.get("stripprefix", "true").lower() != "false"
            n = (_strip_prefix_count(r.get("path") or f"/{rid}/**") if strip else 0) + (_strip_prefix_count(prefix) if global_strip and prefix else 0)
            if n:
                filters.append(f"StripPrefix={n}")
            sens = r.get("sensitiveheaders", sensitive)
            hdrs = ["Cookie", "Set-Cookie", "Authorization"] if sens is None else [h.strip() for h in sens.split(",") if h.strip()]
            for h in hdrs:
                filters.append(("RemoveResponseHeader=" if h.lower() == "set-cookie" else "RemoveRequestHeader=") + h)
            for j, fl in enumerate(filters):
                c.put(f"{base}[{i}].filters[{j}]", fl, doc=doc)
            if uri.startswith("lb://"):
                for bf in build_files(root):
                    bt = read(bf) or ""
                    if "gateway-server-webmvc" in bt:
                        write_if_changed(bf, bt, build_add_dep(bf, bt, "org.springframework.cloud", "spring-cloud-starter-loadbalancer"))
        for k, v in glob.items():
            if k not in ("zuul.prefix", "zuul.stripprefix", "zuul.sensitiveheaders"):
                todo.append(f"{rel(f, root)}: dropped Zuul setting {k}={v} - no direct Gateway MVC equivalent")
        if c.save():
            changes.append(f"{rel(f, root)}: {len(routes)} Zuul route(s) -> spring.cloud.gateway.server.webmvc.routes "
                           f"(StripPrefix and Zuul's default sensitive-header stripping kept)")
    if zuul_dep and not any("routes" in c for c in changes):
        todo.append("Zuul auto-routed every discovered service (/<serviceId>/**). Gateway MVC does not: add explicit routes for each service you exposed.")
    return changes, todo


def cm_netflix(root: Path):
    c1, t1 = cm_hystrix(root)
    c2, t2 = cm_ribbon(root)
    c3, t3 = cm_zuul(root)
    return c1 + c2 + c3, t1 + t2 + t3


# ---- legacy spring-security-oauth2 ------------------------------------------
LEGACY_OAUTH_DEP = lambda g, a: (g or "") in ("org.springframework.security.oauth", "org.springframework.security.oauth.boot") or a == "spring-security-jwt"

JWT_CONVERTER_BEAN = """
    /**
     * Added by spring-migrate: tokens from spring-security-oauth2 carry roles in an "authorities" claim; Spring Security 6
     * only maps "scope". This keeps both so existing hasRole/hasAuthority rules keep working.
     */
    @Bean
    public JwtAuthenticationConverter jwtAuthenticationConverter() {
        JwtGrantedAuthoritiesConverter scopes = new JwtGrantedAuthoritiesConverter();
        JwtGrantedAuthoritiesConverter authorities = new JwtGrantedAuthoritiesConverter();
        authorities.setAuthoritiesClaimName("authorities");
        authorities.setAuthorityPrefix("");
        JwtAuthenticationConverter converter = new JwtAuthenticationConverter();
        converter.setJwtGrantedAuthoritiesConverter(jwt -> {
            Collection<GrantedAuthority> all = new ArrayList<>(scopes.convert(jwt));
            if (jwt.hasClaim("authorities")) {
                all.addAll(authorities.convert(jwt));
            }
            return all;
        });
        return converter;
    }
"""

HMAC_DECODER_BEAN = """
    /** Added by spring-migrate: the legacy resource server verified HMAC-signed JWTs with a shared key. */
    @Bean
    public JwtDecoder jwtDecoder(@Value("${app.legacy-oauth2.jwt.hmac-secret}") String secret) {
        return NimbusJwtDecoder.withSecretKey(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256")).build();
    }
"""


def _legacy_rs_mode(root: Path) -> tuple[str, list[str]]:
    """Map security.oauth2.resource.* to spring.security.oauth2.resourceserver.*; returns ('jwt'|'opaque'|'hmac', changes)."""
    changes, mode = [], "jwt"
    for f in config_files(root):
        c = ConfigFile(f)
        ent = {e["key"].replace("-", "").lower(): e for e in c.keys_matching(r"security\.oauth2\.")}
        if not ent:
            continue
        doc = 0
        def take(k):
            nonlocal doc
            e = ent.get(k)
            if e:
                doc = c.remove(e["key"]) or doc
                return e["value"]
            return None
        jwk = take("security.oauth2.resource.jwk.keyseturi")
        keyval = take("security.oauth2.resource.jwt.keyvalue")
        keyuri = take("security.oauth2.resource.jwt.keyuri")
        info = take("security.oauth2.resource.tokeninfouri")
        rid = take("security.oauth2.resource.id") or take("security.oauth2.resource.resourceid")
        cid = take("security.oauth2.client.clientid")
        csec = take("security.oauth2.client.clientsecret")
        tok = take("security.oauth2.client.accesstokenuri")
        auth = take("security.oauth2.client.userauthorizationuri")
        scope = take("security.oauth2.client.scope")
        grant = take("security.oauth2.client.granttype")
        userinfo = take("security.oauth2.resource.userinfouri")
        take("security.oauth2.resource.prefertokeninfo")
        rs = "spring.security.oauth2.resourceserver"
        if jwk:
            c.put(f"{rs}.jwt.jwk-set-uri", jwk, doc=doc)
        elif keyval and "BEGIN PUBLIC KEY" in keyval:
            pem = f.parent / "legacy-oauth2-public.pem"
            write_file(pem, keyval.replace("\\n", "\n").strip() + "\n")
            c.put(f"{rs}.jwt.public-key-location", f"classpath:{pem.name}", doc=doc)
        elif keyval:
            mode = "hmac"
            c.put("app.legacy-oauth2.jwt.hmac-secret", keyval, doc=doc)
        elif keyuri:
            c.put(f"{rs}.jwt.jwk-set-uri", re.sub(r"/oauth/token_key/?$", "/oauth2/jwks", keyuri), doc=doc,
                  comment="spring-migrate: legacy /oauth/token_key is not JWKS; this assumes the authorization server was migrated to Spring Authorization Server")
        elif info:
            mode = "opaque"
            c.put(f"{rs}.opaquetoken.introspection-uri", info, doc=doc)
            if cid:
                c.put(f"{rs}.opaquetoken.client-id", cid, doc=doc)
            if csec:
                c.put(f"{rs}.opaquetoken.client-secret", csec, doc=doc)
        if rid and mode != "opaque":
            c.put(f"{rs}.jwt.audiences", rid, doc=doc)
        if tok or auth:
            reg, prov = "spring.security.oauth2.client.registration.legacy", "spring.security.oauth2.client.provider.legacy"
            if cid:
                c.put(f"{reg}.client-id", cid, doc=doc)
            if csec:
                c.put(f"{reg}.client-secret", csec, doc=doc)
            c.put(f"{reg}.provider", "legacy", doc=doc)
            c.put(f"{reg}.authorization-grant-type", grant or ("authorization_code" if auth else "client_credentials"), doc=doc)
            if scope:
                c.put(f"{reg}.scope", scope, doc=doc)
            if auth:
                c.put(f"{reg}.redirect-uri", "{baseUrl}/login/oauth2/code/{registrationId}", doc=doc)
                c.put(f"{prov}.authorization-uri", auth, doc=doc)
            if tok:
                c.put(f"{prov}.token-uri", tok, doc=doc)
            if userinfo:
                c.put(f"{prov}.user-info-uri", userinfo, doc=doc)
                c.put(f"{prov}.user-name-attribute", "name", doc=doc)
        if c.save():
            changes.append(f"{rel(f, root)}: security.oauth2.* -> spring.security.oauth2.* ({mode})")
    return mode, changes


def _rs_chain_method(body: str, mode: str) -> str:
    dsl = {"jwt": "oauth2.jwt(Customizer.withDefaults())", "hmac": "oauth2.jwt(Customizer.withDefaults())",
           "opaque": "oauth2.opaqueToken(Customizer.withDefaults())"}[mode]
    body = body.rstrip()
    if body and not body.endswith((";", "}")):
        body += ";"
    return ("@Bean\n    public SecurityFilterChain resourceServerSecurityFilterChain(HttpSecurity http) throws Exception {\n"
            + (body + "\n" if body.strip() else "        http.authorizeRequests().anyRequest().authenticated();\n")
            + f"        http.oauth2ResourceServer(oauth2 -> {dsl});\n        return http.build();\n    }}")


def cm_legacy_oauth2(root: Path) -> tuple[list[str], list[str]]:
    changes, todo = [], []
    srcs = [p for p in java_sources(root) if re.search(r"org\.springframework\.security\.oauth2\.(?:config|provider|client)|@EnableOAuth2Sso|#oauth2\.", read(p) or "")]
    has_rs = any("ResourceServerConfigurerAdapter" in (read(p) or "") or "@EnableResourceServer" in (read(p) or "") for p in srcs)
    has_as = any("AuthorizationServerConfigurerAdapter" in (read(p) or "") or "@EnableAuthorizationServer" in (read(p) or "") for p in srcs)
    has_sso = any("@EnableOAuth2Sso" in (read(p) or "") or "OAuth2RestTemplate" in (read(p) or "") for p in srcs)
    if not (srcs or any(LEGACY_OAUTH_DEP(g, a) for bf in build_files(root) for _, _, g, a in pom_dependency_blocks(read(bf) or ""))):
        return [], []
    mode = "jwt"
    if has_rs or has_sso:
        mode, cc = _legacy_rs_mode(root)
        changes += cc
    for p in srcs:
        t = read(p) or ""
        new = t
        # SpEL: #oauth2.hasScope('x') -> hasAuthority('SCOPE_x')
        new = re.sub(r"#oauth2\.hasScope\(\s*'([^']+)'\s*\)", r"hasAuthority('SCOPE_\1')", new)
        new = re.sub(r"#oauth2\.hasAnyScope\(([^)]*)\)", lambda m: "hasAnyAuthority(" + re.sub(r"'([^']+)'", r"'SCOPE_\1'", m.group(1)) + ")", new)
        new = re.sub(r"#oauth2\.(?:isOAuth|isUser|isClient)\(\)", "isAuthenticated()", new)
        if "ResourceServerConfigurerAdapter" in new:
            new, _ = remove_annotation(new, "EnableResourceServer")
            new = re.sub(r"\s+extends\s+ResourceServerConfigurerAdapter\b", "", new)
            # drop configure(ResourceServerSecurityConfigurer)
            for m in reversed(list(re.finditer(r"(?:@Override\s+)?public\s+void\s+configure\s*\(\s*(?:final\s+)?ResourceServerSecurityConfigurer\s+\w+\s*\)(?:\s*throws\s+[\w.]+)?\s*\{", new))):
                e = balanced(new, m.end() - 1, "{", "}")
                body = new[m.end():e]
                rid = re.search(r"\.resourceId\(\s*\"([^\"]+)\"", body)
                if rid:
                    c = main_config(root, module_of(p, root))
                    if c.get("spring.security.oauth2.resourceserver.jwt.audiences") is None:
                        c.put("spring.security.oauth2.resourceserver.jwt.audiences", rid.group(1))
                        c.save()
                if re.sub(r"\w+\.resourceId\([^)]*\)\s*;|\s", "", body):
                    todo.append(f"{rel(p, root)}: ResourceServerSecurityConfigurer settings besides resourceId were dropped: {body.strip()[:120]}")
                s = new.rfind("\n", 0, m.start()) + 1
                new = new[:s] + new[e + 2:] if new[e + 1:e + 2] == "\n" else new[:s] + new[e + 1:]
            m = re.search(r"(?:@Override\s+)?public\s+void\s+configure\s*\(\s*(?:final\s+)?HttpSecurity\s+(\w+)\s*\)(?:\s*throws\s+[\w.]+)?\s*\{", new)
            if m:
                e = balanced(new, m.end() - 1, "{", "}")
                body = new[m.end():e].rstrip()
                if m.group(1) != "http":
                    body = re.sub(r"\b" + m.group(1) + r"\b", "http", body)
                new = new[:m.start()] + _rs_chain_method(body.strip("\n"), mode) + new[e + 1:]
            else:
                cls = re.search(r"\bclass\s+\w+[^{]*\{", new)
                new = new[:cls.end()] + "\n    " + _rs_chain_method("", mode) + "\n" + new[cls.end():]
            if mode != "opaque" and "JwtAuthenticationConverter jwtAuthenticationConverter" not in new:
                last = new.rstrip().rfind("}")
                new = new[:last] + JWT_CONVERTER_BEAN + ("" if mode != "hmac" else HMAC_DECODER_BEAN) + new[last:]
            imps = ["org.springframework.context.annotation.Bean", "org.springframework.security.config.Customizer",
                    "org.springframework.security.web.SecurityFilterChain"]
            if mode != "opaque":
                imps += ["java.util.ArrayList", "java.util.Collection", "org.springframework.security.core.GrantedAuthority",
                         "org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationConverter",
                         "org.springframework.security.oauth2.server.resource.authentication.JwtGrantedAuthoritiesConverter"]
            if mode == "hmac":
                imps += ["java.nio.charset.StandardCharsets", "javax.crypto.spec.SecretKeySpec", "org.springframework.beans.factory.annotation.Value",
                         "org.springframework.security.oauth2.jwt.JwtDecoder", "org.springframework.security.oauth2.jwt.NimbusJwtDecoder"]
            for i in imps:
                new = add_import(new, i)
            new = remove_import(new, r"org\.springframework\.security\.oauth2\.config\.annotation\.web\.(?:configuration|configurers)\.[\w.]+")
        if "@EnableOAuth2Sso" in new:
            new, _ = remove_annotation(new, "EnableOAuth2Sso")
            new = remove_import(new, r"org\.springframework\.boot\.autoconfigure\.security\.oauth2\.client\.EnableOAuth2Sso")
            m = re.search(r"void\s+configure\s*\(\s*(?:final\s+)?HttpSecurity\s+(\w+)\s*\)(?:\s*throws\s+[\w.]+)?\s*\{", new)
            if m and ".oauth2Login(" not in new:
                new = new[:m.end()] + f"\n        {m.group(1)}.oauth2Login(Customizer.withDefaults());" + new[m.end():]
                new = add_import(new, "org.springframework.security.config.Customizer")
            elif not m:
                todo.append(f"{rel(p, root)}: @EnableOAuth2Sso removed - add `.oauth2Login(Customizer.withDefaults())` to your SecurityFilterChain")
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: legacy spring-security-oauth2 -> Spring Security OAuth2 ({mode})")
        if re.search(r"\bOAuth2RestTemplate\b|\bClientCredentialsResourceDetails\b", new):
            todo.append(f"{rel(p, root)}: OAuth2RestTemplate - use RestClient/RestTemplate with OAuth2ClientHttpRequestInterceptor and an OAuth2AuthorizedClientManager")
    for bf in list(build_files(root)):
        t = read(bf) or ""
        if has_as:
            # the legacy server code still needs these jars to compile until cm_authorization_server replaces it
            new = t
            if has_rs:
                new = build_add_dep(bf, new, "org.springframework.boot", "spring-boot-starter-oauth2-resource-server")
            if has_sso:
                new = build_add_dep(bf, new, "org.springframework.boot", "spring-boot-starter-oauth2-client")
            write_if_changed(bf, t, new)
            continue
        new, removed = build_remove_deps(bf, t, LEGACY_OAUTH_DEP)
        if not removed:
            if write_if_changed(bf, t, new):
                changes.append(f"{rel(bf, root)}: removed spring-security-oauth2 from <dependencyManagement>")
            continue
        if has_rs:
            new = build_add_dep(bf, new, "org.springframework.boot", "spring-boot-starter-oauth2-resource-server")
        if has_sso:
            new = build_add_dep(bf, new, "org.springframework.boot", "spring-boot-starter-oauth2-client")
        if write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: removed {', '.join(removed)}; added the Spring Security OAuth2 starter(s)")
    if has_rs and has_as is False and changes:
        todo.append("OAuth2 resource server: tokens are now validated by Spring Security (signature, exp, aud if configured). "
                    "Legacy principals used the `user_name` claim; the new principal name is `sub`.")
    return changes, todo

def _call_args(seg: str, name: str) -> list[str] | None:
    m = re.search(r"\." + name + r"\s*\(", seg)
    if not m:
        return None
    k = m.end() - 1
    e = balanced(seg, k)
    return split_top_level(seg[k + 1:e]) if e != -1 else None


LEGACY_GRANTS = {"client_credentials": "CLIENT_CREDENTIALS", "authorization_code": "AUTHORIZATION_CODE", "refresh_token": "REFRESH_TOKEN"}


def _legacy_clients(body: str):
    """Parse clients.inMemory().withClient(..)...and().withClient(..)... -> list of dicts."""
    m = re.search(r"\.inMemory\s*\(\s*\)", body)
    if not m:
        return None
    chain = body[m.end():]
    end = chain.find(";")
    chain = chain[:end] if end != -1 else chain
    out = []
    for seg in re.split(r"\.withClient\s*\(", chain)[1:]:
        e = balanced("(" + seg, 0)
        cid = seg[:e - 1].strip()
        rest = seg[e:]
        c = {"id": cid, "secret": None, "grants": [], "scopes": [], "redirects": [], "access": None, "refresh": None,
             "auto": None, "authorities": []}
        if (a := _call_args(rest, "secret")):
            c["secret"] = a[0]
        for key, call in (("grants", "authorizedGrantTypes"), ("scopes", "scopes"), ("redirects", "redirectUris"), ("authorities", "authorities")):
            c[key] = [unquote_java(x) for x in (_call_args(rest, call) or [])]
        if (a := _call_args(rest, "accessTokenValiditySeconds")):
            c["access"] = a[0]
        if (a := _call_args(rest, "refreshTokenValiditySeconds")):
            c["refresh"] = a[0]
        if (a := _call_args(rest, "autoApprove")):
            c["auto"] = a
        out.append(c)
    return out


def _sas_client(c: dict, encoder: bool, todo: list, where: str, opaque: bool) -> str:
    sec = c["secret"]
    if sec:
        enc = re.match(r"\w+(?:\(\))?\.encode\s*\(\s*(\"[^\"]*\")\s*\)$", sec)
        if enc:
            sec = f"passwordEncoder.encode({enc.group(1)})" if encoder else '"{noop}' + unquote_java(enc.group(1)) + '"'
    grants = []
    for g in c["grants"]:
        if g in LEGACY_GRANTS:
            grants.append(LEGACY_GRANTS[g])
        else:
            todo.append(f"{where}: client {c['id']}: grant type `{g}` does not exist in OAuth 2.1 / Spring Authorization Server and was dropped "
                        + ("(move clients to authorization_code + PKCE or client_credentials)" if g == "password" else ""))
    L = [f"RegisteredClient.withId({c['id']})", f"                .clientId({c['id']})"]
    if sec:
        L.append(f"                .clientSecret({sec})")
        L.append("                .clientAuthenticationMethod(ClientAuthenticationMethod.CLIENT_SECRET_BASIC)")
        L.append("                .clientAuthenticationMethod(ClientAuthenticationMethod.CLIENT_SECRET_POST)")
    else:
        L.append("                .clientAuthenticationMethod(ClientAuthenticationMethod.NONE)")
    for g in grants:
        L.append(f"                .authorizationGrantType(AuthorizationGrantType.{g})")
    for r in c["redirects"]:
        L.append(f'                .redirectUri("{r}")')
    for s in c["scopes"]:
        L.append(f'                .scope("{s}")')
    # spring-security-oauth2 defaults: access 12h, refresh 30d; consent unless autoApprove(true)
    access = c["access"] or "43200"
    refresh = c["refresh"] or "2592000"
    fmt = "\n                        .accessTokenFormat(OAuth2TokenFormat.REFERENCE)" if opaque else ""
    L.append(f"                .tokenSettings(TokenSettings.builder()\n                        .accessTokenTimeToLive(Duration.ofSeconds({access}))\n"
             f"                        .refreshTokenTimeToLive(Duration.ofSeconds({refresh})){fmt}\n                        .build())")
    auto = c["auto"]
    consent = not (auto and auto[0].strip() == "true")
    if auto and auto[0].strip() not in ("true", "false"):
        todo.append(f"{where}: client {c['id']}: autoApprove({', '.join(auto)}) for some scopes only - consent is now required for all scopes")
    if "AUTHORIZATION_CODE" in grants:
        L.append(f"                .clientSettings(ClientSettings.builder().requireAuthorizationConsent({str(consent).lower()}).build())")
    L.append("                .build()")
    return "\n".join(L)


# Spring Authorization Server 1.x (Boot 3.5) vs. Spring Security 7 (Boot 4), where the configurer moved into spring-security-config
SAS1_CONFIGURER = "org.springframework.security.oauth2.server.authorization.config.annotation.web.configurers.OAuth2AuthorizationServerConfigurer"
SAS7_CONFIGURER = "org.springframework.security.config.annotation.web.configurers.oauth2.server.authorization.OAuth2AuthorizationServerConfigurer"


def fix_sas_configurer_package(root: Path) -> list[str]:
    changes = []
    old_pkg = SAS1_CONFIGURER.rsplit(".", 1)[0] + "."
    new_pkg = SAS7_CONFIGURER.rsplit(".", 1)[0] + "."
    for p in java_sources(root):
        t = read(p) or ""
        if old_pkg in t:
            new = t.replace(old_pkg, new_pkg)
            if write_if_changed(p, t, new):
                changes.append(f"{rel(p, root)}: Authorization Server configurers -> Spring Security 7 package")
    return changes


SAS_IMPORTS = [
    "java.time.Duration", "java.util.UUID", "org.springframework.context.annotation.Bean", "org.springframework.context.annotation.Configuration",
    "org.springframework.core.Ordered", "org.springframework.core.annotation.Order", "org.springframework.http.MediaType",
    "org.springframework.security.config.Customizer", "org.springframework.security.config.annotation.web.builders.HttpSecurity",
    SAS1_CONFIGURER,
    "org.springframework.security.oauth2.core.AuthorizationGrantType", "org.springframework.security.oauth2.core.ClientAuthenticationMethod",
    "org.springframework.security.oauth2.server.authorization.client.InMemoryRegisteredClientRepository",
    "org.springframework.security.oauth2.server.authorization.client.RegisteredClient",
    "org.springframework.security.oauth2.server.authorization.client.RegisteredClientRepository",
    "org.springframework.security.oauth2.server.authorization.settings.AuthorizationServerSettings",
    "org.springframework.security.oauth2.server.authorization.settings.ClientSettings",
    "org.springframework.security.oauth2.server.authorization.settings.OAuth2TokenFormat",
    "org.springframework.security.oauth2.server.authorization.settings.TokenSettings",
    "org.springframework.security.oauth2.server.authorization.token.JwtEncodingContext",
    "org.springframework.security.oauth2.server.authorization.token.OAuth2TokenCustomizer",
    "org.springframework.security.web.SecurityFilterChain",
    "org.springframework.security.web.authentication.LoginUrlAuthenticationEntryPoint",
    "org.springframework.security.web.util.matcher.MediaTypeRequestMatcher",
    "com.nimbusds.jose.jwk.JWKSet", "com.nimbusds.jose.jwk.source.ImmutableJWKSet", "com.nimbusds.jose.jwk.source.JWKSource",
    "com.nimbusds.jose.proc.SecurityContext",
]


def _sas_class(pkg: str, cls: str, fields: list[str], clients: list[str], key: dict, encoder: bool, default_chain: bool) -> str:
    imps = list(SAS_IMPORTS)
    if encoder:
        imps.append("org.springframework.security.crypto.password.PasswordEncoder")
    if key["kind"] == "hmac":
        imps += ["com.nimbusds.jose.JWSAlgorithm", "com.nimbusds.jose.jwk.OctetSequenceKey", "java.nio.charset.StandardCharsets",
                 "javax.crypto.spec.SecretKeySpec", "org.springframework.security.oauth2.jose.jws.MacAlgorithm"]
    else:
        imps += ["com.nimbusds.jose.jwk.RSAKey", "java.security.KeyPair", "java.security.interfaces.RSAPrivateKey", "java.security.interfaces.RSAPublicKey"]
        if key["kind"] == "keystore":
            imps += ["java.io.InputStream", "java.security.KeyStore", "java.security.PrivateKey", "org.springframework.core.io.ClassPathResource"]
        else:
            imps += ["java.security.KeyPairGenerator"]
    if any("@Value" in f for f in fields):
        imps.append("org.springframework.beans.factory.annotation.Value")
    out = [f"package {pkg};", ""] + [f"import {i};" for i in sorted(set(imps))] + ["",
        "/**",
        " * Migrated by spring-migrate from spring-security-oauth2 (@EnableAuthorizationServer) to Spring Authorization Server.",
        " * The legacy endpoint paths (/oauth/token, /oauth/authorize, /oauth/check_token) are kept so clients keep working;",
        " * token lifetimes default to the legacy values (access 12h, refresh 30 days) unless the client set them.",
        " */",
        "@Configuration",
        f"public class {cls} {{"]
    for f in fields:
        out += ["", "    " + f.strip()]
    out += ["",
        "    @Bean",
        "    @Order(Ordered.HIGHEST_PRECEDENCE)",
        "    public SecurityFilterChain authorizationServerSecurityFilterChain(HttpSecurity http) throws Exception {",
        "        OAuth2AuthorizationServerConfigurer authorizationServer = new OAuth2AuthorizationServerConfigurer();",
        "        http.securityMatcher(authorizationServer.getEndpointsMatcher())",
        "                .with(authorizationServer, Customizer.withDefaults())",
        "                .authorizeHttpRequests(authorize -> authorize.anyRequest().authenticated())",
        "                .csrf(csrf -> csrf.ignoringRequestMatchers(authorizationServer.getEndpointsMatcher()))",
        "                .exceptionHandling(exceptions -> exceptions.defaultAuthenticationEntryPointFor(",
        "                        new LoginUrlAuthenticationEntryPoint(\"/login\"), new MediaTypeRequestMatcher(MediaType.TEXT_HTML)));",
        "        return http.build();",
        "    }"]
    if default_chain:
        out += ["",
            "    /** Login for the authorization-code flow (the legacy server relied on Boot's default security). */",
            "    @Bean",
            "    @Order(Ordered.HIGHEST_PRECEDENCE + 1)",
            "    public SecurityFilterChain defaultSecurityFilterChain(HttpSecurity http) throws Exception {",
            "        http.authorizeHttpRequests(authorize -> authorize.anyRequest().authenticated())",
            "                .formLogin(Customizer.withDefaults())",
            "                .httpBasic(Customizer.withDefaults());",
            "        return http.build();",
            "    }"]
    out += ["",
        "    @Bean",
        f"    public RegisteredClientRepository registeredClientRepository({'PasswordEncoder passwordEncoder' if encoder else ''}) {{",
        "        return new InMemoryRegisteredClientRepository(" + ",\n                ".join(clients) + ");",
        "    }",
        ""]
    if key["kind"] == "hmac":
        out += ["    /** Same HMAC key the legacy JwtAccessTokenConverter signed with, so existing resource servers still verify tokens. */",
            "    @Bean",
            "    public JWKSource<SecurityContext> jwkSource() {",
            f"        byte[] secret = ({key['expr']}).getBytes(StandardCharsets.UTF_8);",
            "        OctetSequenceKey jwk = new OctetSequenceKey.Builder(new SecretKeySpec(secret, \"HmacSHA256\"))",
            "                .keyID(UUID.nameUUIDFromBytes(secret).toString()).algorithm(JWSAlgorithm.HS256).build();",
            "        return new ImmutableJWKSet<>(new JWKSet(jwk));",
            "    }",
            "",
            "    @Bean",
            "    public OAuth2TokenCustomizer<JwtEncodingContext> legacyTokenCustomizer() {",
            "        return context -> {",
            "            context.getJwsHeader().algorithm(MacAlgorithm.HS256);",
            "            context.getClaims().claim(\"client_id\", context.getRegisteredClient().getClientId());",
            "        };",
            "    }"]
    else:
        if key["kind"] == "keystore":
            out += ["    /** Same key pair the legacy server loaded from its keystore. */",
                "    @Bean",
                "    public JWKSource<SecurityContext> jwkSource() throws Exception {",
                "        KeyStore keyStore = KeyStore.getInstance(\"" + key.get("type", "JKS") + "\");",
                f"        try (InputStream in = new ClassPathResource({key['path']}).getInputStream()) {{",
                f"            keyStore.load(in, ({key['storepass']}).toCharArray());",
                "        }",
                f"        PrivateKey privateKey = (PrivateKey) keyStore.getKey({key['alias']}, ({key['keypass']}).toCharArray());",
                f"        KeyPair keyPair = new KeyPair(keyStore.getCertificate({key['alias']}).getPublicKey(), privateKey);"]
        else:
            out += ["    /** TODO(spring-migrate): generated key pair - tokens become invalid on restart. Load a persistent key (keystore/vault). */",
                "    @Bean",
                "    public JWKSource<SecurityContext> jwkSource() throws Exception {",
                "        KeyPairGenerator generator = KeyPairGenerator.getInstance(\"RSA\");",
                "        generator.initialize(2048);",
                "        KeyPair keyPair = generator.generateKeyPair();"]
        out += ["        RSAKey rsaKey = new RSAKey.Builder((RSAPublicKey) keyPair.getPublic()).privateKey((RSAPrivateKey) keyPair.getPrivate())",
            "                .keyID(UUID.randomUUID().toString()).build();",
            "        return new ImmutableJWKSet<>(new JWKSet(rsaKey));",
            "    }",
            "",
            "    @Bean",
            "    public OAuth2TokenCustomizer<JwtEncodingContext> legacyTokenCustomizer() {",
            "        return context -> context.getClaims().claim(\"client_id\", context.getRegisteredClient().getClientId());",
            "    }"]
    out += ["",
        "    @Bean",
        "    public AuthorizationServerSettings authorizationServerSettings() {",
        "        return AuthorizationServerSettings.builder()",
        "                .tokenEndpoint(\"/oauth/token\")",
        "                .authorizationEndpoint(\"/oauth/authorize\")",
        "                .tokenIntrospectionEndpoint(\"/oauth/check_token\")",
        "                .tokenRevocationEndpoint(\"/oauth/revoke\")",
        "                .build();",
        "    }",
        "}", ""]
    return "\n".join(out)


def cm_authorization_server(root: Path) -> tuple[list[str], list[str]]:
    """spring-security-oauth2 AuthorizationServerConfigurerAdapter -> Spring Authorization Server configuration."""
    changes, todo = [], []
    srcs = list(java_sources(root, main_only=True))
    for p in srcs:
        t = read(p) or ""
        if "AuthorizationServerConfigurerAdapter" not in t:
            continue
        where = rel(p, root)
        pkg = (re.search(r"^\s*package\s+([\w.]+)\s*;", t, re.M) or [None, ""])[1]
        cls = re.search(r"\bclass\s+(\w+)", t).group(1)
        m = re.search(r"void\s+configure\s*\(\s*(?:final\s+)?ClientDetailsServiceConfigurer\s+(\w+)\s*\)[^{]*\{", t)
        body = t[m.end():balanced(t, m.end() - 1, "{", "}")] if m else ""
        clients = _legacy_clients(body)
        if clients is None:
            todo.append(f"{where}: clients are not configured with clients.inMemory() (JDBC/custom ClientDetailsService) - "
                        f"create a JdbcRegisteredClientRepository with the Spring Authorization Server schema and migrate the rows")
            continue
        # signing key
        key = {"kind": "rsa"}
        sk = re.search(r"\.setSigningKey\s*\(\s*([^;]+?)\s*\)\s*;", t)
        ks = re.search(r"new\s+KeyStoreKeyFactory\s*\(\s*new\s+ClassPathResource\s*\(\s*(\"[^\"]+\")\s*\)\s*,\s*(.+?)\.toCharArray\(\)\s*\)\s*"
                       r"\.getKeyPair\s*\(\s*(\"[^\"]+\")\s*(?:,\s*(.+?)\.toCharArray\(\))?\s*\)", t, re.S)
        if ks:
            key = {"kind": "keystore", "path": ks.group(1), "storepass": ks.group(2), "alias": ks.group(3), "keypass": ks.group(4) or ks.group(2),
                   "type": "PKCS12" if ks.group(1).lower().endswith((".p12", ".pfx")) else "JKS"}
        elif sk and "BEGIN" not in sk.group(1):
            key = {"kind": "hmac", "expr": sk.group(1)}
        elif sk:
            todo.append(f"{where}: PEM signing key - load it into an RSAKey for the JWKSource (generated key used for now)")
        else:
            todo.append(f"{where}: no persistent signing key found - the generated JWKSource uses an in-memory RSA key")
        opaque = "JwtAccessTokenConverter" not in t
        fields = [f.group(0) for f in re.finditer(r"@Value\s*\(\s*\"[^\"]*\"\s*\)\s*(?:private|protected|public)?\s*[\w.<>]+\s+\w+\s*;", t)]
        encoder = any(re.search(r"\bPasswordEncoder\s+\w+\s*\(", read(x) or "") for x in srcs) and any(
            c["secret"] and ".encode(" in c["secret"] for c in clients)
        others = [x for x in srcs if x != p and re.search(r"WebSecurityConfigurerAdapter|SecurityFilterChain", read(x) or "")]
        if re.search(r"\.authenticationManager\s*\(", t):
            todo.append(f"{where}: endpoints.authenticationManager(...) only served the password grant, which Spring Authorization Server does not support")
        if re.search(r"JdbcTokenStore|RedisTokenStore|JdbcAuthorizationCodeServices|JdbcApprovalStore", t):
            todo.append(f"{where}: persistent token/approval store - use JdbcOAuth2AuthorizationService / JdbcOAuth2AuthorizationConsentService")
        if re.search(r"TokenEnhancer", t):
            todo.append(f"{where}: TokenEnhancer - move the extra claims into the generated legacyTokenCustomizer bean")
        client_src = [_sas_client(c, encoder, todo, where, opaque) for c in clients]
        write_file(p, _sas_class(pkg, cls, fields, client_src, key, encoder, default_chain=not others))
        changes.append(f"{where}: @EnableAuthorizationServer -> Spring Authorization Server ({len(clients)} client(s), "
                       f"{'HMAC key kept' if key['kind'] == 'hmac' else 'keystore key kept' if key['kind'] == 'keystore' else 'generated RSA key'}, "
                       f"legacy /oauth/* endpoint paths kept)")
        todo.append(f"{where}: /oauth/check_token now answers RFC 7662 introspection (active/scope/client_id), not the legacy check_token format")
        for bf in build_files(root):
            bt = read(bf) or ""
            if not any(LEGACY_OAUTH_DEP(g, a) for _, _, g, a in pom_dependency_blocks(bt)) and "spring-security-oauth2" not in bt:
                continue
            new, removed = build_remove_deps(bf, bt, LEGACY_OAUTH_DEP)
            # explicit version: the Boot 2.7 BOM does not manage it yet; fix_temporary_versions drops it after the 3.5 recipes
            new = build_add_dep(bf, new, "org.springframework.boot", "spring-boot-starter-oauth2-authorization-server", version=boot_version_for("3.5"))
            if write_if_changed(bf, bt, new):
                changes.append(f"{rel(bf, root)}: {', '.join(removed) or 'legacy oauth2'} -> spring-boot-starter-oauth2-authorization-server")
    return changes, todo


TEMP_VERSIONED = ("spring-boot-starter-oauth2-authorization-server", "spring-cloud-starter-gateway-server-webmvc")


def fix_temporary_versions(root: Path) -> list[str]:
    """Codemods pin a version on starters that the Boot 2.7 / Cloud 2021 BOMs do not know; once the BOMs manage them, drop it."""
    changes = []
    for bf in build_files(root):
        t = read(bf) or ""
        new = t
        for art in TEMP_VERSIONED:
            if bf.name == "pom.xml":
                new = re.sub(r"(<artifactId>" + art + r"</artifactId>\s*)<version>[^<]*</version>\s*", r"\1", new)
            else:
                new = re.sub(r"(['\"][\w.\-]+:" + art + r"):[\w.\-]+(['\"])", r"\1\2", new)
        if write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: dropped temporary versions now managed by the Boot/Cloud BOMs")
    return changes


# ---- OpenJPA API / annotations / properties in code / enhancer agent --------------------------------
OPENJPA_TYPE_MAP = {
    "OpenJPAEntityManager": "org.hibernate.Session", "OpenJPAEntityManagerSPI": "org.hibernate.Session",
    "OpenJPAEntityManagerFactory": "org.hibernate.SessionFactory", "OpenJPAEntityManagerFactorySPI": "org.hibernate.SessionFactory",
    "OpenJPAQuery": "org.hibernate.query.Query", "OpenJPAQuerySPI": "org.hibernate.query.Query",
}
# OpenJPA-only methods -> Hibernate Session equivalents (receiver is a variable of a converted type)
# getObjectId works on detached instances in OpenJPA; Session.getIdentifier does not, PersistenceUnitUtil does.
OPENJPA_METHOD_MAP = {"getObjectId": "getEntityManagerFactory().getPersistenceUnitUtil().getIdentifier", "evictAll": "clear"}
OPENJPA_BULK = {"persistAll": "persist", "removeAll": "remove", "detachAll": "detach", "refreshAll": "refresh", "evictAll": "evict"}


def _is_openjpa(text: str, simple: str) -> bool:
    return re.search(r"import\s+org\.apache\.openjpa\.[\w.]*\b" + simple + r"\s*;|import\s+org\.apache\.openjpa\.persistence(?:\.jdbc)?\.\*\s*;|@org\.apache\.openjpa\.[\w.]*" + simple + r"\b", text) is not None


def cm_openjpa_api(root: Path) -> tuple[list[str], list[str]]:
    changes, todo = [], []
    for p in java_sources(root):
        t = read(p) or ""
        if "org.apache.openjpa" not in t:
            continue
        new = t
        vars_: dict[str, str] = {}
        for oj, hib in OPENJPA_TYPE_MAP.items():
            for m in re.finditer(r"\b" + oj + r"\b(?:<[^>]*>)?\s+(\w+)\s*[=;,)]", new):
                vars_[m.group(1)] = hib
        # X v = OpenJPAPersistence.cast(expr)  ->  X v = expr.unwrap(X.class)
        def cast_sub(m):
            decl_type, var = m.group(1), m.group(2)
            k = m.end() - 1
            e = balanced(new_holder[0], k)
            arg = new_holder[0][k + 1:e].strip()
            hib = OPENJPA_TYPE_MAP.get(re.sub(r"<.*", "", decl_type), "org.hibernate.Session")
            simple = hib.rsplit(".", 1)[-1]
            return f"{simple} {var} = {arg}.unwrap({simple}.class)", e + 1
        new_holder = [new]
        out, pos = [], 0
        for m in re.finditer(r"\b(OpenJPA\w+(?:<[^>]*>)?)\s+(\w+)\s*=\s*OpenJPAPersistence\.cast\s*\(", new):
            rep, end = cast_sub(m)
            out.append(new[pos:m.start()] + rep)
            pos = end
        new = "".join(out) + new[pos:]
        # inline OpenJPAPersistence.cast(em).foo(...)
        def inline_sub(text):
            res, pos = [], 0
            for m in re.finditer(r"OpenJPAPersistence\.cast\s*\(", text):
                if m.start() < pos:
                    continue
                k = m.end() - 1
                e = balanced(text, k)
                arg = text[k + 1:e].strip()
                decl = re.search(r"\b(EntityManagerFactory|TypedQuery|Query|EntityManager)(?:<[^>]*>)?\s+" + re.escape(arg) + r"\b", text)
                kind = decl.group(1) if decl else "EntityManager"
                hib = {"EntityManagerFactory": "SessionFactory", "Query": "org.hibernate.query.Query", "TypedQuery": "org.hibernate.query.Query"}.get(kind, "Session")
                res.append(text[pos:m.start()] + f"{arg}.unwrap({hib}.class)")
                pos = e + 1
            return "".join(res) + text[pos:]
        new = inline_sub(new)
        for var in vars_:
            for oj, hib in OPENJPA_METHOD_MAP.items():
                new = re.sub(r"\b" + re.escape(var) + r"\." + oj + r"\(", f"{var}.{hib}(", new)
            for oj, hib in OPENJPA_BULK.items():
                def bulk(m, hib=hib, var=var):
                    k = m.end() - 1
                    e = balanced(new_b[0], k)
                    return None if e == -1 else (f"{new_b[0][k + 1:e].strip()}.forEach({var}::{hib})", e + 1)
                new_b = [new]
                res, pos = [], 0
                for m in re.finditer(r"\b" + re.escape(var) + r"\." + oj + r"\s*\((?=\s*\w)", new):
                    r = bulk(m)
                    if r and m.start() >= pos:
                        res.append(new[pos:m.start()] + r[0]); pos = r[1]
                new = "".join(res) + new[pos:]
        for oj, hib in sorted(OPENJPA_TYPE_MAP.items(), key=lambda kv: -len(kv[0])):
            simple = hib.rsplit(".", 1)[-1]
            if re.search(r"\b" + oj + r"\b", re.sub(r"^\s*import.*$", "", new, flags=re.M)):
                q = simple if hib != "org.hibernate.query.Query" or not re.search(r"import\s+(?:javax|jakarta)\.persistence\.Query\s*;", new) else hib
                new = re.sub(r"\b" + oj + r"\b(?!\s*;)", q, re.sub(r"^\s*import\s+org\.apache\.openjpa\.persistence\." + oj + r"\s*;\s*\n", "", new, flags=re.M))
                if q == simple:
                    new = add_import(new, hib)
        for simple in ("Session", "SessionFactory"):
            if re.search(r"\b" + simple + r"\.class\b", new):
                new = add_import(new, "org.hibernate." + simple)
        if "org.hibernate.query.Query.class" not in new and re.search(r"(?<![\w.])Query\.class", new) and "import org.hibernate.query.Query;" not in new \
                and not re.search(r"import\s+(?:javax|jakarta)\.persistence\.Query\s*;", new):
            new = add_import(new, "org.hibernate.query.Query")
        new = remove_import(new, r"org\.apache\.openjpa\.persistence\.OpenJPAPersistence")
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: OpenJPA EntityManager/Query/Factory API -> Hibernate Session/Query/SessionFactory (unwrap)")
        rest = re.findall(r"\b(OpenJPA\w+|FetchPlan|JDBCFetchPlan|FetchConfiguration)\b", re.sub(r"^\s*import.*$", "", new, flags=re.M))
        if rest:
            todo.append(f"{rel(p, root)}: OpenJPA API left without a mechanical equivalent: {', '.join(sorted(set(rest)))} "
                        f"(FetchPlan -> EntityGraph hint `jakarta.persistence.fetchgraph`)")
    return changes, todo


def field_after(text: str, pos: int):
    """The field or getter declared after pos: (type, name, start, end)."""
    rest = text[pos:]
    j = 0
    while True:
        m = re.match(r"\s*@[\w.]+", rest[j:])
        if not m:
            break
        j += m.end()
        k = j
        while k < len(rest) and rest[k] in " \t":
            k += 1
        if k < len(rest) and rest[k] == "(":
            e = balanced(rest, k)
            j = e + 1
    m = re.match(r"\s*((?:(?:private|protected|public|static|final|transient|volatile)\s+)*)([\w.]+(?:\s*<[^;=(){}]*>)?(?:\[\])*)\s+(\w+)\s*(=|;|\()", rest[j:])
    if not m:
        return None
    return m.group(2).strip(), m.group(3), pos + j + m.start(1), pos + j + m.end()


def _field_block(text: str, s: int):
    """(block_start, field) for the annotation at s: block_start is the first line of the annotation block above the field."""
    a = text.rfind("\n", 0, s) + 1
    while a > 1:
        ps = text.rfind("\n", 0, a - 1) + 1
        prev = text[ps:a - 1].strip()
        if prev.startswith("@") and not prev.endswith((";", "{", "}")):
            a = ps
        else:
            break
    return a, field_after(text, a)


def _cut(blk: str, s: int, e: int) -> str:
    ls = blk.rfind("\n", 0, s) + 1
    le = blk.find("\n", e)
    if blk[ls:s].strip() == "" and (le == -1 or blk[e:le].strip() == ""):
        return blk[:ls] + (blk[le + 1:] if le != -1 else "")
    return blk[:s] + blk[e:].lstrip(" ")


def _add_ann_attr(text: str, s: int, e: int, args: str | None, attr: str) -> str:
    """Add attr (e.g. 'updatable = false') to the annotation at text[s:e]."""
    name = re.match(r"@[\w.]+", text[s:e]).group(0)
    if args is None or not args.strip():
        return text[:s] + f"{name}({attr})" + text[e:]
    a = args.strip()
    if "=" not in a.split(",")[0]:
        a = "value = " + a
    return text[:s] + f"{name}({a}, {attr})" + text[e:]


def _converter_class(name: str, ftype: str, ext: str, fac: str | None, owner: str) -> str:
    to_db = f"value.{ext}()" if "." not in ext else f"{ext}(value)"
    if fac:
        to_attr = f"{fac}(dbData)" if "." in fac else f"{owner}.{fac}(dbData)"
    else:
        to_attr = f"new {ftype}(dbData)"
    return (f"\n    /** Added by spring-migrate: replaces OpenJPA @Externalizer(\"{ext}\")" + (f"/@Factory(\"{fac}\")" if fac else "") + ". */\n"
            f"    @Converter\n    public static class {name} implements AttributeConverter<{ftype}, String> {{\n"
            f"        @Override\n        public String convertToDatabaseColumn({ftype} value) {{\n"
            f"            return value == null ? null : String.valueOf({to_db});\n        }}\n\n"
            f"        @Override\n        public {ftype} convertToEntityAttribute(String dbData) {{\n"
            f"            if (dbData == null) {{\n                return null;\n            }}\n"
            f"            try {{\n                return {to_attr};\n            }} catch (Exception e) {{\n"
            f"                throw new IllegalArgumentException(\"Cannot convert '\" + dbData + \"' to {ftype}\", e);\n            }}\n        }}\n    }}\n")


def cm_openjpa_annotations(root: Path) -> tuple[list[str], list[str]]:
    changes, todo = [], []
    for p in java_sources(root, main_only=True):
        t = read(p) or ""
        if "org.apache.openjpa" not in t:
            continue
        new, pkg, what = t, persistence_pkg(t), []
        owner = (re.search(r"\bclass\s+(\w+)", t) or [None, p.stem])[1]

        pending_imports: list[str] = []

        def imp(*names):  # deferred: adding an import mid-edit would shift the offsets being worked on
            pending_imports.extend(n for n in names if n not in pending_imports)
        # @DataCache
        if _is_openjpa(new, "DataCache"):
            for s, e, args in sorted(annotation_spans(new, "DataCache"), reverse=True):
                off = re.search(r"enabled\s*=\s*false", args or "")
                new = new[:s] + ("@Cacheable(false)" if off else "@Cacheable") + new[e:]
                imp("Cacheable")
                what.append("@DataCache -> @Cacheable")
                if args and re.search(r"timeout|name", args):
                    todo.append(f"{rel(p, root)}: @DataCache({args}) - configure expiry/region in your Hibernate L2 cache provider")
        # @FetchGroup(s) -> @NamedEntityGraph(s)
        if _is_openjpa(new, "FetchGroup") or _is_openjpa(new, "FetchGroups"):
            for s, e, args in sorted(annotation_spans(new, "FetchGroup"), reverse=True):
                gname = ann_attr(args, "name") or '""'
                attrs = re.findall(r"@(?:[\w.]+\.)?FetchAttribute\s*\(\s*(?:name\s*=\s*)?(\"[^\"]+\")", args or "")
                nodes = ", ".join(f"@NamedAttributeNode({a})" for a in attrs)
                new = new[:s] + f"@NamedEntityGraph(name = {gname}, attributeNodes = {{{nodes}}})" + new[e:]
            new = re.sub(r"@(?:org\.apache\.openjpa\.persistence\.)?FetchGroups\b", "@NamedEntityGraphs", new)
            imp("NamedEntityGraph", "NamedAttributeNode")
            if "@NamedEntityGraphs" in new:
                imp("NamedEntityGraphs")
            what.append("@FetchGroup -> @NamedEntityGraph")
        # field-level annotations: one at a time, re-scanning after each edit
        order = ["Externalizer", "Factory", "Persistent", "PersistentCollection", "PersistentMap", "ReadOnly", "Index",
                 "ForeignKey", "Dependent", "ElementDependent", "KeyDependent"]
        indexes, converters = [], []
        for n in [x for x in order if _is_openjpa(t, x)]:
            guard = 0
            while guard < 500:
                guard += 1
                spans = list(annotation_spans(new, n))
                if not spans:
                    break
                s0, e0, args = spans[-1]
                a, fld = _field_block(new, s0)
                if not fld:
                    new = new[:s0] + new[e0:]  # not on a field: drop it, reported below
                    todo.append(f"{rel(p, root)}: OpenJPA @{n} not on a field was removed - review")
                    continue
                ftype, fname, fs, _ = fld
                blk, s1, e1 = new[a:fs], s0 - a, e0 - a
                if n == "Persistent":
                    keep = [x for x in split_top_level(args or "") if re.match(r"(fetch|optional)\s*=", x)]
                    if [x for x in split_top_level(args or "") if x and x not in keep]:
                        todo.append(f"{rel(p, root)}: @Persistent({args}) on {fname} - map cascade/mappedBy/embedded by hand")
                    blk = blk[:s1] + ("@Basic(" + ", ".join(keep) + ")" if keep else "@Basic") + blk[e1:]
                    imp("Basic")
                elif n in ("PersistentCollection", "PersistentMap"):
                    keep = [x for x in split_top_level(args or "") if re.match(r"fetch\s*=", x)]
                    blk = blk[:s1] + ("@ElementCollection(" + ", ".join(keep) + ")" if keep else "@ElementCollection") + blk[e1:]
                    imp("ElementCollection")
                elif n == "ReadOnly":
                    if args and "RESTRICT" in args:
                        todo.append(f"{rel(p, root)}: @ReadOnly(RESTRICT) on {fname} threw on update; Hibernate silently skips the column")
                    blk = _cut(blk, s1, e1)
                    col = next(annotation_spans(blk, "Column"), None)
                    if col and "updatable" not in (col[2] or ""):
                        blk = _add_ann_attr(blk, col[0], col[1], col[2], "updatable = false")
                    elif not col:
                        ind = re.match(r"[ \t]*", new[a:]).group(0)
                        blk = blk + f"@Column(updatable = false)\n{ind}" if blk.endswith(ind) else blk + "@Column(updatable = false) "
                    imp("Column")
                elif n == "Index":
                    col = next(annotation_spans(blk, "Column"), None)
                    cname = unquote_java(ann_attr(col[2], "name")) if col else None
                    iname, uniq = ann_attr(args, "name"), ann_attr(args, "unique")
                    indexes.append("@Index(" + (f"name = {iname}, " if iname else "") + f'columnList = "{cname or fname}"' + (f", unique = {uniq}" if uniq else "") + ")")
                    blk = _cut(blk, s1, e1)
                elif n == "ForeignKey":
                    blk = _cut(blk, s1, e1)
                    todo.append(f"{rel(p, root)}: OpenJPA @ForeignKey on {fname} removed (Hibernate creates FKs by default; name it with @JoinColumn(foreignKey=...))")
                elif n in ("Dependent", "ElementDependent", "KeyDependent"):
                    blk = _cut(blk, s1, e1)
                    ra = next((x for k in ("OneToMany", "OneToOne") for x in annotation_spans(blk, k)), None)
                    if ra and "orphanRemoval" not in (ra[2] or ""):
                        blk = _add_ann_attr(blk, ra[0], ra[1], ra[2], "orphanRemoval = true")
                    elif not ra:
                        todo.append(f"{rel(p, root)}: @{n} on {fname} - add cascade = CascadeType.REMOVE by hand")
                elif n == "Externalizer":
                    ext = unquote_java(ann_attr(args, "value")) or "toString"
                    fac = None
                    fa = next(annotation_spans(blk, "Factory"), None) if _is_openjpa(t, "Factory") else None
                    if fa:
                        fac = unquote_java(ann_attr(fa[2], "value"))
                    cname = fname[0].upper() + fname[1:] + "Converter"
                    converters.append(_converter_class(cname, ftype, ext, fac, owner))
                    blk = blk[:s1] + f"@Convert(converter = {owner}.{cname}.class)" + blk[e1:]
                    if fa:
                        fa = next(annotation_spans(blk, "Factory"))
                        blk = _cut(blk, fa[0], fa[1])
                    imp("Convert", "Converter", "AttributeConverter")
                elif n == "Factory":
                    todo.append(f"{rel(p, root)}: @Factory without @Externalizer on {fname} - write an AttributeConverter")
                    blk = _cut(blk, s1, e1)
                new = new[:a] + blk + new[fs:]
                what.append(f"@{n}")
        if indexes:
            tab = next(annotation_spans(new, "Table"), None)
            idx = ", ".join(indexes)
            if tab and "indexes" not in (tab[2] or ""):
                new = _add_ann_attr(new, tab[0], tab[1], tab[2], f"indexes = {{{idx}}}")
            elif not tab:
                cm = re.search(r"^([ \t]*)(?:public\s+|abstract\s+|final\s+)*class\s+" + owner + r"\b", new, re.M)
                new = new[:cm.start()] + f"{cm.group(1)}@Table(indexes = {{{idx}}})\n" + new[cm.start():]
            imp("Table", "Index")
        if converters:
            last = new.rstrip().rfind("}")
            new = new[:last].rstrip("\n") + "\n" + "".join(converters) + new[last:]
        new = remove_import(new, r"org\.apache\.openjpa\.persistence\.(?:jdbc\.)?(?:" + "|".join(
            ["DataCache", "FetchGroups?", "FetchAttribute", "Externalizer", "Factory", "Persistent", "PersistentCollection",
             "PersistentMap", "ReadOnly", "UpdateAction", "Index", "ForeignKey", "Dependent", "ElementDependent", "KeyDependent"]) + ")")
        for n in pending_imports:
            new = add_import(new, f"{pkg}.{n}")
        new = drop_unused_imports(new, [i for i in java_imports(new) if i.startswith("org.apache.openjpa.")])
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: OpenJPA annotations -> JPA/Hibernate ({', '.join(sorted(set(what)))})")
    return changes, todo


def cm_openjpa_code_props(root: Path) -> tuple[list[str], list[str]]:
    changes, todo = [], []
    for p in java_sources(root):
        t = read(p) or ""
        if '"openjpa.' not in t:
            continue
        lines, out, n = t.splitlines(keepends=True), [], 0
        for line in lines:
            m = re.search(r"\"(openjpa\.[\w.]+)\"\s*,\s*(\"(?:[^\"\\]|\\.)*\")?", line)
            if m and line.rstrip().endswith(";"):
                keep, note = map_openjpa_property(m.group(1), unquote_java(m.group(2)) or "")
                if keep:
                    line = line.replace(f'"{m.group(1)}"', f'"{keep[0][0]}"', 1)
                    if keep[0][1] != (unquote_java(m.group(2)) or "") and m.group(2):
                        line = line.replace(m.group(2), json.dumps(keep[0][1]), 1)
                else:
                    ind = re.match(r"[ \t]*", line).group(0)
                    line = f"{ind}// TODO(spring-migrate): OpenJPA property dropped ({note}): {line.strip()}\n"
                    todo.append(f"{rel(p, root)}: {m.group(1)} set in code: {note}")
                n += 1
            out.append(line)
        new = "".join(out)
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: {n} openjpa.* propert(ies) set in code translated/commented")
    for f in config_files(root):
        c = ConfigFile(f)
        hit = False
        for e in c.keys_matching(r"(?:spring\.jpa\.properties\.)?openjpa\."):
            key = e["key"].replace("spring.jpa.properties.", "")
            keep, note = map_openjpa_property(key, e["value"])
            doc = c.remove(e["key"]); hit = True
            for k, v in keep:
                c.put("spring.jpa.properties." + k, v, doc=doc or 0)
            if note:
                todo.append(f"{rel(f, root)}: {e['key']}={e['value']}: {note}")
        if hit and c.save():
            changes.append(f"{rel(f, root)}: openjpa.* properties translated")
    return changes, todo


def cm_openjpa_agent(root: Path) -> tuple[list[str], list[str]]:
    changes = []
    agent = r"-javaagent:[^\s\"',]*openjpa[^\s\"',]*"
    for p in walk(root):
        if not (p.name.startswith("Dockerfile") or p.suffix in (".sh", ".cmd", ".bat", ".service", ".env", ".conf", ".yml", ".yaml")
                or p.name in ("Jenkinsfile", "pom.xml", "build.gradle", "build.gradle.kts", "Procfile", "jvm.config")):
            continue
        t = read(p)
        if not t or "openjpa" not in t.lower():
            continue
        new = re.sub(r"\"" + agent + r"\"\s*,\s*", "", t)            # exec-form JSON array element
        new = re.sub(r"[ \t]*" + agent, "", new)
        new = re.sub(r"(?im)^[ \t]*(?:COPY|ADD)\s+\S*openjpa[-\w.]*\.jar\s+\S+[ \t]*\r?\n", "", new)
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: removed the OpenJPA enhancer javaagent")
    return changes, []


def openjpa_pooled_lo(root: Path) -> list[str]:
    """OpenJPA's table sequence stores the next free value; Hibernate's pooled-lo optimizer reads it the same way."""
    changes = []
    prop = '<property name="hibernate.id.optimizer.pooled.preferred" value="pooled-lo"/>'
    for f in walk(root):
        if f.name == "persistence.xml":
            t = read(f) or ""
            if "pooled.preferred" in t or "<properties>" not in t:
                continue
            new = re.sub(r"([ \t]*)<properties>[ \t]*\n", lambda m: f"{m.group(0)}{m.group(1)}  {prop}\n", t, count=1)
            if write_if_changed(f, t, new):
                changes.append(f"{rel(f, root)}: hibernate.id.optimizer.pooled.preferred=pooled-lo (matches OpenJPA sequence semantics)")
    for f in config_files(root, include_test=False):
        c = ConfigFile(f)
        if c.get("spring.jpa.properties.hibernate.id.optimizer.pooled.preferred") is None and any("data-jpa" in (read(b) or "") for b in build_files(root)):
            c.put("spring.jpa.properties.hibernate.id.optimizer.pooled.preferred", "pooled-lo")
            if c.save():
                changes.append(f"{rel(f, root)}: hibernate.id.optimizer.pooled.preferred=pooled-lo")
        break
    return changes

# --------------------------------------------------------------------------- #
# Post-fixers that run after the OpenRewrite recipes of a stage
# --------------------------------------------------------------------------- #
def fix_quoted_properties(root: Path, base_rev: str | None) -> list[str]:
    """OpenRewrite writes some new .properties values as "..." (e.g. springdoc.group-configs). In .properties the quotes
    become part of the value. Unquote values on lines this stage added."""
    changes = []
    for f in config_files(root):
        if f.suffix != ".properties":
            continue
        t = read(f) or ""
        before = ""
        if base_rev:
            r = subprocess.run(["git", "show", f"{base_rev}:{rel(f, root)}"], cwd=root, capture_output=True)
            before = decode_text(r.stdout)[0] if r.returncode == 0 else ""
        old_lines = set(before.splitlines())
        out, n = [], 0
        for line in t.splitlines():
            m = re.match(r'^(\s*[^#!\s][^=:]*?\s*[=:]\s*)"(.*)"\s*$', line)
            if m and line not in old_lines and (base_rev or re.match(r"\s*(springdoc|logging\.pattern)\.", line)):
                line = m.group(1) + m.group(2)
                n += 1
            out.append(line)
        if n:
            write_file(f, "\n".join(out) + ("\n" if t.endswith("\n") else ""))
            changes.append(f"{rel(f, root)}: unquoted {n} value(s) that OpenRewrite wrote with literal quotes")
    return changes


def fix_springdoc_leftovers(root: Path) -> list[str]:
    changes = []
    srcs = list(java_sources(root))
    has_openapi_bean = any(re.search(r"\bOpenAPI\s+\w+\s*\(", read(p) or "") for p in srcs)
    for p in srcs:
        t = read(p) or ""
        new = re.sub(r"\"/v2/api-docs(?:/\*\*)?\"", '"/v3/api-docs/**"', t)
        if not has_openapi_bean and "io.swagger.v3.oas.models.info.Info" in new and "@Configuration" in new:
            m = re.search(r"(?:private|protected|public)?\s*(?:static\s+)?Info\s+(\w+)\s*\(\s*\)\s*\{", new)
            if m:
                last = new.rstrip().rfind("}")
                bean = (f"\n    /** Added by spring-migrate: Springfox's Docket.apiInfo(...) became springdoc's OpenAPI bean. */\n"
                        f"    @Bean\n    public OpenAPI openApi() {{\n        return new OpenAPI().info({m.group(1)}());\n    }}\n")
                new = new[:last].rstrip("\n") + "\n" + bean + new[last:]
                new = add_import(new, "io.swagger.v3.oas.models.OpenAPI")
                new = add_import(new, "org.springframework.context.annotation.Bean")
                has_openapi_bean = True
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: springdoc follow-ups (OpenAPI info bean / /v3/api-docs paths)")
    return changes


def fix_httpclient5_checked(root: Path) -> list[str]:
    """HttpClient 5's EntityUtils.toString throws the checked ParseException (unchecked in 4.x). Route calls through a
    helper that rethrows it as IOException, which callers of the 4.x API already handle."""
    changes = []
    for p in java_sources(root):
        t = read(p) or ""
        if "org.apache.hc.core5.http.io.entity.EntityUtils" not in t or "EntityUtils.toString(" not in t or "ParseException" in t:
            continue
        arities = set()
        def sub(m):
            k = m.end() - 1
            e = balanced(t, k)
            args = split_top_level(t[k + 1:e])
            arities.add("1" if len(args) == 1 else ("charset" if re.search(r"Charset|StandardCharsets", args[1]) else "string"))
            return "entityToString("
        new = re.sub(r"(?<![\w.])EntityUtils\.toString\s*\(", sub, t)
        helpers = []
        for a in sorted(arities):
            params, call = {"1": ("org.apache.hc.core5.http.HttpEntity entity", "entity"),
                            "charset": ("org.apache.hc.core5.http.HttpEntity entity, java.nio.charset.Charset charset", "entity, charset"),
                            "string": ("org.apache.hc.core5.http.HttpEntity entity, String charset", "entity, charset")}[a]
            helpers.append(f"\n    /** Added by spring-migrate: HttpClient 5 made ParseException checked; HttpClient 4 callers expect IOException. */\n"
                           f"    private static String entityToString({params}) throws java.io.IOException {{\n"
                           f"        try {{\n            return EntityUtils.toString({call});\n"
                           f"        }} catch (org.apache.hc.core5.http.ParseException e) {{\n            throw new java.io.IOException(e);\n        }}\n    }}\n")
        last = new.rstrip().rfind("}")
        new = new[:last].rstrip("\n") + "\n" + "".join(helpers) + new[last:]
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: HttpClient 5 checked ParseException handled (EntityUtils.toString helper)")
    return changes


def fix_boot4_aspectj(root: Path, stage_rev: str | None) -> list[str]:
    """Boot 4 replaced spring-boot-starter-aop with spring-boot-starter-aspectj; the OpenRewrite 4.0 recipe removes the old
    starter without adding the new one, which silently disables @Aspect / Resilience4j / @Retryable annotations."""
    changes = []
    aop_rx = r"@Aspect\b|io\.github\.resilience4j\.\w+\.annotation|@EnableAspectJAutoProxy|@Retryable"
    for bf in build_files(root):
        t = read(bf) or ""
        mod = bf.parent
        # per module: in a multi-module build only the modules whose own code needs AOP get the starter
        needs_aop = "<packaging>pom</packaging>" not in t and any(
            re.search(aop_rx, read(p) or "") for p in java_sources(mod, main_only=True) if module_of(p, root) == mod)
        had = "spring-boot-starter-aop" in t
        if stage_rev and not had:
            r = subprocess.run(["git", "show", f"{stage_rev}:{rel(bf, root)}"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
            had = r.returncode == 0 and "spring-boot-starter-aop" in r.stdout
        if not (had or (needs_aop and "spring-boot-starter" in t)):
            continue
        new = re.sub(r"spring-boot-starter-aop\b", "spring-boot-starter-aspectj", t)
        new = build_add_dep(bf, new, "org.springframework.boot", "spring-boot-starter-aspectj")
        if write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: spring-boot-starter-aop -> spring-boot-starter-aspectj (Boot 4; keeps @Aspect/Resilience4j annotations working)")
    return changes


# ---- springdoc: keep code and dependency on the same line (1.x Boot 2, 2.x Boot 3, 3.x Boot 4) -------------------
SPRINGDOC_V2_CLASSES = {  # springdoc 1.x -> 2.x
    "org.springdoc.core.GroupedOpenApi": "org.springdoc.core.models.GroupedOpenApi",
    "org.springdoc.core.SpringDocUtils": "org.springdoc.core.utils.SpringDocUtils",
    "org.springdoc.core.Constants": "org.springdoc.core.utils.Constants",
    "org.springdoc.core.SpringDocConfigProperties": "org.springdoc.core.properties.SpringDocConfigProperties",
    "org.springdoc.core.SwaggerUiConfigProperties": "org.springdoc.core.properties.SwaggerUiConfigProperties",
    "org.springdoc.core.SwaggerUiConfigParameters": "org.springdoc.core.properties.SwaggerUiConfigParameters",
    "org.springdoc.core.SwaggerUiOAuthProperties": "org.springdoc.core.properties.SwaggerUiOAuthProperties",
    "org.springdoc.core.GenericResponseService": "org.springdoc.core.service.GenericResponseService",
    "org.springdoc.core.OperationService": "org.springdoc.core.service.OperationService",
    "org.springdoc.core.AbstractRequestService": "org.springdoc.core.service.AbstractRequestService",
    "org.springdoc.core.customizers.OpenApiCustomiser": "org.springdoc.core.customizers.OpenApiCustomizer",
}
SPRINGDOC_V2_NAMES = {"OpenApiCustomiser": "OpenApiCustomizer", "addOpenApiCustomiser": "addOpenApiCustomizer"}
SPRINGDOC_V2_ARTIFACTS = {  # springdoc 1.x artifact -> 2.x artifact (None: merged into springdoc-openapi-starter-common)
    "springdoc-openapi-ui": "springdoc-openapi-starter-webmvc-ui", "springdoc-openapi-webmvc-core": "springdoc-openapi-starter-webmvc-api",
    "springdoc-openapi-webflux-ui": "springdoc-openapi-starter-webflux-ui", "springdoc-openapi-webflux-core": "springdoc-openapi-starter-webflux-api",
    "springdoc-openapi-common": "springdoc-openapi-starter-common", "springdoc-openapi-security": None, "springdoc-openapi-data-rest": None,
    "springdoc-openapi-kotlin": None, "springdoc-openapi-hateoas": None,
}
SPRINGDOC_LINE = {"2": ("1", "springdoc-openapi-common", "1.8", "1.8.0"), "3": ("2", "springdoc-openapi-starter-common", "2.8", "2.8.17"),
                  "4": ("3", "springdoc-openapi-starter-common", "3.0", "3.0.3")}


def cm_springdoc_inline_versions(root: Path) -> tuple[list[str], list[str]]:
    """Before the Boot 3.5 recipes: springdoc 1.x artifacts whose version comes from a property get the value inline.
    OpenRewrite's springdoc upgrade bumps such a property to 2.x but leaves the 1.x artifact name, which has no 2.x
    release. fix_springdoc renames and upgrades the artifact afterwards and puts the property back."""
    changes = []
    poms = [p for p in build_files(root) if p.name == "pom.xml"]
    props = {}
    for p in poms:
        props.update(_pom_props(read(p) or ""))
    for p in poms:
        t = read(p) or ""
        def inline(m):
            blk = m.group(0)
            if _first_tag(blk, "groupId") != "org.springdoc" or _first_tag(blk, "artifactId") not in SPRINGDOC_V2_ARTIFACTS:
                return blk
            v = _first_tag(blk, "version")
            pm = re.fullmatch(r"\$\{([\w.\-]+)\}", v or "")
            if not pm or not props.get(pm.group(1)):
                return blk
            return blk.replace(f"<version>{v}</version>", f"<version>{props[pm.group(1)]}</version><!--spring-migrate:{pm.group(1)}-->")
        new = re.sub(r"<dependency>(?:(?!<dependency>).)*?</dependency>", inline, t, flags=re.S)
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: springdoc 1.x version inlined for the upgrade (property restored afterwards)")
    return changes, []


def fix_springdoc(root: Path, boot_line: str) -> list[str]:
    """OpenRewrite moves springdoc code to 2.x packages already in the Boot 2.7 stage, and only upgrades springdoc
    dependencies that carry their own version - not one managed in a root <dependencyManagement> (often via a property)
    or by a company parent/BOM. Then the code and the springdoc jar do not match ("package org.springdoc.core.models
    does not exist"). Align both to the springdoc line of this stage's Boot version."""
    changes = []
    poms = [p for p in build_files(root) if p.name == "pom.xml"]
    texts = {p: read(p) or "" for p in poms}
    if not any("<groupId>org.springdoc</groupId>" in t for t in texts.values()):
        return []
    major, ref_artifact, line, fallback = SPRINGDOC_LINE[boot_line.split(".")[0]]
    want = latest_patch("org.springdoc", ref_artifact, line, fallback)
    v2 = major != "1"
    props_to_set: dict[str, str] = {}
    unversioned: set[str] = set()
    managed_here: set[str] = set()
    for p, t in texts.items():
        new = t
        if v2:
            def rename(m):
                blk = m.group(0)
                a = _first_tag(blk, "artifactId")
                if _first_tag(blk, "groupId") != "org.springdoc" or a not in SPRINGDOC_V2_ARTIFACTS:
                    return blk
                to = SPRINGDOC_V2_ARTIFACTS[a]
                if to is None:
                    return ""      # merged into springdoc 2's common module
                return blk.replace(f"<artifactId>{a}</artifactId>", f"<artifactId>{to}</artifactId>")
            new = re.sub(r"[ \t]*<dependency>(?:(?!<dependency>).)*?</dependency>[ \t]*\r?\n?", rename, new, flags=re.S)
        dm_spans = [m.span() for m in re.finditer(r"<dependencyManagement>.*?</dependencyManagement>", new, re.S)]
        for m in re.finditer(r"<dependency>((?:(?!<dependency>).)*?)</dependency>", new, re.S):
            blk = m.group(1)
            if _first_tag(blk, "groupId") != "org.springdoc":
                continue
            a, ver = _first_tag(blk, "artifactId"), _first_tag(blk, "version")
            if any(x <= m.start() < y for x, y in dm_spans):
                managed_here.add(a)
            if not ver:
                unversioned.add(a)
            elif ver.startswith("${"):
                props_to_set[ver[2:-1]] = want
        def setver(m):
            blk = m.group(0)
            if _first_tag(blk, "groupId") != "org.springdoc":
                return blk
            ver = _first_tag(blk, "version")
            if ver and not ver.startswith("${") and ver.split(".")[0] != major:
                return blk.replace(f"<version>{ver}</version>", f"<version>{want}</version>")
            return blk
        new = re.sub(r"<dependency>(?:(?!<dependency>).)*?</dependency>", setver, new, flags=re.S)
        texts[p] = new
    for prop, v in props_to_set.items():
        for p, t in texts.items():
            m = re.search(r"<" + re.escape(prop) + r">([^<]*)</" + re.escape(prop) + r">", t)
            if m and m.group(1).split(".")[0] != major:
                texts[p] = t.replace(m.group(0), f"<{prop}>{v}</{prop}>")
    # versions that come from outside the build (company parent / BOM): pin them in the top POM
    outside = sorted(unversioned - managed_here)
    top = root / "pom.xml" if (root / "pom.xml").exists() else (poms[0] if poms else None)
    if outside and top:
        t = texts[top]
        for a in outside:
            if a in (SPRINGDOC_V2_ARTIFACTS if v2 else {}) and SPRINGDOC_V2_ARTIFACTS.get(a) is None:
                continue
            dep = (f"      <dependency>\n        <groupId>org.springdoc</groupId>\n        <artifactId>{a}</artifactId>\n"
                   f"        <version>{want}</version>\n      </dependency>\n")
            mm = re.search(r"<dependencyManagement>\s*<dependencies>[ \t]*\n?", t)
            if mm:
                t = t[:mm.end()] + ("" if t[mm.end() - 1] == "\n" else "\n") + dep + t[mm.end():]
            else:
                block = f"  <dependencyManagement>\n    <dependencies>\n{dep}    </dependencies>\n  </dependencyManagement>\n"
                anchor = _top_level(t, "dependencies") or _top_level(t, "build")
                pos = anchor.start() if anchor else t.rindex("</project>")
                t = t[:pos] + block + t[pos:]
        texts[top] = t
    # put back the version properties that cm_springdoc_inline_versions inlined
    for p, t in list(texts.items()):
        for m in list(re.finditer(r"<version>([^<]*)</version><!--spring-migrate:([\w.\-]+)-->", t)):
            prop = m.group(2)
            t = t.replace(m.group(0), f"<version>${{{prop}}}</version>")
            for q in texts:
                qt = t if q == p else texts[q]
                pm = re.search(r"<" + re.escape(prop) + r">([^<]*)</" + re.escape(prop) + r">", qt)
                if pm:
                    nv = want if v2 or pm.group(1).split(".")[0] != major else pm.group(1)
                    qt = qt.replace(pm.group(0), f"<{prop}>{nv}</{prop}>")
                    if q == p:
                        t = qt
                    else:
                        texts[q] = qt
        texts[p] = t
    for p, t in texts.items():
        if write_if_changed(p, read(p) or "", t):
            changes.append(f"{rel(p, root)}: springdoc -> {want} (Boot {boot_line} line; artifacts/managed versions aligned)")
    # code: the class names of the same springdoc line
    for p in java_sources(root):
        t = read(p) or ""
        if "org.springdoc" not in t:
            continue
        new = t
        pairs = list(SPRINGDOC_V2_CLASSES.items()) if v2 else [(b, a) for a, b in SPRINGDOC_V2_CLASSES.items()]
        for a, b in sorted(pairs, key=lambda x: -len(x[0])):
            new = re.sub(re.escape(a) + r"\b", b, new)
        names = SPRINGDOC_V2_NAMES.items() if v2 else [(b, a) for a, b in SPRINGDOC_V2_NAMES.items()]
        for a, b in names:
            new = re.sub(r"\b" + a + r"\b", b, new)
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: springdoc classes -> springdoc {major}.x names")
    return changes


# ---- javax-era JAXB / JAX-WS tooling -> Jakarta successors (Boot 3+) -------------------------------------------
JAXB_ARTIFACTS = {  # (old group, old artifact) -> (new group, new artifact, version line, fallback)
    ("org.jvnet.jaxb2_commons", "jaxb2-basics-runtime"): ("org.jvnet.jaxb", "jaxb-plugins-runtime", "4.0", "4.0.16"),
    ("org.jvnet.jaxb2_commons", "jaxb2-basics"): ("org.jvnet.jaxb", "jaxb-plugins", "4.0", "4.0.16"),
    ("org.jvnet.jaxb2_commons", "jaxb2-value-constructor"): ("org.jvnet.jaxb", "jaxb-plugins", "4.0", "4.0.16"),
    ("org.jvnet.jaxb2_commons", "jaxb2-basics-tools"): ("org.jvnet.jaxb", "jaxb-plugins-tools", "4.0", "4.0.16"),
    ("org.jvnet.jaxb2.maven2", "maven-jaxb2-plugin"): ("org.jvnet.jaxb", "jaxb-maven-plugin", "4.0", "4.0.16"),
    ("org.jvnet.jaxb2.maven2", "maven-jaxb22-plugin"): ("org.jvnet.jaxb", "jaxb-maven-plugin", "4.0", "4.0.16"),
    ("org.jvnet.jaxb2.maven2", "maven-jaxb23-plugin"): ("org.jvnet.jaxb", "jaxb-maven-plugin", "4.0", "4.0.16"),
    ("org.codehaus.mojo", "jaxws-maven-plugin"): ("com.sun.xml.ws", "jaxws-maven-plugin", "4.0", "4.0.5"),
    ("com.sun.xml.ws", "jaxws-maven-plugin"): ("com.sun.xml.ws", "jaxws-maven-plugin", "4.0", "4.0.5"),
}
# jaxb2-basics 1.x/2.x "...2" interfaces became the plain names in org.jvnet.jaxb 4.x
JAXB_RUNTIME_RENAMES = {n + "2": n for n in ("ToString", "Equals", "HashCode", "CopyTo", "MergeFrom", "ToStringStrategy",
                                              "EqualsStrategy", "HashCodeStrategy", "CopyStrategy", "MergeStrategy")}


def fix_jaxb_tooling(root: Path) -> tuple[list[str], list[str]]:
    """jaxb2-basics, maven-jaxb2-plugin and jaxws-maven-plugin < 4 generate/expect javax.xml.bind - Boot 3 is Jakarta."""
    changes, todo = [], []
    for bf in build_files(root):
        if bf.name != "pom.xml":
            continue
        t = read(bf) or ""
        if not re.search(r"jaxb2|jaxws-maven-plugin", t):
            continue
        new = t
        seen_in_block: set = set()
        def swap(m):
            blk = m.group(0)
            body = re.sub(r"<(dependencies|executions|configuration|exclusions)>.*?</\1>", "", blk, flags=re.S)
            g, a, v = _first_tag(body, "groupId"), _first_tag(body, "artifactId"), _first_tag(body, "version")
            target = JAXB_ARTIFACTS.get((g, a))
            if not target:
                return blk
            ng, na, line, fb = target
            if (ng, na) == (g, a) and v and not v.startswith("${") and vtuple(v)[0] >= 4:
                return blk
            want = latest_patch(ng, na, line, fb)
            out = blk.replace(f"<groupId>{g}</groupId>", f"<groupId>{ng}</groupId>", 1).replace(f"<artifactId>{a}</artifactId>", f"<artifactId>{na}</artifactId>", 1)
            if v:
                out = out.replace(f"<version>{v}</version>", f"<version>{want}</version>", 1)
            else:  # managed elsewhere until now; the renamed artifact needs its own version
                out = out.replace(f"<artifactId>{na}</artifactId>", f"<artifactId>{na}</artifactId><version>{want}</version>", 1)
            return out
        for tag in ("plugin", "dependency"):
            new = re.sub(r"<(" + tag + r")>(?:(?!<" + tag + r">).)*?</" + tag + r">", swap, new, flags=re.S)
        # jaxb2-basics + jaxb2-value-constructor both became jaxb-plugins: keep one per <dependencies> block
        def dedupe(m):
            blk, kept, seen = m.group(0), [], set()
            def one(d):
                key = (_first_tag(d.group(0), "groupId"), _first_tag(d.group(0), "artifactId"))
                if key[0] == "org.jvnet.jaxb" and key in seen:
                    return ""
                seen.add(key)
                return d.group(0)
            return re.sub(r"[ \t]*<dependency>(?:(?!<dependency>).)*?</dependency>[ \t]*\r?\n?", one, blk, flags=re.S)
        new = re.sub(r"<dependencies>.*?</dependencies>", dedupe, new, flags=re.S)
        if write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: javax-era JAXB/JAX-WS tooling -> Jakarta successors (org.jvnet.jaxb 4.x, jaxws-maven-plugin 4.x)")
    for p in walk(root):
        t = read(p)
        if not t:
            continue
        new = t
        if p.suffix in (".java", ".kt") and "org.jvnet.jaxb2_commons" in t:
            new = re.sub(r"\borg\.jvnet\.jaxb2_commons\.", "org.jvnet.jaxb.", new)
            for a, b in JAXB_RUNTIME_RENAMES.items():
                new = re.sub(r"\b" + a + r"\b", b, new)
        elif p.suffix in (".xjb", ".xml", ".wsdl", ".xsd") and p.name != "pom.xml" and re.search(
                r"http://java\.sun\.com/xml/ns/(?:jaxb|jaxws)|https://jakarta\.ee/xml/ns/jaxb/xjc", t):
            # Jakarta moved the JAXB and JAX-WS customization namespaces. The XJC extension namespace (xjc:javaType,
            # xjc:simple, ...) is RI-specific and did NOT move - wsimport/xjc reject https://jakarta.ee/xml/ns/jaxb/xjc.
            new = new.replace("https://jakarta.ee/xml/ns/jaxb/xjc", "http://java.sun.com/xml/ns/jaxb/xjc")  # repair earlier runs
            new = re.sub(r"http://java\.sun\.com/xml/ns/jaxb(?=[\"'])", "https://jakarta.ee/xml/ns/jaxb", new)
            new = re.sub(r"http://java\.sun\.com/xml/ns/jaxws(?=[\"'])", "https://jakarta.ee/xml/ns/jaxws", new)
            if re.search(r"https://jakarta\.ee/xml/ns/jax(?:b|ws)[\"']", new):
                # customization version 2.x -> 3.0 on every bindings element (JAXB and JAX-WS files) and on jaxb:version
                # attributes of inline customizations (<xs:schema jaxb:version="2.1"> in a WSDL/XSD)
                new = re.sub(r'(<(?:\w+:)?bindings\b[^>]*?\b(?:\w+:)?version=")2\.\d(")', r"\g<1>3.0\g<2>", new)
                new = re.sub(r'(\b\w+:version=")2\.\d(")', lambda m: m.group(0) if not re.search(r"jaxb", m.group(1)) else m.group(1) + "3.0" + m.group(2), new)
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: JAXB {'runtime classes' if p.suffix in ('.java', '.kt') else 'binding namespace'} -> Jakarta")
    if changes:
        todo.append("JAXB/JAX-WS code generation now uses the Jakarta tool chain (org.jvnet.jaxb 4.x / jaxws-maven-plugin 4.x): "
                    "compare the generated classes once (plugin options like -Xequals/-XtoString/-Xvalue-constructor keep their names)")
    return changes, todo


def boot_managed_versions(boot_version: str, local_repo: Path) -> dict:
    """{(groupId, artifactId): version} managed directly by spring-boot-dependencies (from the local repository)."""
    pom = local_repo / "org/springframework/boot/spring-boot-dependencies" / boot_version / f"spring-boot-dependencies-{boot_version}.pom"
    text = read(pom) if pom.exists() else None
    if not text:
        return {}
    props = _pom_props(text)
    dm = re.search(r"<dependencyManagement>(.*?)</dependencyManagement>", text, re.S)
    out = {}
    for d in re.finditer(r"<dependency>(.*?)</dependency>", dm.group(1) if dm else "", re.S):
        b = d.group(1)
        if "<scope>import</scope>" in b:
            continue
        v = _resolve_prop(_first_tag(b, "version"), props)
        if v and "${" not in v:
            out[(_first_tag(b, "groupId"), _first_tag(b, "artifactId"))] = v
    return out


def fix_downgraded_boot_managed(root: Path, boot_version: str | None, local_repo: Path) -> tuple[list[str], list[str]]:
    """Explicit versions OLDER than what Spring Boot manages (e.g. logback-classic 1.2.x, slf4j-api 1.7.x left from Boot 2)
    override Boot 3/4's versions and mix two generations of a library (AbstractMethodError, NoSuchMethodError ...).
    Remove them from dependencies/<dependencyManagement> so Boot's version applies; set plugin dependencies (which Boot's
    management does not reach) to Boot's version."""
    changes, todo = [], []
    managed = boot_managed_versions(boot_version, local_repo) if boot_version else {}
    if not managed:
        return [], []
    props = {}
    for bf in build_files(root):
        if bf.name == "pom.xml":
            props.update(_pom_props(read(bf) or ""))
    for bf in build_files(root):
        if bf.name != "pom.xml":
            continue
        t = read(bf) or ""
        plugin_spans = [m.span() for m in re.finditer(r"<plugin>.*?</plugin>", t, re.S)]
        edits = []
        for m in re.finditer(r"<dependency>((?:(?!<dependency>).)*?)</dependency>", t, re.S):
            b = m.group(1).split("<exclusions>")[0]
            key = (_first_tag(b, "groupId"), _first_tag(b, "artifactId"))
            raw = _first_tag(b, "version")
            if key not in managed or not raw:
                continue
            v = _resolve_prop(raw, props)
            if "${" in v or vtuple(v)[:2] >= vtuple(managed[key])[:2]:
                continue
            in_plugin = any(a <= m.start() < z for a, z in plugin_spans)
            blk = m.group(0)
            if in_plugin:
                new_blk = blk.replace(f"<version>{raw}</version>", f"<version>{managed[key]}</version>", 1)
                what = f"set to Boot's {managed[key]} (plugin dependency)"
            else:
                new_blk = re.sub(r"\s*<version>" + re.escape(raw) + r"</version>", "", blk, count=1)
                what = f"version removed, Boot manages {managed[key]}"
            edits.append((m.start(), m.end(), new_blk, f"{key[0]}:{key[1]} {v}: {what}"))
        new = t
        for a, z, blk, msg in sorted(edits, reverse=True):
            new = new[:a] + blk + new[z:]
            changes.append(f"{rel(bf, root)}: {msg}")
        if edits:
            write_if_changed(bf, t, new)
    if changes:
        todo.append("Explicit versions older than Spring Boot's were dropped (they mixed Boot 2 and Boot 3 jars): " +
                    "; ".join(c.split(": ", 1)[1] for c in changes[:6]))
    return changes, todo


# Hibernate 5 internals that moved in Hibernate 6 (verified against hibernate-core 6.6 and 7.4; OpenRewrite does not map them)
HIBERNATE6_TYPES = {
    "org.hibernate.query.spi.NativeQueryImplementor": "org.hibernate.query.sql.spi.NativeQueryImplementor",
    "org.hibernate.query.internal.NativeQueryImpl": "org.hibernate.query.sql.internal.NativeQueryImpl",
    "org.hibernate.query.internal.QueryImpl": "org.hibernate.query.sqm.internal.QuerySqmImpl",
    "org.hibernate.query.internal.AbstractProducedQuery": "org.hibernate.query.spi.AbstractQuery",
}


def fix_hibernate6_types(root: Path) -> list[str]:
    changes = []
    for p in java_sources(root):
        t = read(p) or ""
        if "org.hibernate.query" not in t:
            continue
        new = t
        for a, b in HIBERNATE6_TYPES.items():
            new = re.sub(re.escape(a) + r"\b", b, new)
        new = re.sub(r"\bAbstractProducedQuery\b", "AbstractQuery", new) if "org.hibernate.query.spi.AbstractQuery" in new else new
        new = re.sub(r"\bQueryImpl\b", "QuerySqmImpl", new) if "org.hibernate.query.sqm.internal.QuerySqmImpl" in new else new
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: Hibernate 5 internal query classes -> their Hibernate 6 packages")
    return changes


def _effective_java_levels(root: Path, exe: str, extra: list[str], env: dict) -> list[int]:
    """Java levels the compiler gets per module (maven-compiler-plugin release/source, from the effective POM)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "e.xml"
        code, _ = _effective_pom(root, exe, extra, env, out)
        text = read(out) if out.exists() else ""
    levels = []
    for pm in re.finditer(r"<plugin>(?:(?!<plugin>).)*?<artifactId>maven-compiler-plugin</artifactId>(.*?)</plugin>", text or "", re.S):
        conf = re.search(r"<configuration>(.*?)</configuration>", pm.group(1), re.S)
        c = conf.group(1) if conf else ""
        v = _first_tag(c, "release") or _first_tag(c, "source")
        m = re.match(r"(?:1\.)?(\d+)", v or "")
        if m:
            levels.append(int(m.group(1)))
    for m in re.finditer(r"<maven\.compiler\.(?:release|source)>(?:1\.)?(\d+)</maven\.compiler\.(?:release|source)>", text or ""):
        levels.append(int(m.group(1)))
    return levels


def fix_java_level(root: Path, java: int, exe: str, extra: list[str], env: dict) -> list[str]:
    """OpenRewrite's Java upgrade only edits the project's own POMs. When the Java level comes from a (company) parent,
    the code is rewritten for the new Java (e.g. pattern switches) but still compiled at the old one. Set it in the
    top POM, and override a fixed <release>/<source> of the compiler plugin if the parent hard-codes one."""
    top = root / "pom.xml"
    if not top.exists():
        return []
    levels = _effective_java_levels(root, exe, extra, env)
    if not levels or min(levels) >= java:
        return []
    changes = []
    t = read(top) or ""
    new = pom_set_property(t, "java.version", str(java))
    for prop in ("maven.compiler.release", "maven.compiler.source", "maven.compiler.target"):
        if f"<{prop}>" in new or prop == "maven.compiler.release":
            new = pom_set_property(new, prop, str(java))
    if write_if_changed(top, t, new):
        changes.append(f"pom.xml: Java level {min(levels)} (inherited) -> {java} (java.version / maven.compiler.release)")
    if min(_effective_java_levels(root, exe, extra, env) or [java]) < java:
        t = read(top) or ""
        plugin = ("      <plugin>\n        <groupId>org.apache.maven.plugins</groupId>\n        <artifactId>maven-compiler-plugin</artifactId>\n"
                  "        <configuration>\n          <release>${maven.compiler.release}</release>\n        </configuration>\n      </plugin>\n")
        if "<artifactId>maven-compiler-plugin</artifactId>" not in re.sub(r"<pluginManagement>.*?</pluginManagement>", "", t, flags=re.S):
            pl = _top_level(t, "build")
            if pl:
                m = re.search(r"<build>(?:(?!</build>).)*?<plugins>[ \t]*\n?", t[pl.start():], re.S)
                if m and "<pluginManagement>" not in t[pl.start():pl.start() + m.end()]:
                    pos = pl.start() + m.end()
                    new = t[:pos] + ("" if t[pos - 1] == "\n" else "\n") + plugin + t[pos:]
                else:
                    end = t.index("</build>", pl.start())
                    new = t[:end] + "    <plugins>\n" + plugin + "    </plugins>\n  " + t[end:]
            else:
                pos = t.rindex("</project>")
                new = t[:pos] + "  <build>\n    <plugins>\n" + plugin + "    </plugins>\n  </build>\n" + t[pos:]
            if write_if_changed(top, t, new):
                changes.append(f"pom.xml: maven-compiler-plugin <release> overridden to {java} (the parent hard-codes a lower level)")
    return changes


# ---- compile errors with a safe mechanical fix -----------------------------------------------------------------------
_DOMINATED_RX = re.compile(r"\[ERROR\] (/\S+?\.java):\[(\d+),\d+\] (?:this case label is dominated by a preceding case label|"
                           r"diese case-Marke wird von einer vorherigen case-Marke dominiert|"
                           r"[^\n]*(?:dominated|dominiert)[^\n]*)")


def _remove_switch_arm(text: str, line_no: int) -> str | None:
    """Remove the `case ...` arm that starts on line_no (1-based): up to the next case/default label or the switch's end."""
    lines = text.split("\n")
    i = line_no - 1
    if i >= len(lines) or not re.match(r"\s*case\b", lines[i]):
        return None
    ind = len(lines[i]) - len(lines[i].lstrip())
    j = i + 1
    while j < len(lines):
        l = lines[j]
        cur = len(l) - len(l.lstrip())
        if l.strip() and cur <= ind and (re.match(r"\s*(case\b|default\b)", l) or l.strip().startswith("}")):
            break
        j += 1
    return "\n".join(lines[:i] + lines[j:])


def autofix_compile_errors(root: Path, log: Path) -> list[str]:
    """Fix compile errors that have exactly one safe fix. A pattern-switch arm 'dominated by a preceding case label' can
    never match (OpenRewrite's if/else-instanceof -> switch conversion produces these when a later type is a subtype of an
    earlier one; in the if/else chain the branch was merely dead code): remove it."""
    changes = []
    hits: dict[str, set] = {}
    for m in _DOMINATED_RX.finditer(read(log) or ""):
        hits.setdefault(m.group(1), set()).add(int(m.group(2)))
    for f, line_nos in hits.items():
        p = Path(f)
        if not p.exists():
            continue
        t = read(p) or ""
        new = t
        for ln in sorted(line_nos, reverse=True):      # bottom-up keeps the other line numbers valid
            r = _remove_switch_arm(new, ln)
            if r is not None:
                new = r
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root) if str(p).startswith(str(root)) else p}: removed {len(line_nos)} unreachable switch arm(s) "
                           f"(dominated by a preceding case)")
    return changes


HTTPTRACE_JAVA = [
    ("org.springframework.boot.actuate.trace.http.InMemoryHttpTraceRepository", "org.springframework.boot.actuate.web.exchanges.InMemoryHttpExchangeRepository"),
    ("org.springframework.boot.actuate.trace.http.HttpTraceRepository", "org.springframework.boot.actuate.web.exchanges.HttpExchangeRepository"),
    ("org.springframework.boot.actuate.trace.http.HttpTrace", "org.springframework.boot.actuate.web.exchanges.HttpExchange"),
    ("org.springframework.boot.actuate.trace.http.Include", "org.springframework.boot.actuate.web.exchanges.Include"),
]


def fix_httptrace(root: Path) -> list[str]:
    changes = []
    for p in java_sources(root):
        t = read(p) or ""
        if "HttpTrace" not in t and "actuate.trace.http" not in t:
            continue
        new = t
        for a, b in HTTPTRACE_JAVA:
            new = new.replace(a, b)
        for a, b in (("InMemoryHttpTraceRepository", "InMemoryHttpExchangeRepository"), ("HttpTraceRepository", "HttpExchangeRepository"),
                     ("HttpTrace", "HttpExchange")):
            new = re.sub(r"\b" + a + r"\b", b, new)
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: HttpTrace* -> HttpExchange* (actuator httpexchanges)")
    for f in config_files(root):
        c = ConfigFile(f)
        hit = False
        for e in c.keys_matching(r"management\.trace\.http\.|management\.endpoint\.httptrace\.|management\.endpoints\.web\.exposure\.(include|exclude)$"):
            k, v = e["key"], e["value"]
            nk = k.replace("management.trace.http.", "management.httpexchanges.recording.").replace("management.endpoint.httptrace.", "management.endpoint.httpexchanges.")
            nv = re.sub(r"\bhttptrace\b", "httpexchanges", v) if "exposure" in k else v
            if (nk, nv) != (k, v):
                doc = c.remove(k); hit = True
                c.put(nk, nv, doc=doc or 0)
        if hit and c.save():
            changes.append(f"{rel(f, root)}: httptrace settings -> httpexchanges")
    return changes


TRAILING_SLASH_SERVLET = """package {pkg};

import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.web.filter.UrlHandlerFilter;

/**
 * Added by spring-migrate. Spring Framework 6 no longer matches "/orders/" to {{@code @GetMapping("/orders")}}.
 * This filter strips a trailing slash before Spring Security and Spring MVC see the request, so existing clients keep
 * working and security rules are evaluated against the same path the controller is mapped to.
 * Delete this class once no client relies on trailing slashes.
 */
@Configuration(proxyBeanMethods = false)
public class TrailingSlashCompatibilityConfig {{
    @Bean
    public FilterRegistrationBean<UrlHandlerFilter> trailingSlashCompatibilityFilter() {{
        FilterRegistrationBean<UrlHandlerFilter> registration =
                new FilterRegistrationBean<>(UrlHandlerFilter.trailingSlashHandler("/**").wrapRequest().build());
        registration.setOrder(Ordered.HIGHEST_PRECEDENCE);
        return registration;
    }}
}}
"""

TRAILING_SLASH_REACTIVE = """package {pkg};

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.web.filter.reactive.UrlHandlerFilter;

/**
 * Added by spring-migrate. Spring Framework 6 no longer matches "/orders/" to {{@code @GetMapping("/orders")}}.
 * This filter strips a trailing slash before Spring Security and WebFlux see the request.
 * Delete this class once no client relies on trailing slashes.
 */
@Configuration(proxyBeanMethods = false)
public class TrailingSlashCompatibilityConfig {{
    @Bean
    @Order(Ordered.HIGHEST_PRECEDENCE)
    public UrlHandlerFilter trailingSlashCompatibilityFilter() {{
        return UrlHandlerFilter.trailingSlashHandler("/**").mutateRequest().build();
    }}
}}
"""


def fix_trailing_slash(root: Path) -> list[str]:
    """One filter per Spring Boot application (multi-module builds can have several apps)."""
    changes = []
    for app in java_sources(root, main_only=True):
        t = read(app) or ""
        if "@SpringBootApplication" not in t:
            continue
        mod = module_of(app, root)
        bf = module_build_file(mod)
        builds = reactor_text(root, bf) if bf else ""
        servlet = re.search(r"spring-boot-starter-web(?![\w-])|spring-boot-starter-webmvc", builds)
        reactive = "spring-boot-starter-webflux" in builds and not servlet
        mod_src = " ".join(read(p) or "" for p in java_sources(mod))
        if not (servlet or reactive) or re.search(r"setUseTrailingSlashMatch\s*\(\s*true|UrlHandlerFilter", mod_src):
            continue
        pm = re.search(r"^\s*package\s+([\w.]+)\s*;", t, re.M)
        if not pm:
            continue
        target = app.parent / "TrailingSlashCompatibilityConfig.java"
        if target.exists():
            continue
        write_file(target, (TRAILING_SLASH_SERVLET if servlet else TRAILING_SLASH_REACTIVE).format(pkg=pm.group(1)))
        changes.append(f"{rel(target, root)}: keeps Spring 5 trailing-slash matching (UrlHandlerFilter, runs before Spring Security)")
    return changes


DIALECT_MAP = [
    (r"PostgreSQL(?:8[12]|9[0-5]?|10)Dialect", "PostgreSQLDialect"), (r"PostgresPlus\w*Dialect", "PostgresPlusDialect"),
    (r"MySQL(?:5|55|57|8)?(?:InnoDB|MyISAM)?Dialect", "MySQLDialect"), (r"MariaDB(?:10[0-9]*|53)?Dialect", "MariaDBDialect"),
    (r"Oracle(?:8i|9i|10g|12c)Dialect", "OracleDialect"), (r"SQLServer(?:2005|2008|2012|2016)Dialect", "SQLServerDialect"),
    (r"DB2(?:400)?V?\d*Dialect", "DB2Dialect"), (r"(?:H2|HSQL|Derby\w*)Dialect", None),
]


def fix_hibernate_props(root: Path) -> list[str]:
    changes = []
    for p in config_files(root) + [x for x in walk(root) if x.name == "persistence.xml"]:
        t = read(p) or ""
        new = t
        for rx, generic in DIALECT_MAP:
            if generic:
                new = re.sub(r"org\.hibernate\.dialect\." + rx + r"\b", "org.hibernate.dialect." + generic, new)
        new = re.sub(r"(?m)^[ \t]*[\w.\-]*new[_-]generator[_-]mappings[\w.\-]*\s*[=:].*\r?\n?", "", new)
        new = re.sub(r'(?m)^[ \t]*<property\s+name="hibernate\.id\.new_generator_mappings"[^>]*/>[ \t]*\r?\n?', "", new)
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: removed Hibernate 5 dialects/settings (version-specific dialect -> generic, new_generator_mappings dropped)")
    return changes


def _sdkman_java(major: int, vendor: str) -> str | None:
    if OFFLINE:
        return None
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.sdkman.io/2/candidates/java/linux/versions/all", timeout=15) as r:
            ids = r.read().decode().split(",")
    except Exception:
        return None
    c = [i for i in ids if re.match(rf"{major}(\.|$)", i) and i.endswith("-" + vendor) and "fx" not in i and "crac" not in i]
    return max(c, key=lambda v: vtuple(v.split("-")[0].replace("+", "."))) if c else None


def fix_containers_and_ci(root: Path, java: int) -> tuple[list[str], list[str]]:
    """Replaces what fix_ci_java only reported: deprecated openjdk images and JDK pins in other CI systems."""
    changes, todo = [], []
    img = lambda kind, alpine: f"eclipse-temurin:{java}-{kind}" + ("-alpine" if alpine else "")
    def images(text: str) -> str:
        text = re.sub(r"\bmaven:3(?:\.[\d.]+)?-(?:openjdk|jdk|adoptopenjdk|amazoncorretto|eclipse-temurin)-(?:8|11)(?:-slim)?\b",
                      f"maven:3.9-eclipse-temurin-{java}", text)
        text = re.sub(r"\bgradle:(\d+)(?:\.[\d.]+)?-jdk(?:8|11)\b", lambda m: f"gradle:8-jdk{java}" if int(m.group(1)) < 8 else f"gradle:{m.group(1)}-jdk{java}", text)
        text = re.sub(r"\b(?:docker\.io/)?(?:library/)?openjdk:(?:8|11|17|21)(?:[\w.\-]*)",
                      lambda m: img("jdk" if "jre" not in m.group(0) else "jre", "alpine" in m.group(0)), text)
        text = re.sub(r"\badoptopenjdk(?:/openjdk(?:8|11))?:[\w.\-]+", lambda m: img("jre" if "jre" in m.group(0) else "jdk", "alpine" in m.group(0)), text)
        return text
    for p in walk(root):
        t = read(p)
        if t is None:
            continue
        new = t
        is_docker = p.name == "Dockerfile" or p.name.startswith("Dockerfile.") or p.suffix == ".dockerfile"
        if is_docker:
            new = "\n".join(images(l) if l.lstrip().upper().startswith("FROM") else l for l in t.splitlines()) + ("\n" if t.endswith("\n") else "")
        elif p.name in ("Jenkinsfile", ".gitlab-ci.yml", "bitbucket-pipelines.yml", "azure-pipelines.yml", ".travis.yml", "docker-compose.yml",
                        "docker-compose.yaml", "compose.yml", "compose.yaml", ".drone.yml", "cloudbuild.yaml", ".circleci/config.yml") or \
                (p.parent.name == ".circleci" and p.suffix in (".yml", ".yaml")):
            new = images(t)
            new = re.sub(r"(\bjdk\s*\(?\s*['\"])(?:jdk-?)?(?:1\.)?(?:8|11)(['\"])", rf"\g<1>jdk{java}\g<2>", new)
            new = re.sub(r"(?m)^(\s*jdk:\s*)(?:open|oracle)jdk(?:8|11)\s*$", rf"\g<1>openjdk{java}", new)
            new = re.sub(r"(jdkVersionOption:\s*['\"]?)1\.(?:8|11)(['\"]?)", rf"\g<1>1.{java}\g<2>", new)
            new = re.sub(r"(versionSpec:\s*['\"]?)(?:8|11)(['\"]?)", rf"\g<1>{java}\g<2>", new)
            new = re.sub(r"\bcimg/openjdk:(?:8|11)[\w.]*", f"cimg/openjdk:{java}.0", new)
            if p.name == "Jenkinsfile" and new != t and re.search(r"\bjdk\s*\(?\s*['\"]jdk", new):
                todo.append(f"{rel(p, root)}: now asks Jenkins for tool `jdk{java}` - make sure that JDK tool is configured on the controller")
        elif p.name == ".sdkmanrc":
            m = re.search(r"(?m)^java=(?:1\.)?(?:8|11)[\w.+]*-(\w+)\s*$", t)
            if m:
                v = _sdkman_java(java, m.group(1))
                if v:
                    new = re.sub(r"(?m)^java=.*$", f"java={v}", t)
                else:
                    todo.append(f"{rel(p, root)}: pins JDK {m.group(0).split('=')[1]} - set a JDK {java} identifier (`sdk list java`)")
        elif p.name == ".tool-versions":
            m = re.search(r"(?m)^java\s+(\S+)", t)
            if m and re.search(r"(?:^|-)(?:1\.)?(?:8|11)(?:\.|$)", m.group(1)):
                vendor = m.group(1).split("-")[0] if "-" in m.group(1) else "temurin"
                new = re.sub(r"(?m)^java\s+\S+", f"java latest:{vendor}-{java}", t)
                todo.append(f"{rel(p, root)}: set to `latest:{vendor}-{java}`; run `asdf install java latest:{vendor}-{java}` and pin the exact version")
        if write_if_changed(p, t, new):
            changes.append(f"{rel(p, root)}: JDK images/tools -> Java {java}")
    return changes, todo


def fix_build_wrappers(root: Path, boot_line: str) -> list[str]:
    changes = []
    mw = root / ".mvn/wrapper/maven-wrapper.properties"
    if mw.exists():
        t = read(mw) or ""
        m = re.search(r"apache-maven-([\d.]+)-bin", t)
        if m and vtuple(m.group(1)) < (3, 6, 3):
            mv = latest_patch("org.apache.maven", "apache-maven", "3.9", "3.9.11")
            new = t.replace(m.group(1), mv)
            new = re.sub(r"(?m)^distributionSha256Sum=.*\r?\n?", "", new)
            if write_if_changed(mw, t, new):
                changes.append(f"{rel(mw, root)}: Maven wrapper {m.group(1)} -> {mv} (Boot 3+ needs 3.6.3+)")
    gw = root / "gradle/wrapper/gradle-wrapper.properties"
    if gw.exists():
        t = read(gw) or ""
        m = re.search(r"gradle-([\d.]+)-(bin|all)\.zip", t)
        need = (8, 14) if boot_line.startswith("4") else (7, 6, 4)
        if m and vtuple(m.group(1))[:len(need)] < need:
            gv = "8.14.3"
            new = t.replace(f"gradle-{m.group(1)}-", f"gradle-{gv}-")
            new = re.sub(r"(?m)^distributionSha256Sum=.*\r?\n?", "", new)
            if write_if_changed(gw, t, new):
                changes.append(f"{rel(gw, root)}: Gradle wrapper {m.group(1)} -> {gv} (Boot {boot_line} minimum)")
    return changes


def fix_spring_cloud_train(root: Path, boot_line: str) -> list[str]:
    changes = []
    for bf in build_files(root):
        t = read(bf) or ""
        if "spring-cloud" not in t:
            continue
        want = cloud_version_for(boot_line)
        new = re.sub(r"(<spring-cloud(?:-dependencies)?\.version>)[^<]+(</spring-cloud(?:-dependencies)?\.version>)", rf"\g<1>{want}\g<2>", t)
        new = re.sub(r"(<artifactId>spring-cloud-dependencies</artifactId>\s*<version>)(?!\$\{)[^<]+(</version>)", rf"\g<1>{want}\g<2>", new)
        new = re.sub(r"(springCloudVersion['\"]?\s*[,=:]\s*\(?\s*['\"])[^'\"]+(['\"])", rf"\g<1>{want}\g<2>", new)
        new = re.sub(r"(spring-cloud-dependencies:)[\w.\-]+(['\"])", rf"\g<1>{want}\g<2>", new)
        if write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: Spring Cloud release train -> {want} (matches Boot {boot_line})")
    gp = root / "gradle.properties"
    if gp.exists():
        t = read(gp) or ""
        new = re.sub(r"(?m)^(springCloudVersion\s*=\s*).*$", rf"\g<1>{cloud_version_for(boot_line)}", t)
        if write_if_changed(gp, t, new):
            changes.append(f"gradle.properties: springCloudVersion -> {cloud_version_for(boot_line)}")
    return changes


def bump_boot(root: Path, version: str) -> list[str]:
    changes = []
    for bf in build_files(root):
        t = read(bf) or ""
        new = re.sub(r"(<artifactId>spring-boot-(?:starter-parent|dependencies)</artifactId>\s*<version>)(?!\$\{)[^<]+(</version>)", rf"\g<1>{version}\g<2>", t)
        new = re.sub(r"(<spring-boot(?:-dependencies)?\.version>)[^<]+(</spring-boot(?:-dependencies)?\.version>)", rf"\g<1>{version}\g<2>", new)
        new = re.sub(r"(<artifactId>spring-boot-maven-plugin</artifactId>\s*<version>)(?!\$\{)[^<]+(</version>)", rf"\g<1>{version}\g<2>", new)
        new = re.sub(r"(id\s*\(?\s*['\"]org\.springframework\.boot['\"]\s*\)?\s*version\s*\(?\s*['\"])[^'\"]+(['\"])", rf"\g<1>{version}\g<2>", new)
        new = re.sub(r"(springBootVersion\s*=\s*['\"])[^'\"]+(['\"])", rf"\g<1>{version}\g<2>", new)
        new = re.sub(r"(spring-boot-(?:dependencies|gradle-plugin):)[\w.\-]+(['\"])", rf"\g<1>{version}\g<2>", new)
        if write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: Spring Boot -> {version}")
    for name in ("gradle.properties", "gradle/libs.versions.toml"):
        f = root / name
        if f.exists():
            t = read(f) or ""
            new = re.sub(r"(?m)^(\s*(?:springBootVersion|spring-boot|springBoot)\s*=\s*['\"]?)[\d.]+[\w.\-]*(['\"]?)", rf"\g<1>{version}\g<2>", t)
            if write_if_changed(f, t, new):
                changes.append(f"{name}: Spring Boot -> {version}")
    return changes


def fix_third_party(root: Path) -> list[str]:
    changes = []
    for bf in build_files(root):
        t = read(bf) or ""
        new = t
        m = re.search(r"<(org\.)?mapstruct\.version>([^<]+)<", new)
        if m and vtuple(m.group(2)) < (1, 5, 5):
            mv = latest_patch("org.mapstruct", "mapstruct", "1.6", "1.6.3")
            new = new.replace(m.group(0), m.group(0).replace(m.group(2), mv))
        # Lombok pinned inside annotationProcessorPaths: follow Boot's managed version
        new = re.sub(r"(<groupId>org\.projectlombok</groupId>\s*<artifactId>lombok</artifactId>\s*<version>)1\.18\.(?:[0-9]|1\d|2\d)(</version>)",
                     r"\g<1>${lombok.version}\g<2>", new)
        # Querydsl needs the jakarta classifier from Boot 3 on
        def qd(mm):
            b = mm.group(0)
            if "<classifier>" in b:
                return b
            return re.sub(r"(<artifactId>querydsl-(?:apt|jpa)</artifactId>)", r"\1\n      <classifier>jakarta</classifier>", b, count=1)
        new = re.sub(r"<(dependency|path)>(?:(?!<\1>).)*?<groupId>com\.querydsl</groupId>(?:(?!<\1>).)*?</\1>", qd, new, flags=re.S)
        new = re.sub(r"(['\"]com\.querydsl:querydsl-(?:apt|jpa)(?::[\d.]+)?)(['\"])", r"\1:jakarta\2", new) if "querydsl" in new and ":jakarta" not in new and bf.name != "pom.xml" else new
        if write_if_changed(bf, t, new):
            changes.append(f"{rel(bf, root)}: third-party libraries -> Jakarta-compatible versions (MapStruct/Lombok/Querydsl)")
    return changes


# ---- spring-boot-properties-migrator ----------------------------------------
PROPS_MIGRATOR = ("org.springframework.boot", "spring-boot-properties-migrator")


def add_properties_migrator(root: Path) -> dict[Path, str]:
    """Temporarily add spring-boot-properties-migrator (runtime) to every module that uses Boot. Returns originals."""
    saved = {}
    for bf in build_files(root):
        t = read(bf) or ""
        if "spring-boot-starter" not in t or "spring-boot-properties-migrator" in t:
            continue
        saved[bf] = t
        write_file(bf, build_add_dep(bf, t, *PROPS_MIGRATOR, scope="runtime"))
    return saved


def restore_files(saved: dict[Path, str]):
    for p, t in saved.items():
        write_file(p, t)


def parse_properties_migrator(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """-> ({old: replacement}, {key: reason}) from PropertiesMigrationListener output."""
    renames, gone = {}, {}
    for m in re.finditer(r"Key:\s*(\S+)\s+(?:Line:\s*\d+\s+)?Replacement:\s*(\S+)", text):
        renames[m.group(1)] = m.group(2)
    for m in re.finditer(r"Key:\s*(\S+)\s+(?:Line:\s*\d+\s+)?Reason:\s*([^\n]+)", text):
        if m.group(1) not in renames:
            gone[m.group(1)] = m.group(2).strip()
    return renames, gone


def collect_test_output(root: Path, log: Path) -> str:
    parts = [read(log) or ""]
    for p in root.rglob("*-output.txt"):
        if "surefire-reports" in p.as_posix() or "failsafe-reports" in p.as_posix():
            parts.append(read(p) or "")
    for p in root.rglob("TEST-*.xml"):
        if "build/test-results" in p.as_posix():
            parts.append(read(p) or "")
    return "\n".join(parts)


def apply_property_renames(root: Path, renames: dict[str, str]) -> list[str]:
    changes = []
    for f in config_files(root):
        c = ConfigFile(f)
        n = 0
        for old, newk in renames.items():
            for e in c.keys_matching(re.escape(old) + r"$"):
                doc = c.remove(e["key"])
                c.put(newk, e["value"], doc=doc or 0)
                n += 1
        if n and c.save():
            changes.append(f"{rel(f, root)}: {n} property rename(s) reported by spring-boot-properties-migrator")
    return changes


# ---- jars that still need javax.* -----------------------------------------
JAVAX_EE_BYTES = re.compile(rb"javax/(?:servlet|persistence|validation|transaction(?!/xa)|ws/rs|xml/bind|mail|jms|inject|json|websocket|el|enterprise|activation|interceptor|jws|annotation/(?:PostConstruct|PreDestroy|Resource|Priority))/")


def javax_jars(classpath: list[str], direct: set[str] | None = None) -> list[str]:
    """Jars declared directly in the build whose classes reference javax.* EE types and no jakarta.* at all.
    (Transitive libraries such as commons-logging or Micrometer keep optional javax integrations; those are not flagged.)"""
    import zipfile
    bad = []
    for jar in classpath:
        if not jar.endswith(".jar") or re.search(r"/javax/|/jakarta\.|/javax\.", jar):
            continue
        if direct is not None and not any(Path(jar).name.startswith(a + "-") for a in direct):
            continue
        try:
            with zipfile.ZipFile(jar) as z:
                javax = jakarta = False
                for n in z.namelist():
                    if n.endswith(".class") and not n.startswith(("META-INF/versions/",)):
                        b = z.read(n)
                        javax = javax or JAVAX_EE_BYTES.search(b) is not None
                        jakarta = jakarta or b"jakarta/" in b
                        if jakarta:
                            break
                if javax and not jakarta:
                    bad.append(jar)
        except Exception:
            continue
    return bad


# --------------------------------------------------------------------------- #
# Company / internal dependencies (Artifactory, Nexus ...)
#
# A company parent POM or BOM that brings Spring Boot, and internal libraries built against one Boot line, are not
# upgraded by OpenRewrite. We resolve them through the project's own Maven (so settings.xml, mirrors and encrypted
# credentials work), classify every available version by what it was built against, and pick per stage the newest
# version that matches. The result is a plan file you can review/edit (or answer interactively).
# --------------------------------------------------------------------------- #
DEP_PLAN_FILE = "dependency-plan.json"
# Fully qualified goals: an unqualified `dependency:resolve` makes Maven look up the plugin's latest version in every
# repository first (slow behind a corporate Artifactory).
DEP_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"
HIBERNATE_LINE = "2.7-hibernate"   # plan key applied in the OpenJPA -> Hibernate stage (Boot 2.7, javax)
PLAN_LINES = (HIBERNATE_LINE, "3.5", "4.0", "4.1")
# handled by OpenRewrite / the codemods / Boot's own dependency management
DEP_SKIP_GROUPS = ("org.springframework", "io.springfox", "org.springdoc", "org.slf4j", "ch.qos.logback", "org.apache.logging.log4j", "com.netflix", "org.powermock", "org.apache.openjpa", "org.apache.httpcomponents",
                   "javax.", "jakarta.", "org.junit", "junit", "org.openrewrite", "io.micrometer", "org.hibernate", "com.h2database")
PUBLIC_GROUPS = ("org.apache", "com.fasterxml", "com.google", "org.projectlombok", "org.mockito", "org.postgresql", "com.mysql", "mysql",
                 "org.mariadb", "io.github.resilience4j", "org.springdoc", "org.slf4j", "ch.qos", "org.yaml", "org.assertj", "org.hamcrest",
                 "org.flywaydb", "org.liquibase", "io.projectreactor", "io.netty", "org.aspectj", "org.mapstruct", "com.querydsl",
                 "org.testcontainers", "io.swagger", "org.thymeleaf", "org.webjars", "com.zaxxer", "org.jboss", "org.glassfish", "org.eclipse",
                 "com.sun", "org.codehaus", "commons-", "io.jsonwebtoken", "org.bouncycastle", "com.nimbusds", "net.", "io.", "com.github.")


@dataclass
class DepRef:
    kind: str                 # parent | bom | dependency
    group: str
    artifact: str
    version: str
    prop: str | None          # property that holds the version, if any
    files: list = field(default_factory=list)


def _pom_props(text: str) -> dict:
    m = re.search(r"<properties>(.*?)</properties>", text, re.S)
    return dict(re.findall(r"<([\w.\-]+)>\s*([^<]*?)\s*</\1>", m.group(1))) if m else {}


def collect_dependency_refs(root: Path) -> list[DepRef]:
    """Parents, imported BOMs and explicitly versioned dependencies that are not Spring/Boot itself."""
    refs: dict[tuple, DepRef] = {}
    props: dict[str, str] = {}
    poms = [p for p in build_files(root) if p.name == "pom.xml"]
    for p in poms:
        props.update(_pom_props(read(p) or ""))

    own = reactor_coords(root)

    def add(kind, g, a, v, p):
        if not (g and a and v) or g.startswith(DEP_SKIP_GROUPS) or (g, a) in own:
            return
        prop = None
        m = re.fullmatch(r"\$\{([\w.\-]+)\}", v.strip())
        if m:
            prop = m.group(1)
            v = props.get(prop, "")
            if not v or "${" in v:
                return
        r = refs.setdefault((kind, g, a), DepRef(kind, g, a, v.strip(), prop))
        r.files.append(rel(p, root))
    for p in poms:
        t = read(p) or ""
        pm = re.search(r"<parent>(.*?)</parent>", t, re.S)
        if pm:
            add("parent", _first_tag(pm.group(1), "groupId"), _first_tag(pm.group(1), "artifactId"), _first_tag(pm.group(1), "version"), p)
        for dm in re.finditer(r"<dependencyManagement>(.*?)</dependencyManagement>", t, re.S):
            for d in re.finditer(r"<dependency>(.*?)</dependency>", dm.group(1), re.S):
                b = d.group(1)
                kind = "bom" if "<scope>import</scope>" in b else "dependency"
                add(kind, _first_tag(b, "groupId"), _first_tag(b, "artifactId"), _first_tag(b, "version"), p)
        for s, e, g, a in pom_dependency_blocks(t):
            add("dependency", g, a, _first_tag(t[s:e].split("<exclusions>")[0], "version"), p)
        # dependencies of plugins (exec:java generators, code generators ...) run with the build: a company library pinned
        # there would mix Boot 2 jars (e.g. Logback 1.2) into a Boot 3 build
        for pm in re.finditer(r"<plugin>.*?</plugin>", t, re.S):
            for d in re.finditer(r"<dependency>(.*?)</dependency>", pm.group(0), re.S):
                b = d.group(1).split("<exclusions>")[0]
                add("dependency", _first_tag(b, "groupId"), _first_tag(b, "artifactId"), _first_tag(b, "version"), p)
    for p in build_files(root):
        if p.name == "pom.xml":
            continue
        for m in re.finditer(r"['\"]([\w.\-]+):([\w.\-]+):([\w.\-]+)['\"]", read(p) or ""):
            add("dependency", m.group(1), m.group(2), m.group(3), p)
    return list(refs.values())


def is_internal(group: str, internal_groups: list[str], root_group: str | None) -> bool:
    if any(group == g or group.startswith(g + ".") for g in internal_groups):
        return True
    if root_group:
        top = ".".join(root_group.split(".")[:2])
        if group == top or group.startswith(top + "."):
            return True
    return not group.startswith(PUBLIC_GROUPS)


class MavenRepo:
    """Resolves artifacts through `mvn` with the project's settings (Artifactory/Nexus auth, mirrors) into the local repo."""

    def __init__(self, root: Path, exe: str, extra: list[str], env: dict, log_dir: Path):
        self.root, self.exe, self.extra, self.env, self.log_dir = root, exe, extra, env, log_dir
        self._local = None
        self.timings: list[tuple] = []
        self.problems: dict[str, str] = {}
        self.traffic: dict[str, dict] = {}
        self.refresh = False

    def local(self) -> Path:
        """Local repository from -Dmaven.repo.local, the -s settings file, ~/.m2/settings.xml or $M2_HOME - without
        starting Maven (help:evaluate costs a JVM start plus plugin resolution)."""
        if self._local is None:
            self._local = local_repository(self.extra, self.env)
        return self._local

    def _repositories_block(self) -> str:
        t = read(self.root / "pom.xml") or ""
        m = re.search(r"<repositories>.*?</repositories>", t, re.S)
        return m.group(0) if m else ""

    def resolve(self, items: list[tuple], name: str, update: bool = False):
        """items: (group, artifact, version-or-range, type). Failures of single items are tolerated."""
        if not items:
            return
        work = self.log_dir / f"resolve-{name}"
        work.mkdir(parents=True, exist_ok=True)
        for i, (g, a, v, t) in enumerate(items):   # one pom per item, so a missing artifact does not hide the others
            d = work / str(i)
            d.mkdir(exist_ok=True)
            dep = (f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version><type>{t}</type>"
                   f"<exclusions><exclusion><groupId>*</groupId><artifactId>*</artifactId></exclusion></exclusions></dependency>")
            (d / "pom.xml").write_text(
                '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
                f'<groupId>spring-migrate</groupId><artifactId>probe-{i}</artifactId><version>1</version><packaging>pom</packaging>'
                f'{self._repositories_block()}<dependencies>{dep}</dependencies></project>', encoding="utf-8")
        # a reactor build resolves everything in one Maven start
        modules = "".join(f"<module>{i}</module>" for i in range(len(items)))
        (work / "pom.xml").write_text(
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
            '<groupId>spring-migrate</groupId><artifactId>probe</artifactId><version>1</version><packaging>pom</packaging>'
            f'<modules>{modules}</modules></project>', encoding="utf-8")
        # -T: the probes are independent, resolve them concurrently (network-latency bound).
        # -U only where new versions must be seen (metadata); exact versions need no update check.
        # No -U by default: Maven re-reads version lists older than the repositories' update policy (daily) anyway, and
        # forcing it makes Artifactory recompute every list across all its remote repositories.
        # Transfer progress stays on (no -ntp): it tells which repository the time goes to.
        cmd = [self.exe, "-B", "-fae", "-T", str(max(1, min(8, len(items)))), "-f", str(work / "pom.xml"), f"{DEP_PLUGIN}:resolve"]
        if update and self.refresh:
            cmd.insert(1, "-U")
        log = self.log_dir / f"resolve-{name}.log"
        t0 = time.time()
        run(cmd + self.extra, work, log, self.env)
        self.timings.append((name, len(items), time.time() - t0))
        self.problems.update(repository_problems(log))
        for repo_id, st in repository_traffic(log).items():
            agg = self.traffic.setdefault(repo_id, {"requests": 0, "seconds": 0.0, "bytes": 0})
            for k in agg:
                agg[k] += st[k]

    def versions(self, g: str, a: str) -> list[str]:
        d = self.local() / g.replace(".", "/") / a
        vs = set()
        for m in d.glob("maven-metadata*.xml"):
            vs.update(re.findall(r"<version>([^<]+)</version>", read(m) or ""))
        return sorted((v for v in vs if "SNAPSHOT" not in v), key=vtuple)

    def path(self, g, a, v, ext) -> Path:
        return self.local() / g.replace(".", "/") / a / v / f"{a}-{v}.{ext}"


def local_repository(extra: list[str], env: dict) -> Path:
    for i, a in enumerate(extra):
        if a.startswith("-Dmaven.repo.local="):
            return Path(a.split("=", 1)[1]).expanduser()
    settings = []
    for i, a in enumerate(extra):
        if a in ("-s", "--settings") and i + 1 < len(extra):
            settings.append(Path(extra[i + 1]).expanduser())
        elif a.startswith("--settings="):
            settings.append(Path(a.split("=", 1)[1]).expanduser())
    settings.append(Path.home() / ".m2/settings.xml")
    for home in (env.get("MAVEN_HOME"), env.get("M2_HOME")):
        if home:
            settings.append(Path(home) / "conf/settings.xml")
    for f in settings:
        m = re.search(r"<localRepository>\s*([^<]+?)\s*</localRepository>", read(f) or "") if f.exists() else None
        if m:
            v = re.sub(r"\$\{user\.home\}", str(Path.home()), m.group(1))
            v = re.sub(r"\$\{env\.(\w+)\}", lambda x: env.get(x.group(1), ""), v)
            return Path(v).expanduser()
    return Path.home() / ".m2/repository"


_SIZE = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9}


def repository_traffic(log: Path) -> dict[str, dict]:
    """Per repository: downloads and estimated transfer time, from Maven's 'Downloaded from <id>: url (1.2 kB at 3.4 kB/s)'."""
    out: dict[str, dict] = {}
    for m in re.finditer(r"Downloaded from ([\w.\-]+): \S+ \(([\d.,]+) (B|kB|KB|MB|GB) at ([\d.,]+) (B|kB|KB|MB|GB)/s\)", read(log) or ""):
        size = float(m.group(2).replace(",", ".")) * _SIZE[m.group(3)]
        speed = float(m.group(4).replace(",", ".")) * _SIZE[m.group(5)]
        st = out.setdefault(m.group(1), {"requests": 0, "seconds": 0.0, "bytes": 0})
        st["requests"] += 1
        st["bytes"] += int(size)
        st["seconds"] += size / speed if speed else 0.0
    return out


def repository_problems(log: Path) -> dict[str, str]:
    """Repositories that failed or timed out during resolution -> reason (these are what make resolution slow)."""
    out = {}
    text = read(log) or ""
    for m in re.finditer(r"(?:Could not transfer|Failed to transfer|Could not (?:resolve|retrieve)).*?(?:from/to|from) ([\w.\-]+) \(([^)]+)\)[:\s]*([^\n]*)", text):
        reason = m.group(3).strip()
        if not re.search(r"\b404\b|not found|was cached in the local repository", reason, re.I):   # missing artifacts are normal
            out[f"{m.group(1)} ({m.group(2)})"] = reason[:160]
    return out


def _resolve_prop(v: str, props: dict) -> str:
    for _ in range(5):
        v2 = re.sub(r"\$\{([\w.\-]+)\}", lambda m: props.get(m.group(1), m.group(0)), v)
        if v2 == v:
            break
        v = v2
    return v


def built_against(repo: MavenRepo, g: str, a: str, v: str, depth: int = 0) -> dict:
    """What a published artifact was built against: {'boot': '3.5.16'|None, 'spring': '6'|None, 'ee': 'javax'|'jakarta'|None,
    'autoconfig': 'spring.factories'|'imports'|None, 'found': bool}."""
    info = {"boot": None, "spring": None, "ee": None, "autoconfig": None, "found": False, "chain": [], "openjpa": False, "hibernate": None}
    pom = repo.path(g, a, v, "pom")
    text = read(pom) if pom.exists() else None
    if text is None:
        return info
    info["found"] = True
    props = _pom_props(text)
    props.setdefault("project.version", v)
    pm = re.search(r"<parent>(.*?)</parent>", text, re.S)
    if pm:
        pg, pa, pv = (_first_tag(pm.group(1), x) for x in ("groupId", "artifactId", "version"))
        info["chain"].append(f"{pg}:{pa}:{pv}")
        props.setdefault("project.parent.version", pv)
        if pg == "org.springframework.boot" and pa in ("spring-boot-starter-parent", "spring-boot-dependencies", "spring-boot-parent"):
            info["boot"] = pv
        elif depth < 8:
            up = built_against(repo, pg, pa, pv, depth + 1)
            info["boot"] = info["boot"] or up["boot"]
            info["spring"] = up["spring"]
            info["ee"] = up["ee"]
            info["chain"] += up["chain"]
            info["openjpa"] = up["openjpa"]          # dependencies declared in a parent are inherited
            info["hibernate"] = up["hibernate"]
    for d in re.finditer(r"<dependency>(.*?)</dependency>", text, re.S):
        b = d.group(1)
        dg, da, dv = _first_tag(b, "groupId"), _first_tag(b, "artifactId"), _resolve_prop(_first_tag(b, "version"), props)
        if dg == "org.springframework.boot" and dv and "${" not in dv and not info["boot"]:
            info["boot"] = dv
        if dg == "org.springframework" and dv and "${" not in dv and da.startswith("spring-"):
            info["spring"] = dv.split(".")[0]
        if dg == "org.apache.openjpa":
            info["openjpa"] = True
        if da == "hibernate-core" and dg in ("org.hibernate", "org.hibernate.orm"):
            info["hibernate"] = dv.split(".")[0] if dv and "${" not in dv else ("6+" if dg == "org.hibernate.orm" else "5")
        if dg.startswith("jakarta.") or dg == "org.glassfish.jakarta":
            info["ee"] = "jakarta"
        elif re.match(r"javax\.(servlet|persistence|validation|transaction|ws\.rs|xml\.bind|mail|jms|inject)", dg) and info["ee"] != "jakarta":
            info["ee"] = "javax"
    jar = repo.path(g, a, v, "jar")
    if jar.exists():
        import zipfile
        try:
            with zipfile.ZipFile(jar) as z:
                names = set(z.namelist())
                if "META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports" in names:
                    info["autoconfig"] = "imports"
                elif "META-INF/spring.factories" in names and b"EnableAutoConfiguration" in z.read("META-INF/spring.factories"):
                    info["autoconfig"] = "spring.factories"
                javax = jakarta = uses_hibernate = False
                for n in names:
                    if n.endswith(".class") and not n.startswith("META-INF/versions/"):
                        bts = z.read(n)
                        # OpenJPA API use or enhanced entities (PersistenceCapable is added by the enhancer)
                        info["openjpa"] = info["openjpa"] or b"org/apache/openjpa/" in bts
                        uses_hibernate = uses_hibernate or b"org/hibernate/" in bts
                        javax = javax or JAVAX_EE_BYTES.search(bts) is not None
                        jakarta = jakarta or re.search(rb"jakarta/(?:servlet|persistence|validation|transaction|ws/rs|xml/bind|annotation|inject)/", bts) is not None
                info["ee"] = "jakarta" if jakarta else ("javax" if javax else info["ee"])
                info["uses_hibernate"] = uses_hibernate
        except Exception:
            pass
    return info


def hibernate_of(info: dict) -> str | None:
    """Hibernate major a library was built for (explicit dependency, else implied by its Boot line)."""
    if info.get("hibernate") and info["hibernate"] not in ("6+",):
        return info["hibernate"]
    if info.get("boot"):
        return {"2": "5", "3": "6", "4": "7"}.get(info["boot"].split(".")[0])
    return info.get("hibernate")


def boot_line_of(info: dict) -> str:
    """'2', '3', '4', '3+' (Jakarta, Boot line unknown), 'neutral' (no Spring/EE coupling) or 'unknown'."""
    if not info["found"]:
        return "unknown"
    if info["boot"]:
        return info["boot"].split(".")[0]
    if info["spring"]:
        return {"5": "2", "6": "3", "7": "4"}.get(info["spring"], "unknown")
    if info["ee"] == "jakarta" or info["autoconfig"] == "imports":
        return "3+"
    if info["ee"] == "javax" or info["autoconfig"] == "spring.factories":
        return "2"
    return "neutral"


def _accepts(line: str, cls: str, info: dict, target: str) -> int:
    """Score how well a version fits a stage's Boot line (0 = not acceptable)."""
    if info.get("openjpa"):
        return 0  # the migration removes OpenJPA; a version that still needs it cannot work
    if line == HIBERNATE_LINE:  # OpenJPA stage: still Boot 2.7 / javax, but on Hibernate
        return {"2": 3, "neutral": 1}.get(cls, 0)
    major = line.split(".")[0]
    if cls == major:
        bv = info.get("boot")
        if bv and vtuple(bv)[:2] > vtuple(target)[:2]:
            return 0  # never go past the requested target
        if major == "3" and bv and vtuple(bv)[:2] > vtuple(line)[:2]:
            return 0
        return 3
    if cls == "3+":
        return 2
    if cls == "neutral":
        return 1
    if major == "4" and cls == "3":
        return 0
    return 0


DEP_CACHE_HOURS = 24


def _dep_cache_file(refs, target: str, openjpa_stage: bool, extra: list[str]) -> Path:
    import hashlib
    key = json.dumps([sorted((r.kind, r.group, r.artifact, r.version) for r in refs), target, openjpa_stage, extra], sort_keys=True)
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "spring-migrate" / "dependency-plans"
    return base / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")


def detect_dependency_plan(root: Path, repo: MavenRepo, target: str, internal_groups: list[str], say_=None,
                           openjpa_stage: bool = False) -> dict:
    """Cached for DEP_CACHE_HOURS per (references, target, Maven args): re-runs and resumes do not ask the repositories again."""
    say_ = say_ or say
    refs = collect_dependency_refs(root)
    cache = _dep_cache_file(refs, target, openjpa_stage, repo.extra) if refs else None
    if cache and cache.exists() and not repo.refresh and time.time() - cache.stat().st_mtime < DEP_CACHE_HOURS * 3600:
        try:
            plan = json.loads(cache.read_text(encoding="utf-8"))
            plan["internalGroups"] = internal_groups
            age = (time.time() - cache.stat().st_mtime) / 60
            say_(f"[deps] {len(refs)} reference(s): using the check from {age:.0f} min ago (cached; --refresh-dependency-versions to ask the repositories again)")
            return plan
        except (ValueError, OSError):
            pass
    plan = _detect_dependency_plan(root, repo, target, internal_groups, say_, openjpa_stage, refs)
    if cache and plan.get("dependencies") and not any(d.get("status", "").startswith("could not be resolved") for d in plan["dependencies"]):
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(plan), encoding="utf-8")
        except OSError:
            pass
    return plan


def _detect_dependency_plan(root: Path, repo: MavenRepo, target: str, internal_groups: list[str], say_, openjpa_stage: bool,
                            refs: list) -> dict:
    root_pom = re.sub(r"<parent>.*?</parent>", "", read(root / "pom.xml") or "", flags=re.S)
    root_group = _first_tag(root_pom, "groupId") or None
    lines = [l for l in ("3.5", "4.0", "4.1") if vtuple(l) <= vtuple(target)]
    if not refs:
        say_("[deps] no company parent, BOM or explicitly versioned library outside Spring found")
        return {"target": target, "internalGroups": internal_groups, "lines": [], "dependencies": []}
    say_(f"[deps] checking {len(refs)} parent/BOM/dependency reference(s) through your Maven settings")
    # 1) what the CURRENT versions were built against - usually already in the local repository from your builds.
    #    Only references coupled to Spring / Java EE / OpenJPA need a new version; the rest are not looked up at all.
    cur_items = [(r.group, r.artifact, r.version, "pom" if r.kind != "dependency" else "jar") for r in refs]
    missing = [it for it in cur_items if not repo.path(it[0], it[1], it[2], "pom").exists()
               or (it[3] == "jar" and not repo.path(it[0], it[1], it[2], "jar").exists())]
    if missing:
        say_(f"[deps]   1/3 fetching {len(missing)} current version(s) not in your local repository")
        repo.resolve(missing, "current")
    current = {(r.kind, r.group, r.artifact): built_against(repo, r.group, r.artifact, r.version) for r in refs}
    coupled = [r for r in refs if boot_line_of(current[(r.kind, r.group, r.artifact)]) not in ("neutral",)
               or current[(r.kind, r.group, r.artifact)]["openjpa"]]
    skipped = [r for r in refs if r not in coupled]
    if skipped:
        say_(f"[deps]   {len(skipped)} without Spring/Java EE coupling, left as they are: {', '.join(r.artifact for r in skipped[:8])}"
             f"{' ...' if len(skipped) > 8 else ''}")
    candidates = {(r.kind, r.group, r.artifact): [r.version] for r in skipped}
    if coupled:
        # 2) version lists. A RELEASE lookup reads maven-metadata.xml (all versions) and downloads ONE pom; a version
        #    range like [0,) would make Maven download the pom of every version in the range.
        say_(f"[deps]   2/3 reading available versions of {len(coupled)}: {', '.join(r.artifact for r in coupled[:6])}"
             f"{' ...' if len(coupled) > 6 else ''}")
        repo.resolve([(r.group, r.artifact, "RELEASE", "pom") for r in coupled], "metadata", update=True)
        to_fetch = []
        for r in coupled:
            newer = [v for v in repo.versions(r.group, r.artifact) if vtuple(v) > vtuple(r.version) and re.match(r"^[\d.]+(?:[.-](?:RELEASE|Final|GA))?$", v)]
            by_major: dict[str, str] = {}
            for v in newer:
                by_major[v.split(".")[0]] = v       # sorted ascending -> keeps the latest of each major
            cands = [r.version] + list(by_major.values())
            candidates[(r.kind, r.group, r.artifact)] = cands
            to_fetch += [(r.group, r.artifact, v, "pom" if r.kind != "dependency" else "jar") for v in cands[1:]]
        # 3) only the newest release of each newer major line, to see what it was built against
        to_fetch = [it for it in sorted(set(to_fetch)) if not repo.path(it[0], it[1], it[2], "pom").exists()
                    or (it[3] == "jar" and not repo.path(it[0], it[1], it[2], "jar").exists())]
        if to_fetch:
            say_(f"[deps]   3/3 downloading {len(to_fetch)} candidate version(s) to see what they were built against")
            repo.resolve(to_fetch, "candidates")
    for name, n, secs in repo.timings:
        say_(f"[deps]   {name}: {n} artifact(s) in {secs:.0f}s (log: {repo.log_dir / ('resolve-' + name + '.log')})")
    for repo_id, st in sorted(repo.traffic.items(), key=lambda kv: -kv[1]["seconds"])[:5]:
        say_(f"[deps]   repository {repo_id}: {st['requests']} download(s), ~{st['seconds']:.0f}s transferring")
    for r_, why in repo.problems.items():
        say_(f"[deps]   slow/unreachable repository {r_}: {why}")
    if repo.problems:
        say_("[deps]   -> every lookup waits for these. Add a <mirror> for them in your settings.xml (as your normal build does), "
             "or pass the same settings with --build-args \"-s settings.xml\"")
    all_infos = {(r.kind, r.group, r.artifact): {v: built_against(repo, r.group, r.artifact, v) for v in candidates[(r.kind, r.group, r.artifact)]}
                 for r in refs}
    via_libs = any(i[r.version]["openjpa"] for r, i in ((r, all_infos[(r.kind, r.group, r.artifact)]) for r in refs))
    if openjpa_stage or via_libs:
        lines = [HIBERNATE_LINE] + lines
    plan = {"target": target, "internalGroups": internal_groups, "lines": lines, "openjpaViaDependencies": via_libs, "dependencies": []}
    for r in refs:
        cands = candidates[(r.kind, r.group, r.artifact)]
        infos = all_infos[(r.kind, r.group, r.artifact)]
        cur = infos[r.version]
        cur_cls = boot_line_of(cur)
        entry = {"coordinate": f"{r.group}:{r.artifact}", "kind": r.kind, "current": r.version, "versionProperty": r.prop,
                 "files": r.files, "internal": is_internal(r.group, internal_groups, root_group),
                 "builtAgainst": {v: _describe(i) for v, i in infos.items()}, "upgrades": {}, "status": "",
                 "usesOpenJPA": cur["openjpa"]}
        if cur_cls in ("neutral",) and not cur["openjpa"]:
            entry["status"] = "no Spring/Java EE coupling - no change needed"
        elif cur_cls == "unknown":
            entry["status"] = "could not be resolved - check --build-args / settings.xml"
        else:
            missing = []
            for line in lines:
                best = None
                for v in sorted(cands, key=vtuple):
                    sc = _accepts(line, boot_line_of(infos[v]), infos[v], target)
                    if sc and (best is None or (sc, vtuple(v)) >= (best[0], vtuple(best[1]))):
                        best = (sc, v)
                if best and best[1] != r.version:
                    entry["upgrades"][line] = best[1]
                elif not best:
                    missing.append(line)
            if missing and not entry["upgrades"]:
                entry["status"] = f"NO compatible version found for Boot {', '.join(missing)} - needs a new release of this library"
            elif missing:
                entry["status"] = f"upgrade available, but nothing built for Boot {', '.join(missing)} (previous choice is kept there)"
            elif entry["upgrades"]:
                entry["status"] = "upgrade"
            else:
                entry["status"] = "current version already fits"
            if cur["openjpa"]:
                entry["status"] = "uses OpenJPA (removed by the migration); " + entry["status"]
            for line, v in entry["upgrades"].items():
                i = infos.get(v)
                if line.startswith("4") and i and i.get("uses_hibernate") and hibernate_of(i) in ("5", "6"):
                    entry["status"] += f"; {v} was built for Hibernate {hibernate_of(i)} and calls Hibernate APIs - Boot {line} ships Hibernate 7, test it"
        plan["dependencies"].append(entry)
    return plan


def _describe(i: dict) -> str:
    if not i["found"]:
        return "not resolvable"
    parts = []
    if i["boot"]:
        parts.append(f"Boot {i['boot']}")
    elif i["spring"]:
        parts.append(f"Spring {i['spring']}")
    if i["ee"]:
        parts.append(i["ee"])
    if i["autoconfig"]:
        parts.append(f"autoconfig via {i['autoconfig']}")
    if i.get("openjpa"):
        parts.append("OpenJPA")
    elif i.get("uses_hibernate") or i.get("hibernate"):
        parts.append(f"Hibernate {hibernate_of(i) or '?'}")
    return ", ".join(parts) or "no Spring/EE coupling"


def plan_lines(plan: dict) -> list[str]:
    if plan.get("lines"):
        return plan["lines"]
    keys = {k for d in plan.get("dependencies", []) for k in d.get("upgrades", {})}
    return [l for l in PLAN_LINES if l in keys or (l != HIBERNATE_LINE and vtuple(l) <= vtuple(plan.get("target", "4.1")))]


def render_dependency_plan(plan: dict) -> str:
    lines = plan_lines(plan)
    label = lambda l: "Boot 2.7 + Hibernate" if l == HIBERNATE_LINE else f"Boot {l}"
    L = ["## Company / internal dependencies", "",
         "| Dependency | Kind | Current | Built against | " + " | ".join(label(l) for l in lines) + " | Status |",
         "|---|---|---|---|" + "---|" * len(lines) + "---|"]
    for d in plan["dependencies"]:
        cols = [d["upgrades"].get(l, "-") for l in lines]
        L.append(f"| `{d['coordinate']}`{' (internal)' if d.get('internal') else ''} | {d['kind']} | {d['current']} | "
                 f"{d['builtAgainst'].get(d['current'], '')} | " + " | ".join(cols) + f" | {d['status']} |")
    L += ["", f"Edit `{DEP_PLAN_FILE}` (the `upgrades` per Boot line) and pass it with `--dependency-plan` to override.", ""]
    return "\n".join(L)


def interactive_plan(plan: dict, inp=input, out=print) -> dict:
    """Ask for every dependency that has Spring coupling which version to use per Boot line."""
    for d in plan["dependencies"]:
        if d["status"].startswith("no Spring") and not d["upgrades"]:
            continue
        out(f"\n{d['coordinate']} ({d['kind']}, current {d['current']}): {d['status']}")
        for v, desc in d["builtAgainst"].items():
            out(f"    {v:<14} {desc}")
        for line in plan_lines(plan):
            sugg = d["upgrades"].get(line, d["current"])
            ans = inp(f"  version for {'Boot 2.7 + Hibernate (OpenJPA stage)' if line == HIBERNATE_LINE else 'Boot ' + line} [{sugg}] (enter = accept, 'skip' = keep {d['current']}): ").strip()
            if ans.lower() == "skip":
                d["upgrades"].pop(line, None)
            elif ans:
                d["upgrades"][line] = ans
        d["status"] = "chosen interactively"
    return plan


def apply_dependency_plan(root: Path, plan: dict, line: str) -> list[str]:
    """Set the versions the plan chose for this Boot line (parent, BOM import, dependency, or the version property)."""
    changes = []
    for d in plan.get("dependencies", []):
        v = d.get("upgrades", {}).get(line)
        if not v:
            continue
        g, a = d["coordinate"].split(":")[:2]
        for bf in build_files(root):
            t = read(bf) or ""
            if a not in t:
                continue
            new = t
            if bf.name == "pom.xml":
                if d.get("versionProperty"):
                    new = re.sub(r"(<" + re.escape(d["versionProperty"]) + r">)[^<]*(</" + re.escape(d["versionProperty"]) + r">)",
                                 rf"\g<1>{v}\g<2>", new)
                blk = r"(<groupId>" + re.escape(g) + r"</groupId>\s*<artifactId>" + re.escape(a) + r"</artifactId>\s*<version>)(?!\$\{)[^<]+(</version>)"
                new = re.sub(blk, rf"\g<1>{v}\g<2>", new)
            else:
                new = re.sub(r"(['\"]" + re.escape(g) + ":" + re.escape(a) + r":)[\w.\-]+(['\"])", rf"\g<1>{v}\g<2>", new)
            if write_if_changed(bf, t, new):
                changes.append(f"{rel(bf, root)}: {d['coordinate']} -> {v} (built for "
                               f"{'Boot 2.7 + Hibernate' if line == HIBERNATE_LINE else 'Boot ' + line})")
    return changes


HELP_PLUGIN = "org.apache.maven.plugins:maven-help-plugin:3.5.1"


def _effective_pom(root: Path, exe: str, extra: list[str], env: dict, out: Path) -> tuple[int, str]:
    r = subprocess.run([exe, "-B", "-ntp", "-fae", f"{HELP_PLUGIN}:effective-pom", f"-Doutput={out}"] + extra,
                       cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def managed_versions(root: Path, exe: str, extra: list[str], env: dict) -> dict | None:
    """Versions the build resolves today, from the effective POM: {(g, a): v} and {(plugin_a, g, a): v} for plugin deps."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "effective.xml"
        code, _ = _effective_pom(root, exe, extra, env, out)
        text = read(out) if out.exists() else None
    if code != 0 or not text:
        return None
    deps, plugin_deps = {}, {}
    plugin_spans = []
    for pm in re.finditer(r"<plugin>(.*?)</plugin>", text, re.S):
        body = pm.group(1)
        pa = _first_tag(re.sub(r"<(dependencies|executions|configuration)>.*?</\1>", "", body, flags=re.S), "artifactId")
        plugin_spans.append(pm.span())
        for d in re.finditer(r"<dependency>(.*?)</dependency>", body, re.S):
            g, a, v = (_first_tag(d.group(1), t) for t in ("groupId", "artifactId", "version"))
            if g and a and v:
                plugin_deps.setdefault((pa, g, a), v)
    for d in re.finditer(r"<dependency>(.*?)</dependency>", text, re.S):
        if any(x <= d.start() < y for x, y in plugin_spans):
            continue
        g, a, v = (_first_tag(d.group(1), t) for t in ("groupId", "artifactId", "version"))
        if g and a and v:
            deps.setdefault((g, a), v)
    return {"deps": deps, "plugin_deps": plugin_deps}


_MISSING_RX = re.compile(r"'(?:build\.plugins\.plugin\[([\w.\-]+):([\w.\-]+)\]\.)?(?:dependencyManagement\.)?dependencies\.dependency\.version' "
                         r"for ([\w.\-]+):([\w.\-]+):\w+ is missing\.(?: @ line (\d+))?")


def fill_management_gaps(root: Path, before: dict, exe: str, extra: list[str], env: dict) -> tuple[list[str], list[str]]:
    """After a company parent/BOM upgrade: versions the old one managed but the new one does not are pinned (previous
    version) where they are used, so Maven can read the build again. Each pin is reported."""
    changes, todo = [], []
    for _ in range(3):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = _effective_pom(root, exe, extra, env, Path(tmp) / "e.xml")
        if code == 0:
            break
        fixed = 0
        project_file = None
        for line in out.splitlines():
            pm = re.search(r"The project \S+ \((.+?pom\.xml)\) has \d+ error", line)
            if pm:
                project_file = Path(pm.group(1))
                continue
            m = _MISSING_RX.search(line)
            if not m or not project_file or not project_file.exists():
                continue
            plugin_a, g, a, lineno = m.group(2), m.group(3), m.group(4), m.group(5)
            v = (before["plugin_deps"].get((plugin_a, g, a)) if plugin_a else None) or before["deps"].get((g, a))
            if not v and plugin_a:
                v = next((ver for (pa, gg, aa), ver in before["plugin_deps"].items() if (gg, aa) == (g, a)), None)
            if not v:
                todo.append(f"{rel(project_file, root)}: {g}:{a} lost its managed version with the new parent/BOM and no previous version is known - set one")
                continue
            t = read(project_file) or ""
            new = _pin_version(t, g, a, v, plugin_a, int(lineno) if lineno else None)
            if write_if_changed(project_file, t, new):
                fixed += 1
                where = f" (dependency of plugin {plugin_a})" if plugin_a else ""
                changes.append(f"{rel(project_file, root)}: pinned {g}:{a}:{v}{where} - the new parent/BOM no longer manages it")
                todo.append(f"`{g}:{a}` was managed by the old company parent/BOM but not by the new one; pinned the previous version {v} "
                            f"in {rel(project_file, root)}. Check that this version works with Boot 3 / Jakarta (or move to its successor).")
        if not fixed:
            break
    return changes, todo


def _pin_version(text: str, g: str, a: str, v: str, plugin_a: str | None, lineno: int | None) -> str:
    def add(block: str) -> str:
        m = re.search(r"([ \t]*)<artifactId>" + re.escape(a) + r"</artifactId>", block)
        ind = m.group(1) if m else ""
        sep = "\n" + ind if "\n" in block[:m.start() if m else 0] else ""
        return re.sub(r"(<artifactId>" + re.escape(a) + r"</artifactId>)", lambda x: x.group(1) + (sep or "") + f"<version>{v}</version>", block, count=1)
    candidates = []
    for m in re.finditer(r"<dependency>((?:(?!<dependency>).)*?)</dependency>", text, re.S):
        b = m.group(1)
        if _first_tag(b, "groupId") == g and _first_tag(b, "artifactId") == a and not _first_tag(b.split("<exclusions>")[0], "version"):
            in_plugin = any(pm.start() < m.start() < pm.end() for pm in re.finditer(r"<plugin>.*?</plugin>", text, re.S))
            if bool(plugin_a) == in_plugin:
                candidates.append(m)
    if not candidates:
        return text
    if lineno:
        offs = sum(len(l) + 1 for l in text.split("\n")[:lineno - 1])
        candidates.sort(key=lambda m: abs(m.start() - offs))
    m = candidates[0]
    return text[:m.start()] + add(m.group(0)) + text[m.end():]


def stage_boot_version(root: Path, builder, env: dict) -> str | None:
    """The Boot version the build now uses: from the POM, else through the company parent (Maven)."""
    v = detect_build(root)["boot_version"]
    if not (v and not v.startswith("${")):
        v = effective_boot_version(root, builder.exe, builder.extra, env) if builder.tool == "maven" else None
    if v and builder.tool == "maven":
        repo = MavenRepo(root, builder.exe, builder.extra, env, builder.log_dir)
        if not repo.path("org.springframework.boot", "spring-boot-dependencies", v, "pom").exists():
            repo.resolve([("org.springframework.boot", "spring-boot-dependencies", v, "pom")], "boot-bom")
    return v


def apply_plan_safely(root: Path, plan: dict, line: str, exe: str, extra: list[str], env: dict) -> tuple[list[str], list[str]]:
    """apply_dependency_plan, plus: versions the old parent/BOM managed but the new one does not are pinned."""
    moves = [d for d in plan.get("dependencies", []) if d.get("kind") in ("parent", "bom") and d.get("upgrades", {}).get(line)]
    before = managed_versions(root, exe, extra, env) if moves and (root / "pom.xml").exists() else None
    changes = apply_dependency_plan(root, plan, line)
    todo = []
    if before is not None and changes:
        c, t = fill_management_gaps(root, before, exe, extra, env)
        changes += c
        todo += t
    return changes, todo


def effective_boot_version(root: Path, exe: str, extra: list[str], env: dict) -> str | None:
    """Boot version when it comes through a company parent/BOM: the resolved org.springframework.boot:spring-boot."""
    # relative outputFile = one file per module (a single absolute file would be overwritten by every module);
    # -fae: in a multi-module build, modules depending on unbuilt siblings fail here, the others still answer
    name = "target/spring-migrate-boot.txt"
    subprocess.run([exe, "-B", "-ntp", "-q", "-fae", f"{DEP_PLUGIN}:list", "-DincludeGroupIds=org.springframework.boot",
                    "-DincludeArtifactIds=spring-boot", f"-DoutputFile={name}", "-DexcludeTransitive=false"] + extra,
                   cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    found = []
    for f in root.rglob("spring-migrate-boot.txt"):
        m = re.search(r"org\.springframework\.boot:spring-boot:jar:([\w.\-]+)", read(f) or "")
        if m:
            found.append(m.group(1))
        f.unlink()
    return max(found, key=vtuple) if found else None


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


# frontend-maven-plugin's own skip switches (npm/yarn/pnpm/bun, node install, task runners)
FRONTEND_SKIP = ["-Dskip.installnodenpm", "-Dskip.installnodecorepack", "-Dskip.installnodeyarn", "-Dskip.installnodepnpm", "-Dskip.installyarn", "-Dskip.installbun",
                 "-Dskip.npm", "-Dskip.npx", "-Dskip.yarn", "-Dskip.pnpm", "-Dskip.bun", "-Dskip.corepack", "-Dskip.bower",
                 "-Dskip.grunt", "-Dskip.gulp", "-Dskip.jspm", "-Dskip.karma", "-Dskip.webpack", "-Dskip.ember"]
# Quality gates that are bound early in many builds. The OpenRewrite step only needs compiled code, and the tool's own
# pre-step edits are formatted just before the stage commit, so a format/style check there would fail for nothing.
# The build gates after each stage keep all of them.
QUALITY_SKIP = ["-Dspotless.check.skip=true", "-Dcheckstyle.skip=true", "-Dpmd.skip=true", "-Dcpd.skip=true", "-Dspotbugs.skip=true",
                "-Dfmt.skip=true", "-Dformatter.skip=true", "-Dimpsort.skip=true", "-Dlicense.skip=true", "-Denforcer.skip=true",
                "-Dsortpom.skip=true", "-Djacoco.skip=true"]
# Lines that explain a failure better than Maven's generic "[ERROR] Failed to execute goal ..."
CAUSE_RX = re.compile(r"Checkstyle violation|\.java:\[?\d+[,:]\d+\]?:? .*\[\w+\]\s*$|\(\w+\) \w+: |spotless|npm (?:ERR!|error)|yarn error|ERR_PNPM|\[ERROR\] .*\.(?:java|kt):\[|cannot find symbol|Tests run:.*(?:Failures: [1-9]|Errors: [1-9])|"
                      r"<<< (?:FAILURE|ERROR)!|Caused by:|Could not resolve dependencies|Non-resolvable|What went wrong", re.I)


def show_errors(log: Path, limit: int = 10):
    """Print the lines that explain a failure (npm errors, compiler errors, failed tests) before Maven's generic ones."""
    try:
        all_lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return
    causes = [l for l in all_lines if CAUSE_RX.search(l)]
    generic = [l for l in all_lines if ("ERROR" in l or "FAILURE" in l) and l not in causes and "Re-run Maven" not in l
               and "full stack trace" not in l and "please read the following" not in l and "cwiki.apache.org" not in l and l.strip() != "[ERROR]"]
    for l in (causes[:limit - 2] + generic[:max(2, limit - len(causes))])[:limit]:
        say(f"    {l.strip()[:220]}")
    if any(re.search(r"npm (?:ERR!|error)|frontend-maven-plugin", l) for l in all_lines):
        say("    hint: a frontend build (frontend-maven-plugin) failed. OpenRewrite does not need it - if npm cannot work in this "
            "environment, re-run with --skip-frontend")
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
    hits = [l.strip() for l in last.splitlines() if DOWNLOAD_FAIL_RX.search(l)]
    real = []
    for l in hits:
        m = re.search(r"Failed to download ([\w.\-]+):([\w.\-]+):([\w.\-]+)", l)
        if m:
            known = central_versions(m.group(1), m.group(2))
            # a version that does not exist (often produced by a recipe, e.g. a property bump on a renamed artifact) is
            # not a network problem; only versions that exist but could not be fetched mean skipped edits
            if known and m.group(3) not in known:
                continue
        real.append(l[:200])
    return real[:5]


def git(root: Path, *args: str, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise MigrationError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def migration_base(root: Path) -> str | None:
    """Commit the migration started from: parent of the first chore(migration) commit, else HEAD."""
    first = git(root, "log", "--reverse", "--format=%H", "--grep=^chore(migration)", check=False).split()
    if first:
        r = git(root, "rev-parse", "-q", "--verify", first[0] + "^", check=False).strip()
        if r:
            return r
    return git(root, "rev-parse", "HEAD", check=False).strip() or None


def java_major(env: dict) -> int | None:
    try:
        r = subprocess.run(["java", "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
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
    def __init__(self, root: Path, tool: str, env: dict, log_dir: Path, extra: list[str], plugin_version: str | None,
                 rewrite_extra: list[str] | None = None, skip_frontend: bool = False, rewrite_frontend: bool = False, no_fork: bool = False,
                 format_goal: str | None = None):
        self.root, self.tool, self.env, self.log_dir, self.extra = root, tool, env, log_dir, list(extra)
        self.plugin_version = plugin_version
        if skip_frontend and tool == "maven":
            self.extra += FRONTEND_SKIP
        # OpenRewrite's `run` goal first builds up to process-test-classes, which also runs frontend builds (npm install ...).
        # The recipes only need Java sources and dependencies, so skip those there unless asked not to.
        self.rewrite_extra = (([] if rewrite_frontend or skip_frontend or tool != "maven" else FRONTEND_SKIP)
                              + (QUALITY_SKIP if tool == "maven" else []) + list(rewrite_extra or []))
        self.no_fork = no_fork
        self.format_goal = format_goal if format_goal is not None else ("spotless:apply" if tool == "maven" else "spotlessApply")
        if tool == "maven":
            self.exe = str(root / "mvnw") if (root / "mvnw").exists() else "mvn"
        else:
            self.exe = str(root / "gradlew") if (root / "gradlew").exists() else "gradle"

    def rewrite(self, stage: str, recipes: list[str], artifacts: list[str], dry: bool) -> int:
        log = self.log_dir / f"{stage}-rewrite.log"
        if self.tool == "maven":
            plugin = "org.openrewrite.maven:rewrite-maven-plugin" + (f":{self.plugin_version}" if self.plugin_version else "")
            # runNoFork/dryRunNoFork: no build before rewriting (generated sources are then missing for type attribution)
            goal = f"{plugin}:{'dryRun' if dry else 'run'}{'NoFork' if self.no_fork else ''}"
            cmd = [self.exe, "-B", "-ntp", "-U", goal,
                   f"-Drewrite.recipeArtifactCoordinates={','.join(artifacts)}",
                   f"-Drewrite.activeRecipes={','.join(recipes)}",
                   "-Drewrite.exportDatatables=false", "-Drewrite.failOnInvalidActiveRecipes=true"] + self.extra + self.rewrite_extra
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
        cmd = [self.exe, "--no-daemon", "--init-script", str(init), "rewriteDryRun" if dry else "rewriteRun"] + self.extra + self.rewrite_extra
        return run(cmd, self.root, log, self.env)

    def classpath(self) -> list[str]:
        """Resolved runtime+test classpath (Maven only); empty if it cannot be resolved."""
        if self.tool != "maven":
            return []
        # compile: lets the reactor resolve sibling modules; one output file per module (relative path)
        run([self.exe, "-B", "-ntp", "-q", "-fae", "compile", f"{DEP_PLUGIN}:build-classpath", "-Dmdep.outputFile=target/spring-migrate-cp.txt",
             "-Dmdep.includeScope=runtime"] + self.extra, self.root, self.log_dir / "classpath.log", self.env)
        cp = []
        for f in self.root.rglob("spring-migrate-cp.txt"):
            cp += [x for x in (read(f) or "").strip().split(os.pathsep) if x and x not in cp]
            f.unlink()
        return cp

    def format(self, stage: str, files: list[str]) -> list[str] | None:
        """Run the project's formatter (default: Spotless, as configured in the build) on the files a stage changed.
        Returns a note, or None when the project has no such formatter (then it is not tried again)."""
        if self.format_goal is None or not files:
            return []
        log = self.log_dir / f"{stage}-format.log"
        if self.tool == "maven":
            # Spotless matches spotlessFiles against absolute paths; limit it to the changed files
            patterns = ",".join(".*" + re.escape("/" + f) for f in files)
            cmd = [self.exe, "-B", "-ntp", self.format_goal] + (["-DspotlessFiles=" + patterns] if "spotless" in self.format_goal else [])
        else:
            cmd = [self.exe, "--no-daemon", self.format_goal]
        code = run(cmd + self.extra, self.root, log, self.env)
        text = read(log) or ""
        if code != 0 and re.search(r"No plugin found for prefix|Task '\w+' not found|could not find task|NoPluginFoundForPrefix", text, re.I):
            self.format_goal = None
            return None
        if code != 0:
            return [f"formatter `{self.format_goal}` failed (exit {code}) - see {log}"]
        return [f"formatted {len(files)} changed file(s) with `{self.format_goal}`"]

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
    boot_line: str | None = None      # Boot line this stage ends on (drives post-fixers / cloud train)
    legacy: bool = False              # run the legacy-library codemods before the recipes
    bump_to: str | None = None        # set the Boot version directly (no OpenRewrite upgrade recipe exists)


RECIPE_PROPS_4_1 = "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1"


def plan_stages(target: str, java: int, junit4: bool, spring_v: str, extra_recipes: list[str], openjpa: bool = False) -> list[Stage]:
    sp, mj, tf = f"{R_SPRING}:{spring_v}", f"{R_MIGRATE}:RELEASE", f"{R_TESTING}:RELEASE"
    arts = [sp] + ([tf] if junit4 else [])
    stages = [Stage("1-boot-2.7-latest", "Spring Boot 2.7.13 -> latest 2.7.x (safe checkpoint)", [RECIPE_2_7], [sp])]
    if openjpa:
        stages.append(Stage(OPENJPA_STAGE, "OpenJPA -> Hibernate (on Boot 2.7 / Hibernate 5.6, before the Spring 6 jump)", [], []))
    r3 = [RECIPE_3_5, RECIPE_JAVA[java]] + ([RECIPE_JUNIT5] if junit4 else [])
    stages.append(Stage("2-boot-3.5", f"Spring Boot 3.5 / Spring 6 / Jakarta EE 10 / Java {java}", r3, arts + [mj],
                        java_fix=True, boot_line="3.5", legacy=True))
    if target in ("4.0", "4.1"):
        stages.append(Stage("3-boot-4.0", "Spring Boot 4.0 / Spring Framework 7", [RECIPE_4_0], arts, java_fix=True, boot_line="4.0"))
    if target == "4.1":
        stages.append(Stage("4-boot-4.1", "Spring Boot 4.1 (longest-supported line)", [RECIPE_PROPS_4_1], [sp], java_fix=True,
                            boot_line="4.1", bump_to="4.1"))
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
    if not info["boot_version"] and info["tool"] == "maven":
        exe = str(root / "mvnw") if (root / "mvnw").exists() else "mvn"
        info["boot_version"] = effective_boot_version(root, exe, shlex.split(a.build_args or ""), env)
        if info["boot_version"]:
            say(f"[preflight] Spring Boot {info['boot_version']} comes through a company parent/BOM")
    say(f"[preflight] {info['tool']} project, Spring Boot {info['boot_version'] or 'not detected'}, Java {info['java_version'] or 'from parent'}")
    bv = info["boot_version"] or "2.7"
    try:
        major, minor = int(bv.split(".")[0]), int(bv.split(".")[1])
    except (ValueError, IndexError):
        major, minor = 2, 7
    target_t = tuple(int(x) for x in a.target.split("."))
    if (major, minor) >= target_t:
        say(f"[preflight] project is already on Spring Boot {bv}; target {a.target} reached.")
        return 0
    resuming = major >= 3
    (report_dir / "scan-before.md").write_text(render_scan(pre, "Pre-migration scan"), encoding="utf-8")
    blockers = [f for f in pre["findings"].values() if f.hits and f.rule.severity == "blocker" and f.rule.kind == "manual"]
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
    builder = Builder(root, info["tool"], env, log_dir, shlex.split(a.build_args or ""), a.rewrite_plugin_version,
                      rewrite_extra=shlex.split(a.rewrite_args or ""), skip_frontend=a.skip_frontend,
                      rewrite_frontend=a.rewrite_frontend, no_fork=a.rewrite_no_fork,
                      format_goal=a.format_goal)
    if a.no_format:
        builder.format_goal = None

    if resuming:
        done = lambda st: st.key[0] == "1" or (st.key.startswith("2-") and (major, minor) >= (3, 5)) or \
            (st.key.startswith("3-") and major >= 4)
        stages = [st for st in stages if not done(st)]
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
    todo_pre: list[str] = []
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

    # ---- company / internal dependencies --------------------------------
    dep_plan = None
    if a.dependency_plan:
        dep_plan = json.loads(Path(a.dependency_plan).read_text(encoding="utf-8"))
        say(f"[deps] using {a.dependency_plan}")
    elif info["tool"] == "maven" and not a.no_dependency_detection:
        dep_repo = MavenRepo(root, builder.exe, builder.extra, env, log_dir)
        dep_repo.refresh = a.refresh_dependency_versions
        dep_plan = detect_dependency_plan(root, dep_repo, a.target, a.internal_group or [], openjpa_stage=use_openjpa)
        if a.interactive:
            dep_plan = interactive_plan(dep_plan)
    if dep_plan:
        (report_dir / DEP_PLAN_FILE).write_text(json.dumps(dep_plan, indent=2) + "\n", encoding="utf-8")
        for d in dep_plan["dependencies"]:
            if d.get("upgrades") or not d.get("status", "").startswith(("no Spring", "current")):
                say(f"[deps] {d['coordinate']} {d['current']}: {d.get('status', '')} "
                    + " ".join(f"{l}->{v}" for l, v in d.get("upgrades", {}).items()))
                if d.get("status", "").startswith("NO compatible"):
                    todo_pre.append(f"`{d['coordinate']}` {d['current']}: {d['status']}")
        say(f"[deps] plan: {report_dir / DEP_PLAN_FILE} (edit and pass with --dependency-plan to change choices)")
        if dep_plan.get("openjpaViaDependencies") and not a.no_openjpa and not any(st.key == OPENJPA_STAGE for st in stages) \
                and any(st.key.startswith("2-") for st in stages):
            say("[deps] OpenJPA comes in through a dependency -> adding the OpenJPA -> Hibernate stage")
            at = next(i for i, st in enumerate(stages) if st.key.startswith("2-"))
            stages.insert(at, Stage(OPENJPA_STAGE, "OpenJPA -> Hibernate (on Boot 2.7 / Hibernate 5.6, before the Spring 6 jump)", [], []))

    # ---- stages ----------------------------------------------------------
    todo_all: list[str] = list(todo_pre)
    failed_stage = None
    rewrite_failed = False
    for st in stages:
        say(f"\n[stage {st.key}] {st.title}")
        t0 = time.time()
        changes = []
        if st.key == OPENJPA_STAGE:
            stage_rev = git(root, "rev-parse", "HEAD").strip() if not a.no_git else None
            changes, todo = migrate_openjpa(root)
            if dep_plan:
                c_, t_ = apply_plan_safely(root, dep_plan, HIBERNATE_LINE, builder.exe, builder.extra, env)
                changes += c_
                todo_all += t_
            todo_all += todo
            for c in changes:
                say(f"  openjpa: {c}")
            for t in todo:
                say(f"  TODO {t}")
            code, dl = 0, []
        else:
            base_rev = migration_base(root) if not a.no_git else None
            stage_rev = git(root, "rev-parse", "HEAD").strip() if not a.no_git else None
            pre_changes = []
            if st.legacy and not a.no_legacy:
                pre_changes, todo = run_legacy_codemods(root)
                todo_all += todo
                for c in pre_changes:
                    say(f"  legacy: {c}")
            if st.bump_to:
                bv_new = boot_version_for(st.bump_to)
                for c in bump_boot(root, bv_new):
                    pre_changes.append(c)
                    say(f"  bump: {c}")
            code, dl = 1, []
            for attempt in range(1, a.retries + 2):
                code = builder.rewrite(st.key, st.recipes, st.artifacts, dry=False) if st.recipes else 0
                dl = rewrite_had_download_failures(log_dir / f"{st.key}-rewrite.log") if st.recipes else []
                if code == 0 and not dl:
                    break
                # only repository/download problems are worth retrying; a build or recipe failure would fail again
                last = attempt > a.retries or not dl
                if not a.no_git and not a.allow_dirty:
                    # the tree was clean when we started -> discard the partial edits; redo our pre-steps only for a retry,
                    # so a final failure leaves a clean tree that a re-run can start from
                    git(root, "reset", "-q", "--hard", "HEAD")
                    git(root, "clean", "-fdq")
                    if not last:
                        if st.legacy and not a.no_legacy:
                            run_legacy_codemods(root)
                        if st.bump_to:
                            bump_boot(root, boot_version_for(st.bump_to))
                if last:
                    break
                say(f"[stage {st.key}] attempt {attempt} failed (repository/download problem); retrying in 30s")
                time.sleep(30)
            if code != 0 or dl:
                results.append((st.key, st.title, False, time.time() - t0, "OpenRewrite failed - see rewrite log"))
                failed_stage = st.key
                rewrite_failed = True
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
        if st.boot_line and dep_plan and not (code != 0 or dl) and not (st.legacy and not a.no_legacy):
            c_, t_ = apply_plan_safely(root, dep_plan, st.boot_line, builder.exe, builder.extra, env)
            todo_all += t_
            for c in c_:
                pre_changes.append(c)
                say(f"  deps: {c}")
        if st.legacy and not a.no_legacy and not (code != 0 or dl):
            post_c, post_t = run_legacy_codemods(root, "post")
            if dep_plan and st.boot_line:
                c_, t_ = apply_plan_safely(root, dep_plan, st.boot_line, builder.exe, builder.extra, env)
                post_c += c_
                post_t += t_
            todo_all += post_t
            pre_changes += post_c
            for c in post_c:
                say(f"  legacy: {c}")
        if not st.java_fix and st.key != OPENJPA_STAGE and not (code != 0 or dl):
            for c in fix_springdoc(root, st.boot_line or "2.7"):
                pre_changes.append(c)
                say(f"  fixer: {c}")
        if st.java_fix:
            changes, todo = run_fixers(root, a.java, st.boot_line or "3.5", base_rev,
                                       trailing_slash=st.key.startswith("2-") and not a.no_trailing_slash_compat,
                                       stage_rev=stage_rev, boot_version=stage_boot_version(root, builder, env),
                                       extra=builder.extra)
            todo_all += todo
            if builder.tool == "maven":
                changes += fix_java_level(root, a.java, builder.exe, builder.extra, env)
            for c in changes:
                say(f"  fixer: {c}")
            changes = pre_changes + changes
        if not st.java_fix:
            changes = changes + pre_changes if st.key != OPENJPA_STAGE else changes
        note = f"{len(changes)} tool change(s)"
        if not a.no_git and builder.format_goal and not (code != 0 or dl):
            base = stage_rev if st.key != OPENJPA_STAGE else git(root, "rev-parse", "HEAD").strip()
            changed = [f for f in git(root, "diff", "--name-only", base).split() + git(root, "ls-files", "--others", "--exclude-standard").split()
                       if f.endswith((".java", ".kt", ".groovy")) and (root / f).exists()]
            notes = builder.format(st.key, changed)
            if notes is None:
                say(f"[stage {st.key}] no Spotless configured in this build - formatting skipped (use --format-goal for another formatter)")
            for n_ in notes or []:
                say(f"  format: {n_}")
        if not a.no_git and git(root, "status", "--porcelain").strip():
            git(root, "add", "-A")
            msg = f"chore(migration): {st.title}\n\nAutomated by spring-migrate {__version__} (OpenRewrite: {', '.join(st.recipes)})"
            r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise MigrationError(f"git commit failed (is user.name/user.email set?): {r.stderr.strip()}")
            say(f"[git] committed stage {st.key}")
        ok = True
        if not a.skip_tests:
            say(f"[stage {st.key}] build + tests" + (" (with spring-boot-properties-migrator)" if st.boot_line and not a.no_properties_migrator else ""))
            saved = add_properties_migrator(root) if st.boot_line and not a.no_properties_migrator else {}
            try:
                ok = builder.verify(st.key) == 0
                if not ok:
                    fixed = autofix_compile_errors(root, log_dir / f"{st.key}-build.log")
                    if fixed:
                        for c in fixed:
                            say(f"  autofix: {c}")
                        if not a.no_git:
                            restore_files(saved)
                            git(root, "add", "-A")
                            git(root, "commit", "-q", "-m", f"chore(migration): fix compile errors with one safe fix ({st.key})")
                            saved = add_properties_migrator(root) if saved else {}
                        say(f"[stage {st.key}] build + tests again after the compile fixes")
                        ok = builder.verify(st.key + "-autofix") == 0
                        if not ok:
                            (log_dir / f"{st.key}-build.log").write_bytes((log_dir / f"{st.key}-autofix-build.log").read_bytes())
            finally:
                restore_files(saved)
            if saved:
                renames, gone = parse_properties_migrator(collect_test_output(root, log_dir / f"{st.key}-build.log"))
                fixed = apply_property_renames(root, renames)
                for k, why in gone.items():
                    todo_all.append(f"property `{k}` is no longer supported: {why}")
                if fixed:
                    for c in fixed:
                        say(f"  props-migrator: {c}")
                    if not a.no_git:
                        git(root, "add", "-A")
                        git(root, "commit", "-q", "-m", f"chore(migration): property renames reported by spring-boot-properties-migrator ({st.key})")
                    say(f"[stage {st.key}] re-running build + tests without the migrator")
                    ok = builder.verify(st.key + "-props") == 0
                elif renames == {} and not ok:
                    pass
            say(f"[stage {st.key}] {'build OK' if ok else 'BUILD/TESTS FAILED'}")
            if not ok:
                show_errors(log_dir / f"{st.key}-build.log")
        results.append((st.key, st.title, ok, time.time() - t0, note))
        if not ok:
            failed_stage = st.key
            break

    # ---- jars still compiled against javax.* -------------------------------
    if not failed_stage and not a.skip_tests:
        direct = {a for bf in build_files(root) for _, _, _, a in pom_dependency_blocks(read(bf) or "") if a}
        bad = javax_jars(builder.classpath(), direct)
        for j in bad:
            todo_all.append(f"dependency `{Path(j).name}` still references javax.* EE types and will fail at runtime - "
                            f"upgrade it to a Jakarta version (or run it through the Eclipse Transformer)")
        if bad:
            say(f"[check] {len(bad)} dependency jar(s) still reference javax.* - see the report")
    # ---- report ----------------------------------------------------------
    post = scan(root)
    (report_dir / "scan-after.md").write_text(render_scan(post, "Post-migration scan"), encoding="utf-8")
    write_report(report_dir, root, a, pre, post, results, todo_all, failed_stage, branch if not a.no_git else None, dep_plan)
    say(f"\n[done] report: {report_dir/'MIGRATION_REPORT.md'}")
    if failed_stage and rewrite_failed:
        say(f"[done] stopped at stage {failed_stage}: OpenRewrite failed, nothing of this stage was committed and the working tree "
            f"is clean. Fix the cause (see above), then re-run `spring-migrate migrate` - it continues with this stage.")
    elif failed_stage:
        say(f"[done] stopped at stage {failed_stage}. Automated changes are committed; fix the build on top of them, "
            f"then re-run `spring-migrate migrate` for the remaining stages (it detects the current version).")
        return 1
    return 0


def write_report(report_dir, root, a, pre, post, results, todo, failed_stage, branch, dep_plan=None):
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
    if dep_plan:
        L += [""] + render_dependency_plan(dep_plan).splitlines()
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


def cmd_deps(a) -> int:
    root = Path(a.path).resolve()
    if not (root / "pom.xml").exists():
        raise MigrationError("dependency detection resolves through Maven: no pom.xml found "
                             "(for Gradle, write the plan by hand - see README - and pass it with --dependency-plan)")
    env = build_env(getattr(a, "java_home", None))
    exe = str(root / "mvnw") if (root / "mvnw").exists() else "mvn"
    log_dir = Path(tempfile.mkdtemp(prefix="spring-migrate-deps-"))
    repo = MavenRepo(root, exe, shlex.split(a.build_args or ""), env, log_dir)
    repo.refresh = a.refresh_dependency_versions
    plan = detect_dependency_plan(root, repo, a.target, a.internal_group or [])
    if a.interactive:
        plan = interactive_plan(plan)
    out = Path(a.out or root.parent / f"{root.name}-{DEP_PLAN_FILE}")
    write_file(out, json.dumps(plan, indent=2) + "\n")
    print(render_dependency_plan(plan))
    say(f"written {out} (logs: {log_dir}). Use it with: migrate {a.path} --dependency-plan {out}")
    return 0


def cmd_legacy(a) -> int:
    root = Path(a.path).resolve()
    changes, todo = run_legacy_codemods(root, "all")
    for c in changes:
        say(c)
    for t in todo:
        say(f"TODO {t}")
    say(f"{len(changes)} change(s). Review with `git diff`; the result targets Boot 3.5 APIs (run `migrate` for the rest).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The CLI definition (also read by spring_migrate_web.py to build its option forms)."""
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

    dp = sub.add_parser("deps", help="detect company/internal dependencies (parent, BOMs, libraries) and plan their upgrades")
    dp.add_argument("path")
    dp.add_argument("--target", choices=("3.5", "4.0", "4.1"), default="4.1")
    dp.add_argument("--internal-group", action="append", help="groupId prefix of your company artifacts (repeatable; also guessed)")
    dp.add_argument("--build-args", help='extra args for mvn, e.g. "-s /path/settings.xml" for your Artifactory')
    dp.add_argument("--java-home")
    dp.add_argument("--interactive", action="store_true", help="choose the version per Boot line for each dependency")
    dp.add_argument("--refresh-dependency-versions", action="store_true",
                    help="ask the repositories again (default: reuse a check from the last 24 h and Maven's cached version lists)")
    dp.add_argument("--out", help=f"plan file (default: next to the project, <name>-{DEP_PLAN_FILE})")
    dp.set_defaults(fn=cmd_deps)

    lg = sub.add_parser("legacy", help="run only the legacy-library codemods (Netflix, legacy OAuth2, id pinning, JPQL ...)")
    lg.add_argument("path")
    lg.add_argument("--offline", action="store_true")
    lg.set_defaults(fn=cmd_legacy)

    m = sub.add_parser("migrate", help="staged migration on a new git branch")
    m.add_argument("path")
    m.add_argument("--target", choices=("3.5", "4.0", "4.1"), default="4.1",
                   help="final Boot line (default 4.1, the longest-supported line; 3.5 is end-of-life for OSS)")
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
    m.add_argument("--format-goal", help="formatter run on the changed files before each stage commit "
                                        "(default: spotless:apply / spotlessApply, if the build configures Spotless)")
    m.add_argument("--no-format", action="store_true", help="do not run the formatter after each stage")
    m.add_argument("--skip-frontend", action="store_true",
                   help="skip frontend-maven-plugin (npm/yarn/pnpm, node install) in every build, e.g. when npm cannot reach its registry here")
    m.add_argument("--rewrite-frontend", action="store_true",
                   help="also run frontend builds during the OpenRewrite step (skipped there by default; OpenRewrite does not need them)")
    m.add_argument("--rewrite-args", help='extra Maven/Gradle args for the OpenRewrite step only, e.g. "-Dexec.skip -Ddocker.skip"')
    m.add_argument("--rewrite-no-fork", action="store_true",
                   help="do not build before rewriting (rewrite:runNoFork) - for builds whose plugins fail outside CI; "
                        "code that uses generated sources then gets less precise recipes")
    m.add_argument("--extra-recipe", action="append", help="additional OpenRewrite recipe name to run in the last stage (repeatable)")
    m.add_argument("--no-legacy", action="store_true", help="skip the legacy-library codemods (Netflix, legacy OAuth2, id pinning, ...)")
    m.add_argument("--no-trailing-slash-compat", action="store_true", help="do not add the trailing-slash compatibility filter")
    m.add_argument("--no-properties-migrator", action="store_true", help="do not run tests with spring-boot-properties-migrator")
    m.add_argument("--dependency-plan", help=f"use this {DEP_PLAN_FILE} (from `deps` or a previous run) instead of detecting")
    m.add_argument("--internal-group", action="append", help="groupId prefix of your company artifacts (repeatable; also guessed)")
    m.add_argument("--interactive", action="store_true", help="choose company dependency versions per Boot line interactively")
    m.add_argument("--no-dependency-detection", action="store_true", help="do not look for company parent/BOM/library upgrades")
    m.add_argument("--refresh-dependency-versions", action="store_true",
                   help="ask the repositories again (default: reuse a check from the last 24 h and Maven's cached version lists)")
    m.add_argument("--offline", action="store_true", help="do not query Maven Central / SDKMAN for latest versions (use built-in ones)")
    m.set_defaults(fn=cmd_migrate)

    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    global OFFLINE
    OFFLINE = getattr(a, "offline", False)
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
