#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONNECTORS="$(cd "$HERE/.." && pwd)"
CONNECTOR_JAR="${1:-$CONNECTORS/build/connector-owncloud-0.2.0-tac.1.jar}"
if [[ ! -f "$CONNECTOR_JAR" ]]; then
  printf 'Build the ownCloud connector bundle first; expected %s\n' "$CONNECTOR_JAR" >&2
  exit 2
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/owncloud-create-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
unzip -q "$CONNECTOR_JAR" -d "$WORK/bundle"

FRAMEWORK="$WORK/connector-framework-1.7.0.0-M0.jar"
INTERNAL="$WORK/connector-framework-internal-1.7.0.0-M0.jar"
CONTRACT="$WORK/connector-framework-contract-1.7.0.0-M0.jar"
fetch_pinned() {
  local artifact="$1"
  local digest="$2"
  curl -fsSL \
    "https://nexus.evolveum.com/nexus/repository/releases/net/tirasa/connid/$artifact/1.7.0.0-M0/$artifact-1.7.0.0-M0.jar" \
    -o "$WORK/$artifact-1.7.0.0-M0.jar"
  local actual="$(shasum -a 256 "$WORK/$artifact-1.7.0.0-M0.jar" | awk '{print $1}')"
  if [[ "$actual" != "$digest" ]]; then
    printf 'Unexpected ConnId artifact digest: %s\n' "$artifact" >&2
    exit 1
  fi
}
fetch_pinned connector-framework '087b04c83f9ef82593fb925f2bd19ea3248b85cd19700f240efa4d8dbd62ebef'
fetch_pinned connector-framework-internal 'd0ef3b7733ec519fa90988a9a5055e64012972fb2813dc37f23d5ab91bd80b6e'
fetch_pinned connector-framework-contract '07f633fb5b79af6d741d653a169192473b985cb93d5e09f866e1a19f299f09bb'

CLASSPATH="$WORK/bundle:$WORK/bundle/lib/*:$FRAMEWORK:$INTERNAL:$CONTRACT"
mkdir -p "$WORK/test-classes"
javac --release 21 -cp "$CLASSPATH" -d "$WORK/test-classes" \
  "$HERE/src/test/java/com/evolveum/polygon/owncloud/OwnCloudCreateContractTest.java"
java -Downcloud.resourceXml="$CONNECTORS/../resource-owncloud.xml" \
  -cp "$WORK/test-classes:$CLASSPATH" \
  com.evolveum.polygon.owncloud.OwnCloudCreateContractTest
