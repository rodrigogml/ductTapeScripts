# noMore404

## Objetivo

Verificar se sites estão vivos, respondendo e respeitando o comportamento esperado por domínio.

O fluxo roda um conjunto de checks por domínio, monta um único relatório por domínio e envia esse relatório via NotiCLI.

## Dor que resolve

Evita checagens manuais repetitivas de:

- resposta HTTP esperada;
- redirects entre domínios relacionados;
- redirect de `http` para `https`;
- tempo de carregamento do índice;
- status final consolidado por domínio.

## Como utilizar

### Pré-requisitos

1. Linux.
2. `Python 3.13+`.
3. `curl` instalado.
4. `noticli` disponível no `PATH`.
5. Um arquivo local `noMore404.toml` baseado em `noMore404.toml.model`.

### Execução local

```bash
cd noMore404 && python3 noMore404.py
```

O script procura `noMore404.toml` na mesma pasta do executável. Use `--config` para apontar outro arquivo.

### Deploy em produção

1. Crie a árvore de produção versionada:

```bash
sudo mkdir -p /opt/noMore404/releases/v2.0.2
sudo ln -sfn /opt/noMore404/releases/v2.0.2 /opt/noMore404/current
```

2. Faça o build de validação:

```bash
cd noMore404
python3 -m py_compile noMore404.py test_noMore404.py
python3 -m unittest test_noMore404.py
```

3. Gere um pacote `night build`:

```bash
BUILD_ID="$(date +%Y%m%d-%H%M%S)"
tar -czf "/tmp/noMore404-night-build-${BUILD_ID}.tar.gz" \
  README.md \
  noMore404.py \
  noMore404.toml.model \
  test_noMore404.py
```

4. Desdobre o pacote na release versionada:

```bash
sudo install -d /opt/noMore404/releases/v2.0.2
sudo install -m 0755 noMore404.py /opt/noMore404/releases/v2.0.2/noMore404.py
sudo install -m 0644 README.md /opt/noMore404/releases/v2.0.2/README.md
sudo install -m 0644 noMore404.toml.model /opt/noMore404/releases/v2.0.2/noMore404.toml.model
sudo ln -sfn /opt/noMore404/noMore404.toml /opt/noMore404/releases/v2.0.2/noMore404.toml
```

5. Crie o arquivo real de configuração em `/opt/noMore404/noMore404.toml`.

6. Execute o watchdog:

```bash
python3 /opt/noMore404/current/noMore404.py
```

### Configuração

O arquivo real `noMore404.toml` não deve ser versionado. Use o arquivo modelo `noMore404.toml.model` como base.

Estrutura geral:

- `[global]`: defaults do watchdog e do NotiCLI.
- `[global.notify.success]`: sobrescrita geral para sucesso.
- `[global.notify.error]`: sobrescrita geral para falha.
- `[jobs."<domínio>"]`: configuração do domínio.
- `[jobs."<domínio>".notify.success]`: sobrescrita do domínio para sucesso.
- `[jobs."<domínio>".notify.error]`: sobrescrita do domínio para falha.

Cada job pode ligar ou desligar os checks por domínio:

- `check_http_200`
- `check_http_to_https`
- `check_index_time`
- `check_redirects`

Os redirects são declarados como pares `source` -> `target`, então é possível validar tanto `www` -> raiz quanto a direção inversa, se isso fizer parte da tarefa.
Se quiser validar `http` e `https` para a mesma origem, crie uma regra por esquema.
Quando o destino final precisa obrigatoriamente ficar em `https`, declare `target_scheme = "https"` na regra.

### Exemplo de configuração real

Um arquivo de produção precisa, no mínimo, definir:

- `global.noticli_bin`
- `global.sender`
- `global.notify.success`
- `global.notify.error`
- `jobs."<domínio>"`
- `jobs."<domínio>".redirects`
- os checks habilitados para cada domínio

O fluxo recomendado para um primeiro domínio é:

- domínio principal em `primary_domain`;
- aliases em `redirects`;
- `category = "SUCCESS"` em notificações de sucesso;
- `category = "FAIL"` e `priority = "HIGH"` em notificações de falha;
- `check_http_200 = true`;
- `check_http_to_https = true`;
- `check_index_time = true`;
- `check_redirects = true`.

### Saída

Cada domínio gera um relatório único com status curto por check.

Exemplo:

```text
Website monitor falhou

- Dominio: xpto.com.br
- Status: FAIL
- Checks:
  - 200: ok (ok)
  - www.xpto.com.br->xpto.com.br: ok (ok)
  - http->https: fail (fail(https://xpto.com.br:0))
  - index: ok (ok 842ms)
```

### NotiCLI

O `noMore404` chama o NotiCLI como dependência externa.

O script monta o comando no modelo broadcast do NotiCLI v2:

- `send`
- `--sender`
- `--category`
- `--title`
- `--message`
- `--priority`, somente quando configurado

O `sender` recomendado para este projeto é `noMore404`. Para sucesso, use `category = "SUCCESS"` e deixe `priority` ausente para o NotiCLI assumir `NORMAL`. Para falha, use `category = "FAIL"` e `priority = "HIGH"`.

Se `global.noticli_config` não estiver definido, o script deixa o NotiCLI usar o padrão do sistema. Se esse campo existir, ele é passado como `--config`. O arquivo de configuração do NotiCLI deve continuar fora deste repositório e pode reutilizar a mesma base operacional usada por outros projetos, como `vaultGFS`.
Se o NotiCLI não tiver um arquivo padrão válido no host, a notificação falhará mesmo sem `--config`.

### Versionamento

Este repositório usa tags para identificar a versão do script em produção:

- `major`: entrou um script novo no repositório;
- `minor`: houve uma melhoria grande em um script existente;
- `revision`: houve revisão/correção em qualquer script.

Em produção, a versão ativa fica em `/opt/noMore404/releases/<tag>/` e o symlink `/opt/noMore404/current` aponta para a release em uso.

## Informações adicionais

- Um domínio só é considerado sucesso quando 100% dos checks habilitados passam.
- Se qualquer check falhar, o relatório daquele domínio vai para o canal de falha.
- Se não houver checks habilitados para um domínio, o domínio é tratado como falha de configuração operacional.
- A mensagem é propositalmente curta e objetiva.
- O script é pensado para execução direta, não para alterar o NotiCLI.
