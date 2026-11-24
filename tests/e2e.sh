#!/usr/bin/env bash
# End-to-end tests: migrates the sample projects (Boot 2.7.13) to Boot 4.1 and checks the result.
# Each sample has tests that are green on the original code and must stay green after every stage.
# Needs: JDK 17+, Maven, git, network access to Maven Central (or BUILD_ARGS="-s settings.xml").
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${TARGET:-4.1}"
run() {  # run <example-dir-name>; leaves you in the migrated copy
  local work; work="$(mktemp -d)"; cp -r "$here/examples/$1" "$work/p"; find "$work/p" -name target -type d -prune -exec rm -rf {} +; cd "$work/p"
  git init -q -b main && git add -A && git -c user.name=t -c user.email=t@t commit -qm baseline
  git config user.name t; git config user.email t@t
  python3 "$here/spring_migrate.py" migrate . --target "$TARGET" --report-dir "$work/report" ${BUILD_ARGS:+--build-args "$BUILD_ARGS"}
  [ -n "${NO_BOOT_IN_POM:-}" ] || grep -q "<version>${TARGET}\." pom.xml
  grep -q "all stages completed" "$work/report/MIGRATION_REPORT.md"
  ! grep -rqE "javax\.(servlet|persistence|validation)" --include=*.java .
  ! git log --format=%s | grep -qv "^chore(migration)\|^baseline$"   # every commit came from the tool
  echo "$1 OK (report: $work/report/MIGRATION_REPORT.md)"
}
demo() {
  run legacy-demo
  ! grep -rq "HttpTrace" src
  grep -q "management.httpexchanges.recording.enabled" src/main/resources/application.properties
}
openjpa() {
  run legacy-openjpa
  ! grep -rq "org.apache.openjpa" src pom.xml
  ! grep -q "javaagent" Dockerfile
  grep -q "OPENJPA_SEQUENCE_TABLE" src/main/java/com/acme/legacy/Account.java
}
cloud() {
  run legacy-cloud
  ! grep -qiE "hystrix|ribbon|springfox|sleuth|security\.oauth|<mockito.version>" pom.xml
  ! grep -rqiE "hystrix\.|ribbon\.|security\.oauth2\.resource|=\"" src/main/resources
  test -f src/main/java/com/acme/cloud/TrailingSlashCompatibilityConfig.java
  grep -q "spring-boot-starter-aspectj" pom.xml
  grep -q "eclipse-temurin" Dockerfile
}
authserver() {
  run legacy-authserver
  ! grep -q "spring-security-oauth2-autoconfigure" pom.xml
  grep -q "RegisteredClientRepository" src/main/java/com/acme/auth/AuthServerConfig.java
}
company() {
  # a company parent (on spring-boot-starter-parent 2.7) and a javax/spring.factories starter from a fake Artifactory
  local repo; repo="$(mktemp -d)"
  "$here/tests/build-company-repo.sh" "$repo" >/dev/null
  NO_BOOT_IN_POM=1 BUILD_ARGS="-s $repo/settings.xml${BUILD_ARGS:+ $BUILD_ARGS}" run legacy-company
  grep -A2 "<artifactId>acme-parent</artifactId>" pom.xml | grep -q "<version>3.0.0</version>"
  grep -q "<acme-audit.version>3.0.0</acme-audit.version>" pom.xml
  # acme-persistence 1.0.0 needs OpenJPA: the OpenJPA stage must run and pick the Hibernate build, then the Boot 4 build
  git log --format=%s | grep -q "OpenJPA -> Hibernate"
  git show "$(git log -1 --format=%H --grep='OpenJPA -> Hibernate'):pom.xml" | grep -A1 "<artifactId>acme-persistence</artifactId>" | grep -q "<version>1.1.0</version>"
  grep -A1 "<artifactId>acme-persistence</artifactId>" pom.xml | grep -q "<version>3.0.0</version>"
}
multimodule() {
  # root pom + library + web app + worker app; Hystrix version managed in the root's <dependencyManagement>
  run legacy-multimodule
  ! grep -qi "hystrix" pom.xml shop-*/pom.xml
  grep -q "spring-cloud-starter-circuitbreaker-resilience4j" shop-worker/pom.xml
  grep -q "spring-boot-starter-aspectj" shop-worker/pom.xml
  ! grep -q "spring-boot-starter-aspectj" pom.xml shop-core/pom.xml shop-web/pom.xml
  test -f shop-web/src/main/java/com/acme/shop/web/TrailingSlashCompatibilityConfig.java
  ! test -f shop-worker/src/main/java/com/acme/shop/worker/TrailingSlashCompatibilityConfig.java
  grep -q 'sequenceName = "hibernate_sequence"' shop-core/src/main/java/com/acme/shop/core/Product.java
}
case "${1:-all}" in
  demo|openjpa|cloud|authserver|company|multimodule) "$1";;
  all) demo; openjpa; cloud; authserver; company; multimodule;;
  *) echo "usage: $0 [demo|openjpa|cloud|authserver|company|multimodule|all]"; exit 2;;
esac
