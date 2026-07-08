<!--
Sync Impact Report
- Version: 1.0.0 -> 1.0.1
- Princípios modificados: none
- Seções adicionadas: Core Principles, Operational Scope, Rule Model, Governance
- Seções removidas: none
- Artefatos que precisam atualização: none at this stage
- TODOs pendentes: define initial backend strategy for enforcement; confirm whether process/service matching is MVP or future phase
-->
# Network Access Control Scripts Constitution

## Core Principles

### Explicit Scope Required

Every rule MUST target at least one explicit subject and one explicit network scope. A subject is a Linux user, service, or process identity. A network scope is a CIDR, host, interface, or a clearly named preset such as "internet" or "lan". Broad or global rules MUST be rejected unless the operator confirms them interactively.

### Least Privilege by Default

The default posture MUST be deny-by-default for new restrictions and allow-by-exception for reopenings. The script MUST support a workflow where a target starts with a broad block and receives explicit allow rules afterward, because that model matches operational containment better than ad hoc exceptions.

### Reversible and Idempotent Operations

Block, unblock, and list commands MUST be safe to repeat without duplicating state or drifting the policy. Every applied rule MUST have a stable identifier, tag, or equivalent bookkeeping so the matching unblock action can remove exactly what was created.

### Auditability Over Hidden State

The tool MUST be able to show effective rules and stored intents in a form that an operator can review quickly. Listing MUST support filtering by user, process or service, action, and destination scope. Each change MUST record who or what it targeted, what action was taken, and when it was applied.

### Backend-Agnostic Rule Intent

The CLI MUST describe policy intent separately from the firewall backend that enforces it. The first implementation may target one backend, but the command contract MUST keep room for nftables, iptables, firewalld, or a future backend adapter without changing the user-facing policy model.

## Operational Scope

This project is a Linux command-line utility for rapid network control around a selected user, initially using `wildfly` as the concrete example but not hard-coding that user into the design.

The initial operating model SHOULD prioritize outbound control for a user or process, because that is the common case for "internet access" and "local network access" restrictions. Inbound control may be added later, but it MUST not weaken the clarity of outbound rule handling.

The first release SHOULD support these operations:

- block access for a user to a CIDR or named scope
- allow access for a user to a CIDR or named scope
- list rules globally
- list rules filtered by user
- list rules filtered by target scope
- explain the effective policy for a specific user or process

The first release MAY defer process and service matching if the backend cannot do it safely and portably. In that case, the script MUST still expose a stable command shape that can accept those arguments later without breaking the core workflow.

## Rule Model

Rules SHOULD be represented as policy intent with the following attributes:

- subject: user, service, or process
- action: allow or block
- destination: CIDR, host, interface, or named preset
- direction: outbound by default
- scope label: internet, lan, or a custom label
- comment: human-readable reason
- created_at: timestamp
- expires_at: optional expiration for temporary exceptions

The model SHOULD support a baseline block plus explicit exceptions, and also the inverse baseline when an environment needs selective denial. The tool MUST keep those modes explicit so operators do not confuse a temporary exception with a permanent policy.

## Governance

Changes that alter the command contract, the rule model, or the backend abstraction MUST be documented before implementation. New script folders MUST include their own README with purpose, pain solved, detailed usage, and operational notes.

Security-sensitive operations such as backend reset, full rule flush, or broad network unlocks MUST require either an explicit confirmation prompt or a clearly named dangerous flag. Silent destructive behavior is not allowed.

Versioning follows SemVer:

- MAJOR for incompatible changes to the command contract or rule semantics
- MINOR for new commands, new rule dimensions, or backend expansion
- PATCH for clarifications and non-semantic corrections

**Version**: 1.0.1 | **Ratified**: 2026-07-08 | **Last Amended**: 2026-07-08
