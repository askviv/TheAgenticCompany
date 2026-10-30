#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/midpoint-scimrest.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

BASE_URL="https://nexus.evolveum.com/nexus/repository/releases/com/evolveum/polygon/scimrest/connector-scimrest-generic/0.2.0/connector-scimrest-generic-0.2.0.jar"
EXPECTED_SHA256="fb1ddce38fcb52237be2664a65513045507dc7d72e0b1021761ef605b23c2741"
curl -fsSL "$BASE_URL" -o "$WORK/scimrest-generic-0.2.0.jar"
ACTUAL_SHA256="$(shasum -a 256 "$WORK/scimrest-generic-0.2.0.jar" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  printf 'Unexpected Evolveum SCIMREST 0.2.0 artifact digest: %s\n' "$ACTUAL_SHA256" >&2
  exit 1
fi

CONNID_URL="https://nexus.evolveum.com/nexus/repository/releases/net/tirasa/connid/connector-framework/1.7.0.0-M0/connector-framework-1.7.0.0-M0.jar"
CONNID_FRAMEWORK="$WORK/connector-framework-1.7.0.0-M0.jar"
curl -fsSL "$CONNID_URL" -o "$CONNID_FRAMEWORK"
EXPECTED_CONNID_SHA256="087b04c83f9ef82593fb925f2bd19ea3248b85cd19700f240efa4d8dbd62ebef"
ACTUAL_CONNID_SHA256="$(shasum -a 256 "$CONNID_FRAMEWORK" | awk '{print $1}')"
if [[ "$ACTUAL_CONNID_SHA256" != "$EXPECTED_CONNID_SHA256" ]]; then
  printf 'Unexpected ConnId 1.7.0.0-M0 artifact digest: %s\n' "$ACTUAL_CONNID_SHA256" >&2
  exit 1
fi

build_bundle() {
  local app="$1"
  local connector_class="$2"
  local bundle_name="$3"
  local source="$HERE/$app/src/main"
  local stage="$WORK/$app"
  local classes="$WORK/$app-classes"
  local output="$HERE/build/connector-$app-0.2.0-tac.1.jar"
  mkdir -p "$stage" "$classes" "$HERE/build"
  unzip -q "$WORK/scimrest-generic-0.2.0.jar" -d "$stage"
  javac --release 21 -cp "$stage/lib/*:$CONNID_FRAMEWORK" -d "$classes" $(find "$source/java" -name '*.java' -print)
  cp -R "$classes"/* "$stage/"
  cp "$source/resources/"*.groovy "$stage/"
  cp "$source/resources/connector.manifest.json" "$stage/connector.manifest.json"
  cat "$source/resources/Messages.properties" >> "$stage/Messages.properties"
  local manifest="$WORK/$app.MF"
  python3 - "$manifest" "$connector_class" "$bundle_name" <<'PY'
from pathlib import Path
import sys

manifest, connector_class, bundle_name = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
manifest.write_text(
    "Manifest-Version: 1.0\r\n"
    "ConnectorBundle-ConnectorClass: " + connector_class + "\r\n"
    "ConnectorBundle-FrameworkVersion: 1.7.0.0\r\n"
    "ConnectorBundle-Name: " + bundle_name + "\r\n"
    "ConnectorBundle-Version: 0.2.0-tac.1\r\n\r\n",
    encoding="ascii",
)
PY
  rm "$stage/META-INF/MANIFEST.MF"
  jar --create --file "$output" --manifest "$manifest" -C "$stage" .
  printf '%s  ' "$(shasum -a 256 "$output" | awk '{print $1}')"
  printf '%s\n' "$output"
}

build_bundle rocketchat com.evolveum.polygon.rocketchat.RocketChatConnector com.evolveum.polygon.scimrest.connector-rocketchat
build_bundle mailu com.evolveum.polygon.mailu.MailuConnector com.evolveum.polygon.scimrest.connector-mailu
build_bundle owncloud com.evolveum.polygon.owncloud.OwnCloudConnector com.evolveum.polygon.scimrest.connector-owncloud
