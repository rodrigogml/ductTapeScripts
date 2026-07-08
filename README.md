# ductTapeScripts

Este repositório é uma coleção de scripts criados e utilizados em situações específicas.

A proposta é manter cada solução pequena, direta e bem documentada. Quando um script existir aqui, ele deve viver na sua própria pasta, junto com um `README.md` interno detalhando o contexto e o uso.

## Organização esperada

- Cada script deve ter sua própria pasta.
- Cada pasta deve conter um `README.md` próprio.
- O `README.md` interno deve cobrir, no mínimo:
  - Objetivo: para que serve, finalidade, utilização e contexto.
  - Dor resolvida: qual problema ele resolve ou resolveu.
  - Manual de uso: passo a passo detalhado de como utilizar.
  - Informações adicionais: observações, limitações, dependências, exemplos e qualquer outro tópico necessário.

## network-access-control

Script para administrar regras de acesso à rede por usuário Linux, com foco inicial em bloquear e liberar tráfego para casos como o `wildfly`.
O objetivo é permitir operações rápidas de bloqueio, liberação e auditoria de regras com base em usuário, e evoluir depois para serviço, processo e filtros mais finos.

- README interno: [network-access-control/README.md](network-access-control/README.md)

## template-script

Modelo inicial de estrutura para novos scripts deste repositório.
Serve como referência de organização e documentação para futuras pastas de scripts.

- README interno: [template-script/README.md](template-script/README.md)

## wildflyNetworkQuarantine

Mini-projeto para isolar o tráfego de saída do `wildfly` e forçar o BIS2 a operar em contingência quando o acesso à SEFAZ não for confiável.

- README interno: [wildflyNetworkQuarantine/README.md](wildflyNetworkQuarantine/README.md)
