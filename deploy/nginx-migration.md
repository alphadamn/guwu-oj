# Caddy → Nginx edge migration (2026-09-10)

Migrated the public edge for 154.12.60.26 from Caddy (`/etc/caddy/Caddyfile`)
back to nginx with full functional parity. Nginx now owns **80/443 TCP and
UDP 443 (QUIC)** on the public IP; Caddy is stopped and disabled but left
installed for rollback. The BT-panel loopback nginx (`127.0.0.1:80`,
`/nginx_status`) is untouched.

## Files

| File | Role |
| --- | --- |
| `/etc/caddy/Caddyfile` | previous edge (kept verbatim for rollback) |
| `/www/server/panel/vhost/nginx/0.guwu-migration-common.conf` | shared `map`/`log_format` (client-IP selection, websocket Connection, `edge_json` log) |
| `/www/server/panel/vhost/nginx/guwu.camluni.cn.conf` | OJ site (TLS+QUIC, static, Monaco, admin cache, Granian UDS proxy) |
| `/www/server/panel/vhost/nginx/lovespotter.avispotters.net.conf` | lovespotter (also the SNI `default_server`, matching Caddy's first-TLS-site rule) |
| `/www/server/panel/vhost/nginx/node_FlightBox.conf` | FlightBox (guards, security headers, conditional HTTP→HTTPS) |
| `/www/server/panel/vhost/nginx/0.default.conf` | port-80 catch-all (`root /www/server/nginx/html`) |
| `/etc/systemd/system/guwu-oj.service` | added `ExecStartPost=/usr/bin/chgrp www /run/guwu-oj/guwu-oj.sock` |

Pre-Caddy nginx of record: `*.conf.bak-migrated` in the same vhost dir.

## Caddy → nginx directive mapping (guwu.camluni.cn)

| Caddy | Nginx |
| --- | --- |
| `bind 154.12.60.26` | `listen 154.12.60.26:443 ssl;` + `listen 154.12.60.26:443 quic;` (+ `:80`) |
| edge HTTP/3 (automatic) + auto `Alt-Svc` | `quic` listener, `quic_gso on;`, `add_header Alt-Svc 'h3=":443"; ma=2592000' always;` (exactly one reuseport QUIC socket, on the `default_server` vhost) |
| `tls <cert> <key>` (TLS 1.2+1.3, Go ciphers) | `ssl_certificate*`, `ssl_protocols TLSv1.2 TLSv1.3`, Go-equivalent `ssl_ciphers`, `ssl_session_cache shared:SSL:10m` |
| `encode gzip` (min 512 B) | `gzip on; gzip_min_length 1k; gzip_proxied any; gzip_vary on;` (original site gzip block) |
| `@admin path /admin/*` → `header Cache-Control "no-store..."` | `location ^~ /admin/` + `add_header Cache-Control ... always;` (upstream header still passes → same double `Cache-Control` as Caddy) |
| `@wkblock path_regexp ...` → `respond 403` | `if ($uri ~ "^/\.well-known/.*\.(php|...)$") { return 403; }` |
| `handle /.well-known/* { root /www/wwwroot/guwu-oj }` | `location /.well-known/ { root /www/wwwroot/guwu-oj; }` |
| `handle /favicon.ico` (+ apple-touch-icon, sitemap.xml), `Cache-Control public, max-age=86400` | `location = /favicon.ico` etc. + `add_header Cache-Control "public, max-age=86400";` |
| `handle_path /static/*` → `root staticfiles` | `location /static/ { alias /www/wwwroot/guwu-oj/staticfiles/; }` |
| `@monaco` → `rewrite /npm/monaco-editor@0.52.2/{cap}` → `reverse_proxy https://cdn.jsdelivr.net` | `location ^~ /monaco/` with runtime DNS (`resolver 127.0.0.53 ipv6=off`), `rewrite ... break`, `proxy_pass https://$monaco_upstream`, `proxy_ssl_name cdn.jsdelivr.net`, 10s/600s timeouts, `proxy_hide_header Alt-Svc` (jsDelivr's own advert suppressed, as Caddy did) |
| `(guwu_cf)` / `(guwu_direct)` snippets | one proxy location + `map $http_cf_connecting_ip $guwu_client_ip` → `X-Real-IP`/`X-Forwarded-For` = `CF-Connecting-IP` when present, else peer address; `X-Forwarded-Proto https` (hard-coded, as in Caddy); `X-Forwarded-Host $host` |
| `reverse_proxy unix//run/guwu-oj/guwu-oj.sock` (HTTP/1.1, dial 60s, resp-hdr 600s) | `upstream guwu_granian` (UDS, `keepalive 64`) + `proxy_connect_timeout 60s; proxy_read_timeout 600s;` |
| `http://guwu...` → `redir permanent` | `return 301 https://guwu.camluni.cn$request_uri;` |
| `header -Alt-Svc` (lovespotter/FlightBox) | no `Alt-Svc` header emitted; no QUIC advertisement for those vhosts (listeners exist but unadvertised, as with Caddy) |
| FlightBox security headers (`X-Frame-Options`, CSP, HSTS, ...) | same `add_header ... always` set inside `location /` (guard 403/404s carry none — same as Caddy) |
| FlightBox `header_down -X-Powered-By` / `header X-Cache ""` | `proxy_hide_header X-Powered-By;` / `add_header X-Cache $upstream_cache_status always;` (empty without a cache hit → header omitted) |
| FlightBox `@redir` XFP=http conditional | `if ($http_x_forwarded_proto = "http") { return 301 ...; }` inside `location /` |
| lovespotter `@dotfiles /\.` → 403 | `location ~ /\. { deny all; }` |
| `log { format json, roll 100mb keep 10 }` | `access_log ... edge_json` (JSON mirrors Caddy fields; rotation via the existing wwwlogs rotation) |

## Deliberate deltas

1. **BT-WAF lua hooks neutralized per vhost** (`access/header/body/log_by_lua_block { return }`).
   The globally-loaded `btwaf.conf` injects `Set-Cookie: server_name_session=...` on
   every response; Caddy never ran the WAF, and the cookie would break Cloudflare
   caching. **To re-enable the WAF for a vhost, delete the four `*_by_lua_block`
   lines in that server block.**
2. **No origin proxy-cache for dynamic traffic** (Caddy cached nothing) — but
   `/monaco/` and FlightBox `/api/photos/…/image` edge-caching was **restored**
   from the pre-Caddy config (pure performance win; immutable/CDN assets).
3. `client_max_body_size 0` for guwu (Caddy set no limit; the old nginx had 50 m).
   Lovespotter 12 m, FlightBox 64 m unchanged.
4. `Via: 1.1 Caddy` is no longer added; `Server: nginx` on nginx-served static
   (`server: granian` still passes through for dynamic responses).
5. Restored original extras Caddy had dropped: CF real-IP for accurate access
   logs, `/_next/static/` disk offload (365 d immutable), sensitive-file 404 on guwu.
6. `guwu-oj.service` now `chgrp`s the UDS socket to `www` (nginx workers are
   `www`; Caddy ran as root and did not need this).

## Verification performed

- guwu: `/` 200 (h2), `/problems/` 200, `/static/**` 200 + `Cache-Control: public, max-age=86400`, `/admin/login/` double `Cache-Control` incl. `no-store`, favicon/sitemap 200 + 1 d cache, `/.env` 404, `/.well-known/**.js` 403, HTTP→HTTPS 301
- Headers: `alt-svc: h3=":443"; ma=2592000` (exactly one, Monaco included), `server: granian` passthrough, Django HSTS/CSRF intact, no WAF cookie
- FlightBox: full security-header set on 443; `:80` without `X-Forwarded-Proto` → proxied (CDN pull), with `XFP: http` → 301; `/_next/static/**` 200 from disk
- lovespotter: 200 on :80/:443, dotfiles 403, no Alt-Svc
- Unknown SNI → `www.avispotters.net` cert (lovespotter `default_server`); unknown Host on :80 → BT default page; `127.0.0.1:80/nginx_status` intact
- TLS 1.2 + 1.3 handshakes OK, valid LE cert (guwu.camluni.cn, exp. 2026-10-16)
- HTTP/3: aioquic client → `ALPN negotiated protocol h3`, 200 over QUIC
- Logs: JSON lines in `/www/wwwlogs/guwu.camluni.cn_4449.log`, `FlightBox.log`, `lovespotter.avispotters.net.log`
- Perf (origin-to-origin, median): HTML ~0.06 s (Caddy ~0.055 s), static ~0.009 s (=), Monaco ~0.010 s vs ~0.020 s (≈2× faster via edge cache)

## Rollback

```bash
systemctl stop nginx && systemctl disable nginx
systemctl enable --now caddy
# (optional) revert the guwu-oj.service ExecStartPost line + daemon-reload
```

The Caddyfile is unchanged and caddy rebinds 154.12.60.26:80/443 TCP+UDP.
