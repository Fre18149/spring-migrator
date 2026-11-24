#!/usr/bin/env bash
# End-to-end tests: migrates the sample projects (Boot 2.7.13) to Boot 4.1 and checks the result.
# Each sample has tests that are green on the original code and must stay green after every stage.
# Needs: JDK 17+, Maven, git, network access to Maven Central (or BUILD_ARGS="-s settings.xml").
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${TARGET:-4.1}"
run() {  # run <example-dir-name>; leaves you in the migrated copy
  local work; work="$(mktemp -d)"; cp -r "$here/examples/$1" "$work/p"; rm -rf "$work/p/target"; cd "$work/p"
  git init -q -b main && git add -A && git -c user.name=t -c user.email=t@t commit -qm baseline
  git config user.name t; git config user.email t@t
  python3 "$here/spring_migrate.py" migrate . --target "$TARGET" --report-dir "$work/report" ${BUILD_ARGS:+--build-args "$BUILD_ARGS"}
  grep -q "<version>${TARGET}\." pom.xml
  grep -q "all stages completed" "$work/report/MIGRATION_REPORT.md"
  ! grep -rqE "javax\.(servlet|persistence|validation)" src
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
case "${1:-all}" in
  demo|openjpa|cloud|authserver) "$1";;
  all) demo; openjpa; cloud; authserver;;
  *) echo "usage: $0 [demo|openjpa|cloud|authserver|all]"; exit 2;;
esac
