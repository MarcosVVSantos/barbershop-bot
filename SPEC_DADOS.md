# SPEC: Camada de Dados — barbershop-mcp

> **Contexto pro agente:** Leia este documento inteiro antes de escrever código.
> Explore a estrutura atual do projeto primeiro (schema, jobs existentes do
> APScheduler, integração com Evolution API, `barbershops.yaml`) e adapte os
> nomes/padrões abaixo ao que já existe no código. Implemente em fases, na
> ordem. Ao final de cada fase, rode os testes e me mostre o que foi feito
> antes de seguir pra próxima.

## Posicionamento (por que isso existe)

Este produto NÃO é um bot de agendamento. É um sistema de **inteligência de
negócio para barbearias**, com WhatsApp como interface. A automação (IA) é
meio; o produto é o dado: faturamento, clientes recuperados, no-show, ociosidade.

Regra de arquitetura derivada disso: **toda feature deve gerar dado analisável
e bem estruturado.** Se uma mudança não deixa rastro consultável, repensar.

## Arquitetura atual (não mudar sem necessidade)

- FastMCP + DeepSeek V3 (LLM) + Evolution API (WhatsApp)
- Multi-tenant via `barbershops.yaml`
- Jobs agendados com APScheduler (já existe job de recuperação de cliente inativo)
- Deploy: Docker Compose em VPS Hetzner CX22 (recursos limitados — evitar
  dependências pesadas; preferir matplotlib ou QuickChart para gráficos, não
  headless browser)

---

## FASE 1 — Fluxo de estados do agendamento

### Objetivo
Hoje o agendamento não distingue "cliente veio" de "cliente faltou". O no-show
é a ausência de um evento — precisa ser registrado explicitamente.

### Estados

```
agendado ──► confirmado   (cliente respondeu o lembrete prévio)
         ──► cancelado    (cliente avisou que não vai — liberar o slot)
         ──► concluido    (DEFAULT após o horário passar sem marcação contrária)
         ──► no_show      (barbeiro marcou falta no resumo do dia)
```

### Mudanças de schema
- Campo `status` no agendamento com os valores acima (enum/constraint).
- Campo opcional `confirmado_em` (timestamp).
- Garantir que o agendamento tenha `valor` do serviço (necessário pro relatório
  calcular receita e perda por no-show). Se não existir, adicionar e popular a
  partir do catálogo de serviços.
- Migração dos registros existentes: tudo no passado vira `concluido`.

### Regra de ouro
O padrão após o horário passar é `concluido`. Silêncio do barbeiro NUNCA vira
no-show — senão o número infla e o relatório perde credibilidade.

---

## FASE 2 — Job: confirmação prévia com o cliente

### Comportamento
- X horas antes do horário (configurável por tenant no `barbershops.yaml`,
  default: 3h), enviar ao cliente via Evolution API:
  > "Seu horário é hoje às {hora} com {barbeiro}. Confirma? Responda SIM ou
  > NÃO PODEREI IR"
- Resposta afirmativa → `status = confirmado`, grava `confirmado_em`.
- Resposta negativa → `status = cancelado` + notificar o barbeiro que o slot
  {hora} vagou.
- Sem resposta → mantém `agendado` (não penalizar).
- Interpretar a resposta com a camada de LLM já existente (o cliente vai
  responder coisa como "vou sim", "não vou conseguir" — não exigir palavra exata).

### Cuidados
- Não enviar confirmação pra agendamento feito com menos de X horas de
  antecedência (acabou de agendar, não faz sentido confirmar).
- Job idempotente: nunca enviar a mesma confirmação duas vezes (marcar
  `lembrete_enviado_em`).

---

## FASE 3 — Job: resumo do dia pro barbeiro (detecção de no-show)

### Comportamento
- No fim do expediente (horário de fechamento do tenant + 30min), enviar ao
  WhatsApp do dono/barbeiro:
  > "Fechamento de hoje: João 14h, Pedro 15h, Lucas 16h. Algum faltou?
  > Responda o(s) nome(s) ou NENHUM"
- Nomes citados na resposta → `status = no_show` nos respectivos agendamentos.
- "Nenhum" ou sem resposta até o dia seguinte → todos viram `concluido`.
- Manter também o comando ativo `faltou {hora}` como atalho a qualquer momento
  (integrar aos comandos admin já existentes).

---

## FASE 4 — Relatório mensal no WhatsApp (texto)

### Comportamento
- Job no dia 1º de cada mês, por tenant, enviado ao dono via Evolution API.

### Formato (texto WhatsApp, com *negrito* e emojis)

```
📊 *Resumo de {mês} — {nome da barbearia}*

💰 Faturamento: R$ {total} ({variação}% vs mês anterior)
✂️ {n} atendimentos | Ticket médio: R$ {ticket}

👥 Clientes:
• {n} novos
• {n} recorrentes
• {n} sumidos há +45 dias

✅ Recuperados pelo sistema: {n} clientes
   → R$ {valor} que voltaram pro caixa

⚠️ No-show: {n} horários (R$ ~{valor} perdidos)
📅 Horário mais ocioso: {dia} {faixa}

_Responda RELATORIO pra ver mais detalhes_
```

### Definições das métricas (importante — não improvisar)
- **Faturamento**: soma de `valor` dos agendamentos `concluido` no mês.
- **Cliente novo**: primeiro agendamento `concluido` da vida dele foi neste mês.
- **Cliente recorrente**: já tinha atendimento `concluido` em mês anterior.
- **Sumido**: último `concluido` há mais de 45 dias (reusar a lógica do job de
  inativos existente).
- **Recuperado pelo sistema**: cliente que recebeu mensagem do job de
  recuperação de inativos E teve agendamento `concluido` até 30 dias depois.
  Valor recuperado = soma desses atendimentos. **Esta é a métrica mais
  importante do relatório** — é ela que justifica a mensalidade.
- **Perda por no-show**: soma de `valor` dos `no_show`.
- **Horário ocioso**: faixa (dia da semana + intervalo de horas) com menor taxa
  de ocupação dentro do horário de funcionamento do tenant.
- Comando sob demanda: dono manda `relatorio` a qualquer momento → recebe o
  parcial do mês corrente.

---

## FASE 5 (futura — NÃO implementar agora, só não bloquear)

- Gráficos como imagem via matplotlib/QuickChart enviados no WhatsApp.
- Página web por link mágico (`/r/{token}`, sem login) com detalhes: lista de
  sumidos com telefone, ranking de serviços, comissão por barbeiro.
- Ao modelar as queries das fases anteriores, deixar as agregações em funções
  reutilizáveis (o relatório web vai consumir as mesmas).

---

## Critérios de aceite gerais

1. Multi-tenant: tudo configurável por barbearia no `barbershops.yaml`
   (horários dos jobs, antecedência da confirmação, telefone do dono).
2. Jobs idempotentes e com log — se o VPS reiniciar, nada é enviado em dobro.
3. Timezone América/São_Paulo em todos os jobs.
4. Testes para: transições de estado, cálculo de cada métrica do relatório
   (com dados sintéticos), e idempotência dos jobs.
5. Nenhuma mensagem nova ao cliente final fora das especificadas (não virar spam).
