#!/bin/bash
# WebHarbor + Caddy entrypoint: backdated TLS chain, Caddyfile, then the upstream start script.
set -e

CONFIG=/opt/webharbor-config/sites.json
CADDYFILE=/etc/caddy/Caddyfile
CA_DIR=/var/lib/caddy/pki/authorities/local

mkdir -p /etc/caddy /var/lib/caddy "$CA_DIR"

# Backdated chain: agents run under a frozen past clock, so real-time certs look not-yet-valid.
DOMAINS=$(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
print(' '.join(d for s in cfg['sites'] for d in s['domains']))
")

# Reuse an existing chain only if it is complete and covers every sites.json domain.
need_gen=0
if [ -f "$CA_DIR/root.crt" ] && [ -f "$CA_DIR/root.key" ] && \
   [ -f "$CA_DIR/leaf.crt" ] && [ -f "$CA_DIR/leaf.key" ]; then
    LEAF_SANS=$(openssl x509 -in "$CA_DIR/leaf.crt" -noout -ext subjectAltName 2>/dev/null || true)
    for d in $DOMAINS localhost; do
        case "$LEAF_SANS" in
            *"DNS:$d"*) ;;
            *) echo "[entrypoint] leaf.crt missing SAN $d — regenerating chain"
               need_gen=1; break ;;
        esac
    done
else
    need_gen=1
fi

if [ "$need_gen" = 1 ]; then
    echo "[entrypoint] Generating backdated TLS chain (2020-01-01 -> 2036-01-01)..."
    WORK=$(mktemp -d)
    mkdir -p "$WORK/newcerts"
    : > "$WORK/index.txt"
    echo 1000 > "$WORK/serial"

    # `openssl ca` sets explicit notBefore/notAfter on OpenSSL 3.0 (`req -not_before` needs 3.4).
    cat > "$WORK/openssl.cnf" <<EOF
[ ca ]
default_ca = wildawm_ca

[ wildawm_ca ]
database         = $WORK/index.txt
serial           = $WORK/serial
new_certs_dir    = $WORK/newcerts
default_md       = sha256
policy           = policy_loose
x509_extensions  = v3_ca
copy_extensions  = none
unique_subject   = no
email_in_dn      = no

[ policy_loose ]
commonName             = supplied
organizationName       = optional

[ v3_ca ]
basicConstraints       = critical,CA:TRUE,pathlen:0
keyUsage               = critical,keyCertSign,cRLSign,digitalSignature
subjectKeyIdentifier   = hash

[ v3_leaf ]
basicConstraints       = CA:FALSE
keyUsage               = critical,digitalSignature,keyEncipherment
extendedKeyUsage       = serverAuth
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer
subjectAltName         = @alt_names

[ alt_names ]
EOF
    i=1
    for d in $DOMAINS localhost; do
        echo "DNS.$i = $d" >> "$WORK/openssl.cnf"
        i=$((i + 1))
    done

    openssl genrsa -out "$CA_DIR/root.key" 2048 2>/dev/null
    openssl req -new -key "$CA_DIR/root.key" \
        -subj "/O=WildAWM/CN=WildAWM WebHarbor Root CA" -out "$WORK/ca.csr"
    openssl ca -config "$WORK/openssl.cnf" -selfsign -batch \
        -startdate 200101000000Z -enddate 360101000000Z \
        -keyfile "$CA_DIR/root.key" -in "$WORK/ca.csr" \
        -out "$CA_DIR/root.crt" -notext -extensions v3_ca

    openssl genrsa -out "$CA_DIR/leaf.key" 2048 2>/dev/null
    openssl req -new -key "$CA_DIR/leaf.key" \
        -subj "/O=WildAWM/CN=WildAWM WebHarbor" -out "$WORK/leaf.csr"
    openssl ca -config "$WORK/openssl.cnf" -batch \
        -startdate 200101000000Z -enddate 360101000000Z \
        -cert "$CA_DIR/root.crt" -keyfile "$CA_DIR/root.key" \
        -in "$WORK/leaf.csr" -out "$CA_DIR/leaf.crt" -notext -extensions v3_leaf

    rm -f "$CA_DIR/intermediate.crt" "$CA_DIR/intermediate.key"
    rm -rf "$WORK"
    echo "[entrypoint] TLS chain written to $CA_DIR ($(echo $DOMAINS | wc -w) site domains + localhost)"
else
    echo "[entrypoint] Reusing existing TLS chain in $CA_DIR (covers all sites.json domains)"
fi

# Agent containers (uid 1000) mount this dir read-only and trust root.crt.
chmod 644 "$CA_DIR/root.crt" "$CA_DIR/leaf.crt"
chmod 600 "$CA_DIR/root.key" "$CA_DIR/leaf.key"

python3 - <<'PY'
import json, sys
cfg = json.load(open("/opt/webharbor-config/sites.json"))
sites = cfg["sites"]
cert = "/var/lib/caddy/pki/authorities/local/leaf.crt"
key = "/var/lib/caddy/pki/authorities/local/leaf.key"
lines = [
    "# Auto-generated from /opt/webharbor-config/sites.json — do not hand-edit.",
    "{",
    "    auto_https disable_redirects",
    "    admin off",
    "}",
    "",
]
for s in sites:
    hosts = ", ".join(s["domains"])
    lines += [
        f"{hosts} {{",
        f"    tls {cert} {key}",
        f"    reverse_proxy 127.0.0.1:{s['port']} {{",
        f"        header_up Host {{upstream_hostport}}",
        f"    }}",
        f"}}",
        "",
    ]
open("/etc/caddy/Caddyfile", "w").write("\n".join(lines))
print(f"[entrypoint] Wrote Caddyfile with {len(sites)} sites", flush=True)
PY

echo "[entrypoint] Starting Caddy..."
caddy start --config /etc/caddy/Caddyfile --adapter caddyfile

# The upstream start script execs control_server.py as PID 1.
echo "[entrypoint] Handing off to /opt/websyn_start.sh"
exec /opt/websyn_start.sh
