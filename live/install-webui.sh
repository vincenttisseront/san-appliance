#!/bin/bash
# Build the pinned loopback WebUI on the Ubuntu 24.04 host and install it
# into a Noble rootfs. No Docker. Native addons are built here, for glibc 2.39.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf 'install-webui: run as root\n' >&2
    exit 1
fi

ROOT=${1:?rootfs path}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
NODE_VERSION=22.23.4
NODE_SHA256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
PIN_FILE=$REPO_ROOT/image/third_party/esos-webui.sha256
CACHE=$SCRIPT_DIR/work/cache
SRC=$SCRIPT_DIR/work/esos-webui
NODE_DIR=$CACHE/node-v$NODE_VERSION-linux-x64

log() { printf 'install-webui: %s\n' "$*"; }
die() { printf 'install-webui: %s\n' "$*" >&2; exit 1; }

[ -d "$ROOT" ] || die "rootfs missing: $ROOT"
[ -f "$PIN_FILE" ] || die "missing pin $PIN_FILE"
PIN=$(tr -d '[:space:]' < "$PIN_FILE")
[[ "$PIN" =~ ^[0-9a-f]{40}$ ]] || die "esos-webui pin is not a 40-digit git sha"

for cmd in curl git g++ make python3; do
    command -v "$cmd" >/dev/null || die "missing host command: $cmd"
done

mkdir -p "$CACHE"
tarball=$CACHE/node-v$NODE_VERSION-linux-x64.tar.xz
if [ ! -f "$tarball" ] || ! echo "$NODE_SHA256  $tarball" | sha256sum -c - >/dev/null 2>&1; then
    log "download node v$NODE_VERSION"
    curl -fsSL --retry 3 --retry-delay 2 --max-time 180 \
        -o "$tarball.partial" \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"
    echo "$NODE_SHA256  $tarball.partial" | sha256sum -c - || die "node tarball checksum mismatch"
    mv "$tarball.partial" "$tarball"
fi
if [ ! -x "$NODE_DIR/bin/node" ]; then
    rm -rf "$NODE_DIR"
    mkdir -p "$NODE_DIR"
    tar -xJf "$tarball" -C "$NODE_DIR" --strip-components=1
fi
"$NODE_DIR/bin/node" -p 'process.version' | grep -qx "v$NODE_VERSION" || die "node binary is not v$NODE_VERSION"

export PATH="$NODE_DIR/bin:$PATH"
export NODE_OPTIONS=--max-old-space-size=3072
export npm_config_audit=false
export npm_config_fund=false

if [ ! -d "$SRC/.git" ]; then
    rm -rf "$SRC"
    git -c safe.directory='*' init "$SRC"
    git -c safe.directory='*' -C "$SRC" remote add origin https://github.com/vincenttisseront/esos-webui.git
fi
log "fetch esos-webui $PIN"
git -c safe.directory='*' -C "$SRC" fetch --depth 1 origin "$PIN"
git -c safe.directory='*' -C "$SRC" checkout --detach --force FETCH_HEAD
[ "$(git -c safe.directory='*' -C "$SRC" rev-parse HEAD)" = "$PIN" ] || die "pin mismatch"
rm -rf "$SRC/.output"
log "npm ci && npm run build"
(
    cd "$SRC"
    npm ci
    npm run build
)
[ -f "$SRC/.output/server/index.mjs" ] || die "webui bundle missing"
[ -f "$SRC/scripts/serve-local.mjs" ] || die "serve-local.mjs missing from the pinned tree"
[ -f "$SRC/server/db/migrations/meta/_journal.json" ] || die "drizzle journal missing"

log "load native modules with the node that the image will ship"
(
    cd "$SRC/.output/server"
    node -e '
        const Database = require("better-sqlite3");
        const db = new Database(":memory:");
        db.exec("create table t(x int)");
        require("argon2");
        require("cpu-features");
        process.stdout.write("native-ok\n");
    '
) || die "native modules failed to load under node v$NODE_VERSION"

if grep -q 'server/index.mjs' "$ROOT/etc/systemd/system/san-webui.service" 2>/dev/null; then
    die "san-webui.service must not launch server/index.mjs"
fi
grep -q '/usr/lib/san-webui/scripts/serve-local.mjs' "$ROOT/etc/systemd/system/san-webui.service" \
    || die "san-webui.service does not start serve-local.mjs"

rm -rf "$ROOT/usr/lib/san-webui"
mkdir -p "$ROOT/usr/lib/san-webui/server/db"
cp -a "$SRC/scripts" "$ROOT/usr/lib/san-webui/scripts"
cp -a "$SRC/.output" "$ROOT/usr/lib/san-webui/.output"
cp -a "$SRC/server/db/migrations" "$ROOT/usr/lib/san-webui/server/db/migrations"
cp -a "$SRC/package.json" "$ROOT/usr/lib/san-webui/package.json"
install -m 0755 "$NODE_DIR/bin/node" "$ROOT/usr/bin/node"
chown -R root:root "$ROOT/usr/lib/san-webui" "$ROOT/usr/bin/node"
find "$ROOT/usr/lib/san-webui" -type d -exec chmod 0755 {} +
find "$ROOT/usr/lib/san-webui" -type f -exec chmod 0644 {} +
chmod 0755 \
    "$ROOT/usr/local/sbin/san-webui-listen-check" \
    "$ROOT/usr/local/sbin/san-webui-secrets"

if ! chroot "$ROOT" getent passwd sanui >/dev/null; then
    chroot "$ROOT" useradd \
        --system \
        --user-group \
        --home-dir /var/lib/san/webui \
        --shell /usr/sbin/nologin \
        sanui
fi
chroot "$ROOT" install -d -o sanui -g sanui -m 0750 /var/lib/san/webui
if [ -e "$ROOT/var/lib/san/webui/webui.env" ]; then
    die "webui.env is baked into the image"
fi
grep -q '^sanui:' "$ROOT/etc/passwd"
grep -q '/usr/sbin/nologin' "$ROOT/etc/passwd"

log "load native modules inside the Noble rootfs"
chroot "$ROOT" /usr/bin/node -e '
    const Database = require("/usr/lib/san-webui/.output/server/node_modules/better-sqlite3");
    const db = new Database(":memory:");
    db.exec("create table t(x int)");
    require("/usr/lib/san-webui/.output/server/node_modules/argon2");
    require("/usr/lib/san-webui/.output/server/node_modules/cpu-features");
    process.stdout.write("rootfs-native-ok\n");
' || die "native modules do not load in the Noble rootfs"

printf '%s\n' "$PIN" > "$ROOT/usr/lib/san-webui/SOURCE_COMMIT"
chmod 0644 "$ROOT/usr/lib/san-webui/SOURCE_COMMIT"
log "installed node v$NODE_VERSION and esos-webui $PIN"
