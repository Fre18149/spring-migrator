#!/usr/bin/env bash
# Builds a fake company Artifactory (a file-based Maven repository) from tests/company-repo:
#   com.acme.platform:acme-parent        1.4.0 (Boot 2.7) / 2.1.0 (Boot 3.5) / 3.0.0 (Boot 4.1)
#   com.acme.platform:acme-audit-starter 1.2.0 (javax, spring.factories) / 2.0.0 + 3.0.0 (jakarta, AutoConfiguration.imports)
#   com.acme.platform:acme-persistence   1.0.0 (OpenJPA) / 1.1.0 (Hibernate 5, javax) / 2.0.0 + 3.0.0 (Hibernate 6/7, jakarta)
# Usage: tests/build-company-repo.sh OUTDIR   -> OUTDIR/repo and OUTDIR/settings.xml (use with --build-args "-s OUTDIR/settings.xml")
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mkdir -p "$1" && cd "$1" && pwd)"
mkdir -p "$out/repo"
cat > "$out/settings.xml" <<XML
<settings>
  <profiles>
    <profile>
      <id>company</id>
      <repositories>
        <repository><id>company</id><url>file://$out/repo</url><snapshots><enabled>false</enabled></snapshots></repository>
      </repositories>
    </profile>
  </profiles>
  <activeProfiles><activeProfile>company</activeProfile></activeProfiles>
</settings>
XML
for p in acme-parent/1.4.0 acme-parent/2.1.0 acme-parent/3.0.0 acme-audit-starter/1.2.0 acme-audit-starter/2.0.0 acme-audit-starter/3.0.0 \
         acme-persistence/1.0.0 acme-persistence/1.1.0 acme-persistence/2.0.0 acme-persistence/3.0.0; do
  work="$(mktemp -d)"; cp -r "$here/company-repo/$p/." "$work/"
  mvn -B -ntp -q -s "$out/settings.xml" -f "$work/pom.xml" -DskipTests deploy -DaltDeploymentRepository="company::default::file://$out/repo"
  # a local build would make the artifact resolvable without the repo; force resolution through the "Artifactory"
  rm -rf "$HOME/.m2/repository/com/acme/platform"
  echo "deployed $p"
done
