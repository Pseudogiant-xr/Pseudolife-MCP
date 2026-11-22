#!/usr/bin/env bash
# Invoked only inside the disposable Intel runner's private Ubuntu VM.
set -euo pipefail
umask 077
tag=$1
commit=$2
root="$HOME/.acceptance"
mkdir "$root" "$root/bin" "$root/home"
cd "$root"

sudo -n apt-get -qq update
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -qq install -y +    ca-certificates curl docker.io docker-compose-v2 git
sudo -n systemctl start docker
for executable in /usr/bin/* /bin/* /usr/sbin/* /sbin/*; do
    case "${executable##*/}" in
        python*|pypy*|pip*|uv*|gh|tailscale|xdg-open|sensible-browser|docker) continue ;;
    esac
    if [[ -f "$executable" && -x "$executable" && ! -e "bin/${executable##*/}" ]]; then
        ln -s "$executable" "bin/${executable##*/}"
    fi
done
printf '#!/bin/sh\nexec /usr/bin/sudo -n /usr/bin/docker "$@"\n' > bin/docker
chmod 700 bin/docker

codex=codex-x86_64-unknown-linux-musl
curl --disable --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    "https://github.com/openai/codex/releases/download/rust-v0.171.0/$codex.tar.gz" -o codex.tar.gz
printf '01baa2e47809f8597be5b2f8abd4e4dfe3fb5bcb6e504c2fbaaad97056c15e8d  codex.tar.gz\n' | sha256sum -c
tar -xzf codex.tar.gz -- "$codex"
mv "$codex" bin/codex

git clone --quiet --depth=1 --branch "$tag" https://github.com/Pseudogiant-xr/Pseudolife-MCP.git release
cd release
test "$(git rev-parse HEAD)" = "$commit"
env -i HOME="$root/home" CODEX_HOME="$root/home/.codex" PATH="$root/bin" LC_ALL=C \
    PSEUDOLIFE_SHIM_ENGINE=native PSEUDOLIFE_UNATTENDED=1 \
    bash ops/install.sh --yes --client codex --extractor openai-only --engine rust --firewall-rule no

# Keep the original Compose project and loopback listener. Add only the
# private vzNAT listener, after installing the host-only ingress rule.
cidr=$(ip -o -4 addr show dev lima0 scope global | awk 'NR == 1 {print $4}')
read -r host_address guest_address < <(python3 -c '
import ipaddress
import sys
guest = ipaddress.IPv4Interface(sys.argv[1])
assert guest.ip.is_private and not guest.ip.is_loopback and not guest.ip.is_link_local
print(guest.network.network_address + 1, guest.ip)
' "$cidr")
sudo -n iptables -I DOCKER-USER 1 -i lima0 -p tcp -m conntrack +    --ctorigdst "$guest_address" --ctorigdstport 8765 -j DROP
sudo -n iptables -I DOCKER-USER 1 -i lima0 -s "$host_address/32" -p tcp -m conntrack +    --ctorigdst "$guest_address" --ctorigdstport 8765 -j RETURN
sudo -n iptables -C DOCKER-USER -i lima0 -s "$host_address/32" -p tcp -m conntrack +    --ctorigdst "$guest_address" --ctorigdstport 8765 -j RETURN

compose_config=$(sudo -n docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' pseudolife-mcp-daemon)
IFS=, read -ra files <<< "$compose_config"
compose=()
for file in "${files[@]}"; do
    [[ "$file" == "$root/release/ops/"* && -f "$file" ]]
    compose+=(-f "$file")
done
test "${#compose[@]}" -gt 0
cat > "$root/private-listener.yml" <<EOF
services:
  pseudolife-daemon:
    ports: !override
      - "127.0.0.1:8765:8765"
      - "$guest_address:8765:8765"
EOF
sudo -n docker compose --env-file ops/.env "${compose[@]}" -f "$root/private-listener.yml" \
    up -d --no-deps --no-build pseudolife-daemon

token=$(sed -n 's/^PSEUDOLIFE_MCP_TOKEN=//p' ops/.env | tail -1)
test -n "$token"
printf '%s\n' "$token" > "$root/owner.token"
cat > "$root/owner" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
root="$HOME/.acceptance"
cd "$root/release"
exec env -i HOME="$root/home" CODEX_HOME="$root/home/.codex" PATH="$root/bin" LC_ALL=C \
    PSEUDOLIFE_MCP_TOKEN_FILE="$root/owner.token" PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765 \
    PSEUDOLIFE_MCP_NO_SPAWN=1 "$root/home/.local/share/pseudolife-mcp/bin/pseudolife-mcp" "$@"
EOF
chmod 700 "$root/owner"
printf 'peer-ready\n%s\n%s\n' "$guest_address" "$token"
