# bis2Buster

## Objetivo

Monitorar sinais basicos de saude do BIS2 a partir do banco MySQL, com foco inicial em cupons NFC-e que indicam atraso, fila ou falhas relacionadas a SEFAZ.

A v1 consulta a tabela `fiscal_docfiscal`, consolida as contagens por categoria e envia um unico relatorio por alvo monitorado via NotiCLI.

## Dor que resolve

Evita verificacao manual recorrente de cupons acumulados no BIS2 e torna visivel, por notificacao, quando ha sinais de falha operacional:

- problemas na SEFAZ;
- cupons em fila com mais de 3 horas;
- cupons em SEFAZ offline direto;
- cupons em SEFAZ offline corrigidos.

## Como utilizar

### Pre-requisitos

1. Linux.
2. `Python 3.13+`.
3. Pacote Python `mysql-connector-python`.
4. `noticli` disponivel no `PATH`.
5. Usuario MySQL somente leitura com acesso a tabela `fiscal_docfiscal`.
6. Um arquivo local `bis2Buster.toml` baseado em `bis2Buster.toml.model`.

Instalacao da dependencia MySQL:

```bash
python3 -m pip install mysql-connector-python
```

### Execucao local

```bash
cd bis2Buster && python3 bis2Buster.py
```

O script procura `bis2Buster.toml` na mesma pasta do executavel. Use `--config` para apontar outro arquivo.

```bash
python3 bis2Buster.py --config /opt/bis2Buster/bis2Buster.toml
```

### Configuracao

O arquivo real `bis2Buster.toml` nao deve ser versionado. Use `bis2Buster.toml.model` como base.

Estrutura geral:

- `[global]`: defaults do monitor, MySQL, janela de consulta e NotiCLI.
- `[global.notify.success]`: notificacao de sucesso, com `category = "SUCCESS"` e sem prioridade.
- `[global.notify.error]`: notificacao de falha, com `category = "FAIL"` e `priority = "HIGH"`.
- `[jobs."<nome>"]`: conexao MySQL do BIS2 monitorado.
- `[jobs."<nome>".notify.success]`: sobrescrita opcional para sucesso.
- `[jobs."<nome>".notify.error]`: sobrescrita opcional para falha.

Nao coloque credenciais reais no arquivo `.model`, no README ou em commits.

### Janela de consulta

Por padrao, o script consulta os ultimos 3 meses no fuso `America/Sao_Paulo`, usando data final exclusiva:

```text
data_inicio = agora - 3 meses
data_fim    = agora
```

Para uma janela fixa, configure `data_inicio` e `data_fim` juntos no job ou no bloco global:

```toml
data_inicio = "2026-01-01T00:00:00"
data_fim = "2026-01-02T00:00:00"
```

### Regras monitoradas

O script filtra apenas:

```sql
df.type = 'NFCe'
```

E usa o relacionamento:

```sql
LEFT JOIN fiscal_docfiscal dfg
    ON dfg.id = df.subDeviceId
```

Categorias:

- `Problemas na SEFAZ`: falha se houver qualquer cupom com `df.status IN ('SEFAZPROBLEM', 'SEFAZERROR')`.
- `Cupons em Fila`: falha se houver qualquer cupom dentro da janela escolhida que ja esteja em fila ha pelo menos `queue_age_hours` horas, com `df.status = 'ERROR_SYNC'`, `df.deviceId IS NULL` e documento relacionado nao `ERROR_SYNC`.
- `SEFAZ Offline (Direto)`: falha se houver qualquer cupom com `df.status = 'SEFAZOFFLINE'` e `df.subDeviceId IS NULL`.
- `SEFAZ Offline (Corrigidos)`: falha se houver qualquer cupom com `df.status = 'SEFAZOFFLINE'` e `df.subDeviceId IS NOT NULL`.

O script nao soma categorias diferentes. Cada categoria aparece em linha propria no relatorio.

### Saida

Exemplo de relatorio:

```text
BIS2 monitor falhou

- Job: bis2-producao
- Status: FAIL
- Periodo: 2026-07-13 00:00:00 -> 2026-07-14 00:00:00
- Fila limite: 2026-07-13 12:30:00
- Checks:
  - Problemas na SEFAZ: ok (nenhum cupom encontrado)
  - Cupons em Fila: fail (2 cupom(ns) encontrado(s))
  - SEFAZ Offline (Direto): ok (nenhum cupom encontrado)
  - SEFAZ Offline (Corrigidos): ok (nenhum cupom encontrado)
```

### NotiCLI

O `bis2Buster` chama o NotiCLI como dependencia externa.

Para sucesso, a notificacao usa `category = "SUCCESS"` e nao envia `--priority`. Para falha, usa `category = "FAIL"` e `--priority HIGH`.

Se `global.noticli_config` nao estiver definido, o script deixa o NotiCLI usar o padrao do sistema. Se esse campo existir, ele e passado como `--config`.

### Deploy em producao

1. Crie a arvore de producao versionada:

```bash
sudo mkdir -p /opt/bis2Buster/releases/v3.0.0
sudo ln -sfn /opt/bis2Buster/releases/v3.0.0 /opt/bis2Buster/current
```

2. Valide o script:

```bash
cd bis2Buster
python3 -m py_compile bis2Buster.py test_bis2Buster.py
python3 -m unittest test_bis2Buster.py
```

3. Instale os arquivos da release:

```bash
sudo install -d /opt/bis2Buster/releases/v3.0.0
sudo install -m 0755 bis2Buster.py /opt/bis2Buster/releases/v3.0.0/bis2Buster.py
sudo install -m 0644 README.md /opt/bis2Buster/releases/v3.0.0/README.md
sudo install -m 0644 bis2Buster.toml.model /opt/bis2Buster/releases/v3.0.0/bis2Buster.toml.model
sudo ln -sfn /opt/bis2Buster/bis2Buster.toml /opt/bis2Buster/releases/v3.0.0/bis2Buster.toml
```

4. Crie o arquivo real em `/opt/bis2Buster/bis2Buster.toml`.

5. Execute:

```bash
python3 /opt/bis2Buster/current/bis2Buster.py
```

### Agendamento

Em producao, o agendamento recomendado fica em `/etc/cron.d/bis2Buster` e executa como `rodrigogml` todos os dias as 06:00 e 18:00:

```cron
0 6,18 * * * rodrigogml PYTHONDONTWRITEBYTECODE=1 /usr/bin/flock -n /tmp/bis2Buster.lock /opt/bis2Buster/venv/bin/python /opt/bis2Buster/current/bis2Buster.py --config /opt/bis2Buster/bis2Buster.toml >> /opt/bis2Buster/bis2Buster.log 2>&1
```

## Informacoes adicionais

- A v1 e somente leitura no banco.
- O retorno `0` indica sucesso em todos os jobs.
- O retorno `1` indica falha em alguma checagem.
- O retorno `2` indica erro de configuracao.
- O retorno `3` indica erro ao notificar.
- O retorno `4` indica erro runtime, como dependencia MySQL ausente.
- O script nao altera dados do BIS2 e nao executa correcao automatica.
