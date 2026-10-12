# Roadmap de sprints — Hexavante File Organizer

Execute **somente a sprint autorizada**, sem avanço automático.

## Sprint 00 — Fundação
**Agente:** Arquiteto + Coordenador

**Escopo:** MVC, GTK4/Libadwaita, SQLite, migrações, testes headless e documentação. Sem I/O nos diretórios pessoais.

**Aceite:** implementação comprovada, testes executados e registrados, revisão QA, documentação atualizada, autorização humana para prosseguir.

## Sprint 01 — Scanner
**Agente:** Filesystem

**Escopo:** Raízes autorizadas, MIME, inventário, exclusão de symlinks/especiais e limites.

**Aceite:** implementação comprovada, testes executados e registrados, revisão QA, documentação atualizada, autorização humana para prosseguir.

## Sprint 02 — Classificação
**Agente:** Classificação

**Escopo:** Regras persistentes, heurísticas determinísticas, destinos e justificativas.

**Aceite:** implementação comprovada, testes executados e registrados, revisão QA, documentação atualizada, autorização humana para prosseguir.

## Sprint 03 — Movimentação segura
**Agente:** Segurança + Filesystem

**Escopo:** Preview, aprovação vinculada ao plano, journal, crash recovery, undo, testes de falha.

**Aceite:** implementação comprovada, testes executados e registrados, revisão QA, documentação atualizada, autorização humana para prosseguir.

## Sprint 04 — Interface nativa
**Agente:** Interface GTK

**Escopo:** Dashboard, sugestões, aprovação explícita, regras, histórico, estados de erro.

**Aceite:** implementação comprovada, testes executados e registrados, revisão QA, documentação atualizada, autorização humana para prosseguir.

## Sprint 05 — Duplicados + monitor
**Agente:** Filesystem + QA

**Escopo:** SHA-256 streaming, Gio.FileMonitor, estabilização e debounce; só sugestões.

**Aceite:** implementação comprovada, testes executados e registrados, revisão QA, documentação atualizada, autorização humana para prosseguir.

## Sprint 06 — Agendamentos
**Agente:** Automação

**Escopo:** Persistência, execução sem sobreposição e apenas dry-run.

**Aceite:** implementação comprovada, testes executados e registrados, revisão QA, documentação atualizada, autorização humana para prosseguir.

## Sprint 07 — QA + pacote Fedora
**Agente:** Segurança + Coordenador

**Escopo:** Regressão, simulações de falha, revisão de segurança, documentação e preparação RPM.

**Aceite:** implementação comprovada, testes executados e registrados, revisão QA, documentação atualizada, autorização humana para prosseguir.

## Template de relatório por sprint
- Sprint e commit/branch revisados:
- Arquivos criados/alterados:
- Comandos executados (sem omitir falhas):
- Testes aprovados e pendentes:
- Riscos residuais e limitações:
- Aprovação humana solicitada: SIM (não presumir aceite)
