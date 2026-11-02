#!/usr/bin/env bash
# Run inside the matching Ubuntu guest, never on the physical host.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
export WORLD_KERNEL_ARCH=x86
exec bash "$here/build.sh" "$@"
