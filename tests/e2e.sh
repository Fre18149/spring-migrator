#!/usr/bin/env bash
# End-to-end smoke tests: migrates the sample projects to Boot 4.0 and checks the result.
# Needs: JDK 17+, Maven, git, network access to Maven Central (or BUILD_ARGS="-s settings.xml").
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
run() {  # run <example-dir-name>
  local work; work="$(mktemp -d)"; cp -r "$here/examples/$1" "$work/p"; cd "$work/p"
  git init -q -b main && git add -A && git -c user.name=t -c user.email=t@t commit -qm baseline
  git config user.name t; git config user.email t@t
  python3 "$here/spring_migrate.py" migrate . --target 4.0 --report-dir "$work/report" ${BUILD_ARGS:+--build-args "$BUILD_ARGS"}
  grep -q "<version>4\." pom.xml
  ! grep -rqE "javax\.(servlet|persistence|validation)" src
  echo "$1 OK (report: $work/report/MIGRATION_REPORT.md)"
}
case "${1:-all}" in
  demo) run legacy-demo;;
  openjpa) run legacy-openjpa;;
  all) run legacy-demo; run legacy-openjpa;;
esac
