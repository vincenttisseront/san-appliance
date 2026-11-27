#!/bin/bash
# Archive d'application publiée avec la release GitHub.
# Produit san-appliance-app.tar.gz et san-appliance-app.tar.gz.sha256.
# Ne signe pas : aucune clé privée n'est créée ici.
set -euo pipefail
dest=${1:-dist}
version=${2:-0}
mkdir -p "$dest/app-stage"
printf '%s\n' "$version" > "$dest/app-stage/version"
cat > "$dest/app-stage/health" << 'EOF'
#!/bin/sh
set -eu
test -s "$(dirname "$0")/version"
EOF
chmod 0755 "$dest/app-stage/health"
tar -C "$dest/app-stage" -czf "$dest/san-appliance-app.tar.gz" version health
(
    cd "$dest"
    sha256sum san-appliance-app.tar.gz > san-appliance-app.tar.gz.sha256
)
rm -rf "$dest/app-stage"
printf 'packed %s version %s\n' "$dest/san-appliance-app.tar.gz" "$version"
