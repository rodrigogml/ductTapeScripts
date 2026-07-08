#!/usr/bin/env bash
set -euo pipefail

TABLE_FAMILY="ip"
TABLE_NAME="wildfly_net"
CHAIN_NAME="output"
WILDFLY_UID="999"
ALLOWED_CIDRS=("10.8.0.0/24" "192.168.3.0/24")

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERRO: %s\n' "$*" >&2
  exit 1
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "Execute como root ou via sudo."
  fi
}

find_nft() {
  if command -v nft >/dev/null 2>&1; then
    command -v nft
    return 0
  fi

  for candidate in /usr/sbin/nft /sbin/nft; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  fail "Comando nft não encontrado."
}

table_exists() {
  "$NFT" list table "$TABLE_FAMILY" "$TABLE_NAME" >/dev/null 2>&1
}

main() {
  require_root
  NFT="$(find_nft)"

  log "Iniciando bloqueio para o usuário wildfly (UID ${WILDFLY_UID})."

  if table_exists; then
    log "Estado anterior: a tabela ${TABLE_FAMILY} ${TABLE_NAME} já existia."
    log "Ação: removendo a tabela existente para recriá-la de forma idempotente."
    "$NFT" delete table "$TABLE_FAMILY" "$TABLE_NAME"
  else
    log "Estado anterior: a tabela ${TABLE_FAMILY} ${TABLE_NAME} não existia."
  fi

  log "Criando a tabela ${TABLE_FAMILY} ${TABLE_NAME}."
  "$NFT" add table "$TABLE_FAMILY" "$TABLE_NAME"

  log "Criando a cadeia ${CHAIN_NAME} com hook de saída e policy accept."
  "$NFT" add chain "$TABLE_FAMILY" "$TABLE_NAME" "$CHAIN_NAME" '{ type filter hook output priority filter + 10; policy accept; }'

  log "Inserindo regras permitidas para o UID ${WILDFLY_UID}."
  "$NFT" add rule "$TABLE_FAMILY" "$TABLE_NAME" "$CHAIN_NAME" meta skuid "$WILDFLY_UID" oif "lo" accept
  "$NFT" add rule "$TABLE_FAMILY" "$TABLE_NAME" "$CHAIN_NAME" meta skuid "$WILDFLY_UID" ip daddr "${ALLOWED_CIDRS[0]}" accept
  "$NFT" add rule "$TABLE_FAMILY" "$TABLE_NAME" "$CHAIN_NAME" meta skuid "$WILDFLY_UID" ip daddr "${ALLOWED_CIDRS[1]}" accept
  "$NFT" add rule "$TABLE_FAMILY" "$TABLE_NAME" "$CHAIN_NAME" meta skuid "$WILDFLY_UID" reject

  log "Resumo do que foi aplicado:"
  log "- tabela: ${TABLE_FAMILY} ${TABLE_NAME}"
  log "- cadeia: ${CHAIN_NAME} (output, priority filter + 10, policy accept)"
  log "- liberado: loopback, ${ALLOWED_CIDRS[0]}, ${ALLOWED_CIDRS[1]}"
  log "- bloqueado: qualquer outro destino para o UID ${WILDFLY_UID}"
  log "Estado final:"
  "$NFT" list table "$TABLE_FAMILY" "$TABLE_NAME"
}

main "$@"
