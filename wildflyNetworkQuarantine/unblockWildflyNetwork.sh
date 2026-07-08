#!/usr/bin/env bash
set -euo pipefail

TABLE_FAMILY="ip"
TABLE_NAME="wildfly_net"

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

  log "Iniciando remoção do bloqueio ${TABLE_FAMILY} ${TABLE_NAME}."

  if table_exists; then
    log "Estado anterior: a tabela ${TABLE_FAMILY} ${TABLE_NAME} existia."
    log "Ação: removendo a tabela e tudo o que ela criou."
    "$NFT" delete table "$TABLE_FAMILY" "$TABLE_NAME"
    log "Remoção concluída."
  else
    log "Estado anterior: a tabela ${TABLE_FAMILY} ${TABLE_NAME} não existia."
    log "Nenhuma alteração foi necessária."
  fi

  if table_exists; then
    log "Estado final: a tabela ${TABLE_FAMILY} ${TABLE_NAME} ainda está presente."
  else
    log "Estado final: a tabela ${TABLE_FAMILY} ${TABLE_NAME} não está presente."
  fi
}

main "$@"
