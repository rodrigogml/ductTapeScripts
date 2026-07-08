#!/usr/bin/env bash
set -euo pipefail

TABLE_FAMILY="ip"
TABLE_NAME="wildfly_net"
CHAIN_NAME="output"
WILDFLY_USER="wildfly"
WILDFLY_UID="999"
ALLOWED_CIDRS=("10.8.0.0/24" "192.168.3.0/24")
TEST_HOSTS=("1.1.1.1:443" "192.168.3.77:80" "10.8.1.1:80")

log() {
  printf '%s\n' "$*"
}

section() {
  printf '\n== %s ==\n' "$*"
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

find_run_as_user() {
  if command -v runuser >/dev/null 2>&1; then
    printf '%s\n' "$(command -v runuser)"
    return 0
  fi

  if command -v su >/dev/null 2>&1; then
    printf '%s\n' "$(command -v su)"
    return 0
  fi

  fail "Nem runuser nem su foram encontrados."
}

table_exists() {
  "$NFT" list table "$TABLE_FAMILY" "$TABLE_NAME" >/dev/null 2>&1
}

show_service_state() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl show wildfly \
      -p LoadState \
      -p ActiveState \
      -p SubState \
      -p User \
      -p Group \
      -p FragmentPath \
      -p IPAddressAllow \
      -p IPAddressDeny \
      -p PrivateNetwork \
      -p RestrictAddressFamilies \
      2>/dev/null || true
  else
    log "systemctl não encontrado."
  fi
}

show_table() {
  if table_exists; then
    log "Tabela encontrada: ${TABLE_FAMILY} ${TABLE_NAME}"
    "$NFT" list table "$TABLE_FAMILY" "$TABLE_NAME"
  else
    log "Tabela ausente: ${TABLE_FAMILY} ${TABLE_NAME}"
  fi
}

show_ruleset_matches() {
  log "Trechos do ruleset relacionados ao bloqueio:"
  "$NFT" list ruleset | awk '
    /table ip wildfly_net/ { show=1 }
    show { print }
    show && /^\}/ { show=0 }
  ' || true
}

show_user_identity() {
  if command -v getent >/dev/null 2>&1; then
    getent passwd "$WILDFLY_USER" || true
    getent group "$WILDFLY_USER" || true
  fi
}

test_connectivity() {
  local runner
  runner="$(find_run_as_user)"

  log "Testes como ${WILDFLY_USER} (UID ${WILDFLY_UID}):"

  for target in "${TEST_HOSTS[@]}"; do
    host="${target%:*}"
    port="${target##*:}"

    if [ "$runner" = "$(command -v runuser 2>/dev/null || true)" ]; then
      if timeout 5 runuser -u "$WILDFLY_USER" -- bash -lc "cat < /dev/null >/dev/tcp/${host}/${port}" >/dev/null 2>&1; then
        log "- ${host}:${port} -> OK"
      else
        log "- ${host}:${port} -> FALHA"
      fi
    else
      if timeout 5 su -s /bin/bash "$WILDFLY_USER" -c "cat < /dev/null >/dev/tcp/${host}/${port}" >/dev/null 2>&1; then
        log "- ${host}:${port} -> OK"
      else
        log "- ${host}:${port} -> FALHA"
      fi
    fi
  done
}

main() {
  require_root
  NFT="$(find_nft)"

  section "Identidade"
  log "Usuário alvo: ${WILDFLY_USER}"
  log "UID esperado: ${WILDFLY_UID}"
  show_user_identity

  section "Serviço"
  show_service_state

  section "Quarentena"
  if table_exists; then
    log "Status: ATIVA"
  else
    log "Status: AUSENTE"
  fi
  show_table

  section "Ruleset"
  show_ruleset_matches

  section "Teste de acesso"
  log "Permitidos esperados pela regra: ${ALLOWED_CIDRS[0]}, ${ALLOWED_CIDRS[1]}, loopback"
  log "Endpoints de teste: 1.1.1.1:443, 192.168.3.77:80, 10.8.1.1:80"
  test_connectivity
}

main "$@"
