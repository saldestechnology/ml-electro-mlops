#!/usr/bin/env bash
# Install (or upgrade) the monitoring binaries for the current user under ~/.local/opt, no root.
#   install.sh [prometheus alertmanager node_exporter grafana]   default: all
# Each tarball is checked against the SHA-256 the project publishes before it is unpacked.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/versions.env"
OPT=$HOME/.local/opt
mkdir -p "$OPT"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

prom_project() {  # <name> <version>: prometheus/* release with sha256sums.txt
  local name=$1 ver=$2 file="$1-$2.linux-amd64.tar.gz"
  local url="https://github.com/prometheus/$name/releases/download/v$ver"
  [[ -x $OPT/$name-$ver/$name ]] && { echo "$name $ver present"; return; }
  curl -fsSL -o "$tmp/$file" "$url/$file"
  curl -fsSL -o "$tmp/sums" "$url/sha256sums.txt"
  (cd "$tmp" && grep " $file\$" sums | sha256sum -c --quiet -)
  tar -xzf "$tmp/$file" -C "$OPT" && mv "$OPT/${file%.tar.gz}" "$OPT/$name-$ver"
  ln -sfn "$name-$ver" "$OPT/$name"
  echo "$name $ver installed"
}

grafana() {
  local ver=$GRAFANA_VERSION file="grafana-$GRAFANA_VERSION.linux-amd64.tar.gz"
  [[ -x $OPT/grafana-$ver/bin/grafana ]] && { echo "grafana $ver present"; return; }
  curl -fsSL -o "$tmp/$file" "https://dl.grafana.com/oss/release/$file"
  echo "$GRAFANA_SHA256  $tmp/$file" | sha256sum -c --quiet -
  mkdir -p "$OPT/grafana-$ver"
  tar -xzf "$tmp/$file" -C "$OPT/grafana-$ver" --strip-components=1
  ln -sfn "grafana-$ver" "$OPT/grafana"
  echo "grafana $ver installed"
}

for what in "${@:-prometheus alertmanager node_exporter grafana}"; do
  for w in $what; do
    case $w in
      prometheus) prom_project prometheus "$PROMETHEUS_VERSION" ;;
      alertmanager) prom_project alertmanager "$ALERTMANAGER_VERSION" ;;
      node_exporter) prom_project node_exporter "$NODE_EXPORTER_VERSION" ;;
      grafana) grafana ;;
      *) echo "unknown: $w" >&2; exit 1 ;;
    esac
  done
done
