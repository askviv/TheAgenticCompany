#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
ROCKET_JAR="$HERE/build/connector-rocketchat-0.2.0-tac.1.jar"
MAILU_JAR="$HERE/build/connector-mailu-0.2.0-tac.1.jar"
for jar in "$ROCKET_JAR" "$MAILU_JAR"; do
  if [[ ! -f "$jar" ]]; then
    printf 'Build the connectors first; expected %s\n' "$jar" >&2
    exit 2
  fi
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/native-create-contract.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

fetch_pinned() {
  local artifact="$1"
  local digest="$2"
  local target="$WORK/$artifact-1.7.0.0-M0.jar"
  curl -fsSL \
    "https://nexus.evolveum.com/nexus/repository/releases/net/tirasa/connid/$artifact/1.7.0.0-M0/$artifact-1.7.0.0-M0.jar" \
    -o "$target"
  local actual
  actual="$(shasum -a 256 "$target" | awk '{print $1}')"
  if [[ "$actual" != "$digest" ]]; then
    printf 'Unexpected ConnId artifact digest: %s\n' "$artifact" >&2
    exit 1
  fi
}

fetch_pinned connector-framework '087b04c83f9ef82593fb925f2bd19ea3248b85cd19700f240efa4d8dbd62ebef'
fetch_pinned connector-framework-internal 'd0ef3b7733ec519fa90988a9a5055e64012972fb2813dc37f23d5ab91bd80b6e'
fetch_pinned connector-framework-contract '07f633fb5b79af6d741d653a169192473b985cb93d5e09f866e1a19f299f09bb'
unzip -q "$ROCKET_JAR" -d "$WORK/bundle"

docker run --rm \
  -v "$ROOT:/source:ro" \
  -v "$WORK:/work" \
  -w /work \
  eclipse-temurin:21-jdk-alpine sh -c '
    CP="/work/bundle/lib/*:/work/connector-framework-1.7.0.0-M0.jar:/work/connector-framework-internal-1.7.0.0-M0.jar:/work/connector-framework-contract-1.7.0.0-M0.jar:/source/deploy/single-host/midpoint/connectors/build/connector-rocketchat-0.2.0-tac.1.jar:/source/deploy/single-host/midpoint/connectors/build/connector-mailu-0.2.0-tac.1.jar"
    mkdir -p /work/classes
    javac --release 21 -cp "$CP" -d /work/classes /source/deploy/single-host/midpoint/connectors/ConnectorCreateContractTest.java
    java --add-modules jdk.httpserver -cp "/work/classes:$CP" ConnectorCreateContractTest \
      /source/deploy/single-host/midpoint/connectors/build/connector-rocketchat-0.2.0-tac.1.jar \
      /source/deploy/single-host/midpoint/connectors/build/connector-mailu-0.2.0-tac.1.jar
  '
