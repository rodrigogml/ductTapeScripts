# IPaparazzi

## Objetivo

Monitorar o IPv4 público de uma conexão e manter registros DNS `A` existentes sincronizados quando esse endereço mudar.

O IPaparazzi funciona mesmo quando a máquina está atrás de roteador, firewall ou NAT, porque consulta serviços externos para descobrir o endereço visto pela internet. Uma execução pode administrar vários registros, zonas e contas Cloudflare.

## Dor que resolve

Conexões com IP dinâmico podem trocar de endereço sem aviso e deixar VPNs, acessos remotos, websites e outros serviços apontando para um endereço antigo. O IPaparazzi automatiza a correção sem consultar desnecessariamente a API DNS em toda execução.

## Funcionamento

Cada execução segue esta sequência:

1. Consulta, em paralelo, três fontes fixas de IPv4 público:
   - Cloudflare: `https://1.1.1.1/cdn-cgi/trace`;
   - AWS: `https://checkip.amazonaws.com/`;
   - ipify: `https://api.ipify.org/`.
2. Valida cada resposta e rejeita endereços privados, reservados, loopback, multicast ou inválidos.
3. Continua somente quando pelo menos duas fontes retornam exatamente o mesmo IPv4.
4. Compara o consenso com o estado local de cada registro.
5. Não chama a API DNS quando o IP permanece igual e o registro foi confirmado nas últimas 24 horas.
6. Quando o IP muda ou a confirmação expira, consulta o registro na Cloudflare.
7. Atualiza conteúdo, TTL ou estado de proxy somente quando houver diferença.
8. Registra o resultado, salva o estado atomicamente e envia uma notificação relevante pelo NotiCLI.

Uma falha afeta apenas o registro correspondente. Os demais registros e contas continuam sendo processados.

> [!IMPORTANT]
> O IPaparazzi nunca cria registros ausentes. Um registro `A` configurado precisa existir previamente na Cloudflare; caso contrário, ele falha com segurança e os demais continuam.

> [!WARNING]
> Atualizar o DNS não abre portas nem configura redirecionamento no roteador ou firewall. Em conexões sujeitas a CGNAT, o IPv4 público detectado pode não aceitar conexões de entrada mesmo com o DNS correto; isso precisa ser confirmado com o provedor de internet.

## Pré-requisitos

- Windows ou Linux.
- Python 3.13 ou mais recente.
- Uma conta Cloudflare com os registros `A` já criados.
- Um API Token da Cloudflare com permissão `DNS Write`, limitado às zonas necessárias.
- NotiCLI disponível no `PATH`, caso as notificações estejam habilitadas.
- Linux: `cron` para instalação do agendamento.
- Windows: módulo nativo `ScheduledTasks` do PowerShell para instalação do agendamento.

O projeto usa somente a biblioteca padrão do Python.

## Configuração inicial

Copie o modelo sem remover ou editar o arquivo versionado:

### Linux

```bash
cd IPaparazzi
cp IPaparazzi.toml.model IPaparazzi.toml
chmod 600 IPaparazzi.toml
```

### Windows PowerShell

```powershell
Set-Location IPaparazzi
Copy-Item IPaparazzi.toml.model IPaparazzi.toml
```

Edite `IPaparazzi.toml`. O arquivo real está no `.gitignore` e pode conter os tokens das contas.

> [!CAUTION]
> Nunca versione `IPaparazzi.toml`, publique seu conteúdo ou passe o token na linha de comando. O script não registra tokens, mas o arquivo precisa permanecer acessível somente ao usuário da execução, aos administradores e, quando utilizado, ao usuário `SYSTEM` do Windows.

### Configuração global

| Campo | Padrão | Finalidade |
|-------|--------|------------|
| `state_file` | `IPaparazzi.state.json` | Estado confirmado de cada registro e resultado da execução anterior. |
| `log_file` | `IPaparazzi.log` | Log rotativo da aplicação. |
| `lock_file` | `IPaparazzi.lock` | Trava contra execuções simultâneas. |
| `reconcile_hours` | `24` | Prazo máximo antes de consultar novamente cada registro no provedor. |
| `request_timeout_seconds` | `10` | Timeout individual das chamadas HTTPS. |
| `request_retries` | `2` | Tentativas por fonte de descoberta de IP. |
| `retry_delay_seconds` | `1.0` | Espera progressiva entre tentativas. |
| `lock_stale_minutes` | `60` | Idade após a qual uma trava abandonada pode ser removida. |
| `log_max_bytes` | `5242880` | Limite de 5 MB por arquivo de log. |
| `log_backup_count` | `5` | Quantidade de arquivos antigos preservados. |

Caminhos relativos são resolvidos a partir da pasta do arquivo TOML, e não do diretório atual do terminal.

### Contas e registros Cloudflare

Cada conta possui seu próprio token e sua própria lista de registros:

```toml
[providers.cloudflare]

[[providers.cloudflare.accounts]]
name = "conta-principal"
enabled = true
api_token = "TOKEN_REAL_DA_CONTA"

[[providers.cloudflare.accounts.records]]
enabled = true
zone_id = "ID_DA_ZONA"
name = "remote.example.com"
proxied = false
ttl = 120

[[providers.cloudflare.accounts.records]]
enabled = true
zone_id = "ID_DA_ZONA"
name = "web.example.com"
proxied = true
ttl = 1
```

| Campo | Obrigatório | Descrição |
|-------|-------------|-----------|
| `name` da conta | Sim | Identificador local único, sem relação obrigatória com o nome na Cloudflare. |
| `enabled` | Não | Permite desabilitar uma conta ou registro sem removê-lo; padrão `true`. |
| `api_token` | Sim | Token da conta. Contas habilitadas rejeitam o valor `CHANGE_ME`. |
| `zone_id` | Sim | ID da zona Cloudflare que contém o registro. |
| `name` do registro | Sim | Nome completo, como `remote.example.com`. |
| `proxied` | Não | `true` para Proxied ou `false` para DNS Only; padrão `false`. |
| `ttl` | Não | TTL desejado. O padrão é `1`, que significa Auto na API Cloudflare. |

Registros com `proxied = true` obrigatoriamente usam `ttl = 1`, pois a Cloudflare controla o TTL desses registros.

> [!WARNING]
> `proxied` não é apenas uma opção de cache. O tráfego passa pela rede Cloudflare e fica limitado aos protocolos e portas atendidos pelo produto. RDP na porta 3389, SSH e outros serviços TCP comuns devem usar `proxied = false`, salvo quando houver um produto compatível, como Cloudflare Spectrum.

### Notificações

As notificações são configuradas em `[global.notifications]`. O IPaparazzi não envia mensagens em execuções normais sem alteração.

Eventos possíveis:

- `changed`: pelo menos um registro foi atualizado;
- `error`: faltou consenso ou houve falha em algum registro;
- `recovered`: a execução anterior falhou e a atual terminou sem erros.

```toml
[global.notifications]
enabled = true
noticli_bin = "noticli"
sender = "IPaparazzi"
# noticli_config = "C:/caminho/para/noticli.toml"

[global.notifications.error]
category = "FAIL"
priority = "HIGH"
title = "IPaparazzi falhou"
message = "{report}"
```

O placeholder `{report}` contém o resumo da execução. O placeholder `{event}` contém o nome do evento em maiúsculas.

## Uso manual

### Validar a configuração

```bash
python IPaparazzi.py --config IPaparazzi.toml --check-config
```

Esse comando não consulta a internet nem altera DNS.

### Executar normalmente

```bash
python IPaparazzi.py --config IPaparazzi.toml
```

### Forçar reconciliação

```bash
python IPaparazzi.py --config IPaparazzi.toml --force-reconcile
```

Use `--force-reconcile` para ignorar o cache de 24 horas e consultar todos os registros habilitados. O comando continua atualizando somente quando encontra diferenças.

## Instalação do agendamento

Antes de instalar, copie e preencha `IPaparazzi.toml`. Os dois instaladores validam a configuração e substituem o agendamento anterior de mesmo nome, evitando duplicidade.

### Deploy de produção versionado

Mantenha o código de cada release separado da configuração e dos dados operacionais:

```text
<raiz>/IPaparazzi/
├── IPaparazzi.toml
├── current -> releases/<tag>
└── releases/
    └── <tag>/
        ├── IPaparazzi.py
        ├── IPaparazzi.toml.model
        ├── README.md
        ├── install-cron.sh
        └── install-task.ps1
```

O arquivo real `IPaparazzi.toml`, os logs e o estado ficam fora de `releases/<tag>`. Os instaladores devem receber explicitamente esse arquivo quando a configuração estiver na raiz operacional.

Exemplo no Windows:

```powershell
X:\opt\IPaparazzi\current\install-task.ps1 `
  -ScriptPath "X:\opt\IPaparazzi\current\IPaparazzi.py" `
  -ConfigPath "X:\opt\IPaparazzi\IPaparazzi.toml" `
  -IntervalMinutes 15
```

### Linux com cron

O intervalo padrão é 15 minutos:

```bash
chmod +x install-cron.sh IPaparazzi.py
./install-cron.sh --interval 15
```

Também são aceitos `1`, `2`, `3`, `4`, `5`, `6`, `10`, `12`, `20`, `30` e `60` minutos. Esses valores dividem uma hora inteira e evitam intervalos irregulares na expressão do cron.

Para uma instalação fora da pasta do projeto:

```bash
./install-cron.sh \
  --interval 20 \
  --python /usr/bin/python3 \
  --script /opt/IPaparazzi/current/IPaparazzi.py \
  --config /opt/IPaparazzi/IPaparazzi.toml
```

O instalador:

- valida a configuração;
- aplica permissão `600` ao TOML real;
- aplica permissão `755` ao script;
- remove a entrada anterior marcada como gerenciada pelo IPaparazzi;
- instala a nova entrada no `crontab` do usuário atual.

### Windows com Agendador de Tarefas

Abra o PowerShell e execute:

```powershell
.\install-task.ps1 -IntervalMinutes 15
```

Por padrão, a tarefa executa como o usuário atual enquanto ele estiver conectado. Para um servidor que precise executar mesmo sem sessão interativa, abra o PowerShell como administrador e use:

```powershell
.\install-task.ps1 -IntervalMinutes 15 -RunAsSystem
```

O modo `SYSTEM` precisa conseguir localizar o Python, o NotiCLI e qualquer configuração externa do NotiCLI. Informe caminhos absolutos quando necessário:

```powershell
.\install-task.ps1 `
  -IntervalMinutes 20 `
  -PythonExe "C:\Python313\python.exe" `
  -ScriptPath "C:\Apps\IPaparazzi\IPaparazzi.py" `
  -ConfigPath "C:\Apps\IPaparazzi\IPaparazzi.toml"
```

O instalador restringe a ACL do TOML real ao usuário atual, administradores e `SYSTEM`. Use `-SkipAclHardening` somente quando as permissões forem gerenciadas por outro mecanismo.

## Logs e estado

O log registra:

- início e fim de cada execução;
- resultado individual das três fontes;
- IPv4 aceito pelo consenso;
- registro confirmado, atualizado, ignorado, desabilitado ou com falha;
- resultado das notificações.

O estado JSON não contém tokens. Sua gravação ocorre em arquivo temporário no mesmo diretório e termina com substituição atômica, reduzindo o risco de corrupção durante uma interrupção.

Uma trava impede execuções simultâneas. O cron e o Agendador de Tarefas também são configurados para reduzir sobreposição, mas a trava protege execuções manuais concorrentes.

## Códigos de saída

| Código | Significado |
|--------|-------------|
| `0` | Execução concluída sem erros. |
| `1` | Não houve consenso entre pelo menos duas fontes de IPv4. |
| `2` | Configuração inexistente ou inválida. |
| `3` | Um ou mais registros falharam no provedor. |
| `4` | DNS foi processado, mas o NotiCLI falhou. |
| `5` | Falha local de execução, estado ou arquivo. |
| `6` | Outra execução ainda mantém a trava. |
| `130` | Execução interrompida pelo operador. |

## Testes

```bash
python -m py_compile IPaparazzi.py test_IPaparazzi.py
python -m unittest test_IPaparazzi.py
```

Os testes não acessam a Cloudflare nem alteram DNS; as integrações externas são substituídas por implementações controladas.

## Limitações e cuidados

- O MVP suporta somente IPv4 e registros `A`.
- As três fontes de descoberta são fixas no código.
- A reconciliação só encontra registros dentro do `zone_id` configurado.
- Alterações manuais podem permanecer sem detecção por até `reconcile_hours` quando o IPv4 local não muda.
- Um token inválido afeta somente os registros daquela conta.
- O proxy Cloudflare pode ocultar o IP de origem no DNS, mas não transforma protocolos incompatíveis em tráfego suportado.
- A execução precisa de acesso HTTPS às fontes, à API Cloudflare e aos destinos usados pelo NotiCLI.

## Referências

- [Cloudflare: tipos de registros DNS](https://developers.cloudflare.com/dns/manage-dns-records/reference/dns-record-types/)
- [Cloudflare: atualização de registros pela API](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/edit/)
- [Cloudflare: status de proxy](https://developers.cloudflare.com/dns/proxy-status/)
- [Cloudflare: portas compatíveis com proxy](https://developers.cloudflare.com/fundamentals/reference/network-ports/)
- [Cloudflare: endpoint `/cdn-cgi/`](https://developers.cloudflare.com/fundamentals/reference/cdn-cgi-endpoint/)
- [AWS: `checkip.amazonaws.com`](https://docs.aws.amazon.com/cli/v1/userguide/bash_rds_code_examples.html)
- [ipify: API pública](https://www.ipify.org/)

O briefing aprovado que originou esta implementação está em [`docs/briefing/20260722-briefing.md`](docs/briefing/20260722-briefing.md).
