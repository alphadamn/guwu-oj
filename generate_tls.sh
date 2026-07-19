#!/bin/bash

set -e



echo "=== Redis TLS 证书生成向导 ==="

read -p "证书存放目录 [默认: /etc/redis/tls]: " CERT_DIR

CERT_DIR=${CERT_DIR:-/etc/redis/tls}

read -p "CA 证书的 CN [默认: MyRootCA]: " CA_CN

CA_CN=${CA_CN:-MyRootCA}

read -p "服务器证书的 CN (标识用) [默认: redis-server]: " SERVER_CN

SERVER_CN=${SERVER_CN:-redis-server}

read -p "CA 有效期（天）[默认: 3650]: " DAYS_CA

DAYS_CA=${DAYS_CA:-3650}

read -p "服务器有效期（天）[默认: 365]: " DAYS_SERVER

DAYS_SERVER=${DAYS_SERVER:-365}

read -p "IP 列表（逗号分隔）[默认: 127.0.0.1,192.168.196.147]: " IP_LIST

IP_LIST=${IP_LIST:-"127.0.0.1,192.168.196.147"}

read -p "DNS 列表（逗号分隔）[默认: localhost,*.redis.local]: " DNS_LIST

DNS_LIST=${DNS_LIST:-"localhost,*.redis.local"}



echo "配置确认："

echo "  目录: $CERT_DIR"

echo "  CA CN: $CA_CN"

echo "  服务器 CN: $SERVER_CN"

echo "  IP: $IP_LIST"

echo "  DNS: $DNS_LIST"

read -p "确认？[y/N]: " CONFIRM

[[ "$CONFIRM" =~ ^[Yy]$ ]] || exit 0



mkdir -p "$CERT_DIR" && cd "$CERT_DIR"



# ----- 生成 CA 私钥 -----

openssl genrsa -out ca.key 2048

chmod 600 ca.key



# ----- 生成 CA 证书（单行命令 + -subj）-----

openssl req -x509 -new -key ca.key -out ca.crt -days "$DAYS_CA" -subj "/C=CN/ST=Beijing/L=Beijing/O=GuwuOJ/CN=$CA_CN" -extensions v3_ca -config <(cat <<EOF

[ v3_ca ]

basicConstraints = critical, CA:TRUE

keyUsage = critical, keyCertSign, cRLSign

subjectKeyIdentifier = hash

authorityKeyIdentifier = keyid:always,issuer

EOF

)



# ----- 生成服务器私钥 -----

openssl genrsa -out redis.key 2048

chmod 600 redis.key



# ----- 生成服务器 CSR（单行 + -subj）-----

openssl req -new -key redis.key -out redis.csr -subj "/C=CN/ST=Beijing/L=Beijing/O=GuwuOJ/CN=$SERVER_CN"



# ----- 构建 SAN 配置（动态生成）-----

SAN_CONFIG=""

idx=1

IFS=',' read -ra ips <<< "$IP_LIST"

for ip in "${ips[@]}"; do

    SAN_CONFIG+="IP.$idx = $ip"$'\n'

    ((idx++))

done

IFS=',' read -ra dns <<< "$DNS_LIST"

for d in "${dns[@]}"; do

    SAN_CONFIG+="DNS.$idx = $d"$'\n'

    ((idx++))

done



cat > san.cnf <<EOF

[ v3_req ]

keyUsage = keyEncipherment, dataEncipherment, digitalSignature

extendedKeyUsage = serverAuth, clientAuth

subjectAltName = @alt_names

[ alt_names ]

$SAN_CONFIG

EOF



# ----- 签署服务器证书（单行）-----

openssl x509 -req -in redis.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out redis.crt -days "$DAYS_SERVER" -extensions v3_req -extfile san.cnf



# ----- 清理 -----

rm -f redis.csr ca.srl san.cnf

chmod 600 ca.key redis.key

chmod 644 ca.crt redis.crt



echo "✅ 证书生成成功！文件位于：$CERT_DIR"

ls -l "$CERT_DIR"

echo ""

echo "配置 redis.conf："

echo "  tls-cert-file $CERT_DIR/redis.crt"

echo "  tls-key-file $CERT_DIR/redis.key"

echo "  tls-ca-cert-file $CERT_DIR/ca.crt"

echo "  tls-auth-clients no"
