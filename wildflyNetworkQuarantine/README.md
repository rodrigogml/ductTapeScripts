# wildflyNetworkQuarantine

## Objetivo

Este mini-projeto existe para isolar a aplicação BIS2, executada sob o usuário `wildfly`, do acesso à internet e de redes internas fora das faixas explicitamente permitidas.

A regra aplicada aqui replica a restrição encontrada no sistema: o tráfego de saída do UID `999` é liberado apenas para `localhost`, `10.8.0.0/24` e `192.168.3.0/24`, com rejeição para qualquer outro destino.

## Dor que resolve

Este bloqueio foi criado como resposta operacional à falha de `07/07/2026`, quando os serviços da SEFAZ aceitavam a conexão, mas não respondiam.

Naquele cenário, o BIS2 não estava preparado para entrar em contingência quando a conexão de rede aparentava estar disponível, mas o serviço remoto permanecia sem resposta. O resultado prático era manter a aplicação tentando seguir por um caminho improdutivo, em vez de forçar o comportamento de contingência.

Ao negar a saída de rede do `wildfly` para destinos fora das faixas previstas, o script ajuda a provocar o isolamento necessário para esse tipo de operação controlada.

## Como utilizar

### Pré-requisitos

1. Sistema Linux com `nftables`.
2. Execução como `root` ou via `sudo`.
3. O usuário `wildfly` deve continuar com UID `999`, ou o script precisará ser ajustado.

### Scripts disponíveis

1. `blockWildflyNetwork.sh`
2. `unblockWildflyNetwork.sh`
3. `statusWildflyNetwork.sh`

### Como aplicar o bloqueio

```bash
sudo ./blockWildflyNetwork.sh
```

O script:

1. verifica se a tabela `ip wildfly_net` já existia;
2. remove essa tabela se ela já estiver presente;
3. recria a tabela e a cadeia `output` com `priority filter + 10` e `policy accept`;
4. adiciona as regras que permitem apenas `lo`, `10.8.0.0/24` e `192.168.3.0/24`;
5. bloqueia qualquer outro destino para o UID `999`.

### Como desfazer o bloqueio

```bash
sudo ./unblockWildflyNetwork.sh
```

O script remove integralmente a tabela `ip wildfly_net`, garantindo que tudo o que foi criado pelo bloqueador seja eliminado.

### Como inspecionar o status

```bash
sudo ./statusWildflyNetwork.sh
```

O script de status mostra rapidamente:

1. a identidade do usuário `wildfly`;
2. o estado do serviço `wildfly`;
3. se a tabela `ip wildfly_net` existe;
4. o conteúdo completo da tabela, quando presente;
5. os trechos do `ruleset` relacionados à quarentena;
6. um teste prático de conectividade como `wildfly`.

### Saída esperada

Os scripts exibem mensagens claras sobre:

1. o que já existia antes da execução;
2. o que foi criado, recriado ou removido;
3. qual é o estado final após a operação.

## Informações adicionais

### Idempotência

O script de bloqueio é idempotente por desenho: se a tabela já existir, ela é removida e recriada, evitando múltiplas tabelas ou entradas duplicadas.

O script de desbloqueio também é idempotente: se a tabela não existir, ele apenas informa que não havia nada para remover.

### Escopo técnico

Este mini-projeto foi escrito para o cenário observado neste repositório, em um host Debian 13 com `nftables`.

### Limitações

- O UID `999` é assumido como o usuário `wildfly`.
- As faixas liberadas estão fixadas no script.
- Se o ambiente mudar, o script precisa ser revisado antes de reutilização em outro host.

### Observação operacional

O bloqueio foi pensado para uso deliberado em uma janela de contingência, não como configuração genérica permanente de firewall.
