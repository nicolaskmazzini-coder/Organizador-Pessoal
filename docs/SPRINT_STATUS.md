# Sprint status — Hexavante File Organizer

## Sprint 00 — Fundação — APROVADA PELO HUMANO (base para 01)
- Escopo: MVC, GTK4/Libadwaita, SQLite, migrações, testes headless, documentação. Sem I/O pessoal.
- Agentes: @arquiteto (implementação) + @coordenador (integração/gate). Revisão: @seguranca-qa (APROVAR).

### Arquivos criados
- `pyproject.toml`, `.gitignore`, `opencode.json`
- `src/hexavante/{__init__,models/entities,services/interfaces,storage/{db,schema,migrations/001_init.sql},controllers,views/app,main}`
- `tests/test_{smoke,entities,storage,interfaces_contract,headless}` (22 testes)
- `docs/{ARCHITECTURE,SECURITY,SPRINT_STATUS}.md`

### Comandos e resultados reais
- `/tmp/opencode/venv-sprint00/bin/pytest -v` → `22 passed in 0.04s` (após correção de 1 falso positivo em `test_apenas_app_py_importa_gi`)
- `PYTHONPATH=src python3 -m hexavante.main --headless --db :memory:` → `hexavante headless ok (schema v1, dry_run=True)` EXIT 0
- `grep -rn ^import gi src tests` → só `src/hexavante/views/app.py:14,18` (isolamento provado)
- `git init` executado (sem commit, conforme regra); pytest instalado em venv isolado `/tmp/opencode/venv-sprint00` (não no host, sem sudo)

### Aceite
- [x] Estrutura src/tests criada
- [x] pytest verde sem display
- [x] Migração v1 aplica/idempotente/rollback provados
- [x] Núcleo importa sem `gi`
- [x] Docs criadas
- [x] Parecer seguranca-qa: APROVAR como candidata (sem autorizar Sprint 01)
- [ ] Aprovação humana explícita: PENDENTE

### Revisão seguranca-qa
- Veredito: **APROVAR como candidata** (sem autorizar Sprint 01). 22 passed confirmados pelo QA em venv isolado.
- Achados não-bloqueantes corrigidos pelo coordenador: tautologia `or True` em `test_storage.py:38`, vazamento de conexão em `main.py:37-45` (agora `try/finally: close`).
- Pendentes p/ 01/03: validação `..`/escopo em `preview`, enforcement symlink real, log de falha WAL, teste em Python 3.10 real, UI com display.
- pytest ausente no host (usado venv isolado); Python 3.14 vs alvo 3.10 não validado em 3.10 real; WAL em NFS não testado; UI GTK real só validável com display (Sprint 04).
- Aprovação humana solicitada: SIM (não presumir aceite)

## Sprint 01 — Scanner — APROVADA PELO HUMANO
- Escopo: raízes autorizadas, MIME, inventário, exclusão de symlinks/especiais e limites. Sem movimentação.
- Agentes: @filesystem (implementação) + @coordenador (integração/gate). Revisão: @seguranca-qa (APROVAR).
- Escopo: raízes autorizadas, MIME, inventário, exclusão de symlinks/especiais e limites. Sem movimentação.
- Agentes: @filesystem (implementação) + @coordenador (integração/gate). Revisão: @seguranca-qa.

### Arquivos criados/alterados
- CRIADOS: `src/hexavante/services/scanner.py` (SafeScanner + ScanStats, 232 linhas), `tests/test_scanner.py` (17 testes, 328 linhas)
- ALTERADOS: `docs/{ARCHITECTURE,SECURITY,SPRINT_STATUS}.md`
- NÃO tocados: `entities.py, interfaces.py, db.py, main.py, views/` (contratos 00 preservados)

### Comandos e resultados reais
- `/tmp/opencode/venv-sprint00/bin/pytest -v` → `39 passed in 0.06s` (22 da 00 + 17 da 01), EXIT 0 — confirmado pelo coordenador
- `grep -E followlinks|follow_symlinks=True|shutil|rename|unlink|mkdir src/.../scanner.py` → só comentários/docstrings, nenhuma escrita
- `git status --short` → repo ainda sem commits (regra), só untracked

### Aceite
- [x] Raízes autorizadas obrigatórias, escape `..`/symlink barrado
- [x] MIME sem dependência nova, inventário só regulares
- [x] Symlinks/especiais/temporários excluídos com contadores
- [x] Limites max_depth/max_files com truncated
- [x] 17 testes tmp_path verdes + regressão 00 verde
- [x] Parecer seguranca-qa: APROVAR como candidata (sem autorizar Sprint 02; 39 passed reconfirmados)
- [x] Aprovação humana explícita: SIM (2026-10-12)

## Sprint 02 — Classificação — APROVADA PELO HUMANO
- Escopo: regras persistentes, heurísticas determinísticas, destinos e justificativas. Sem movimentação, sem criar pastas.
- Agentes: @classificacao (implementação) + @coordenador (integração/gate). Revisão: @seguranca-qa (APROVAR).

### Arquivos criados/alterados
- CRIADOS: `src/hexavante/services/classifier.py` (DeterministicClassifier + DeterministicRuleEngine), `src/hexavante/services/rule_store.py` (SqliteRuleStore), `tests/test_classification.py` (27 testes)
- ALTERADOS: `docs/{ARCHITECTURE,SECURITY,SPRINT_STATUS}.md`
- NÃO tocados: `entities.py, interfaces.py, scanner.py, db.py, main.py, views/`

### Comandos e resultados reais
- `/tmp/opencode/venv-sprint00/bin/pytest -v` → `66 passed in 0.08s` (39 anteriores + 27 novos), EXIT 0 — confirmado pelo coordenador
- `grep mkdir|shutil|rename|open( classifier.py rule_store.py` → só comentário, nenhuma escrita; sem `import gi`

### Aceite
- [x] Heurísticas determinísticas sem LLM, sem inferência de conteúdo
- [x] Precedência priority/rule_id + disabled + glob + sem-match→revisão
- [x] Regras persistentes SQLite com upsert e ordenação
- [x] Destinos só sugestões, baixa confiança exige revisão
- [x] 27 testes sintéticos verdes + regressão verde
- [x] Parecer seguranca-qa: APROVAR como candidata (sem autorizar Sprint 03; 66 passed reconfirmados)
- [x] Aprovação humana explícita: SIM

## Sprint 03 — Movimentação segura — APROVADA PELO HUMANO
- Escopo: preview, aprovação vinculada ao plano, journal, crash recovery, undo, testes de falha.
- Agentes: @filesystem + @coordenador. Revisão: @seguranca-qa (APROVAR).

## Sprint 04 — Interface nativa — APROVADA PELO HUMANO
- Escopo: dashboard, sugestões, aprovação explícita, regras, histórico, estados de erro.
- Agentes: @interface-gtk + @coordenador. Revisão: @seguranca-qa (APROVAR).

## Sprint 05 — Duplicados + monitor — APROVADA PELO HUMANO
- Escopo: SHA-256 streaming, estabilização, debounce; só sugestões.
- Agentes: @filesystem + @coordenador. Revisão: @seguranca-qa (APROVAR).

## Sprint 06 — Agendamentos — APROVADA PELO HUMANO
- Escopo: persistência, execução sem sobreposição e apenas dry-run.
- Agentes: @automacao + @coordenador. Revisão: @seguranca-qa (APROVAR).

## Sprint 07 — QA + pacote Fedora — CANDIDATA A APROVAÇÃO (aguardando humano + QA final)
- Escopo: regressão, simulações de falha, revisão de segurança, documentação e preparação RPM.
- Agentes: @seguranca-qa + @coordenador.

### Arquivos criados/alterados
- CRIADOS: `tests/test_failure_modes.py` (7 testes: ENOSPC/EACCES/corrida/restart/crash-recovery/integração scheduler+scanner/spec), `packaging/hexavante-file-organizer.spec`
- ALTERADO: `pyproject.toml` (`[project.scripts]` console `hexavante`), `docs/{ARCHITECTURE,SECURITY,SPRINT_STATUS}.md`

### Comandos e resultados reais
- `/tmp/opencode/venv-sprint00/bin/pytest -v` → `190 passed in 0.24s` (183 + 7), EXIT 0
- Intermediário real: 3 falhas nos testes novos (ENOSPC em ramo errado, sqlite cross-thread, reexecução pós-crash) → todas corrigidas com comportamento seguro provado
- `which rpmbuild` → ausente (só `/usr/bin/rpm`); spec NÃO construído aqui (sem sudo/instalação); validação via teste de campos + revisão

### Aceite
- [x] Regressão total verde (190)
- [x] Falhas físicas simuladas sem perda de dados
- [x] Revisão de segurança final: APROVAR v0.1 (seguranca-qa, sem alterar arquivos; 1 observação menor corrigida: prefixo `.hexavante-tmp-*` no teste ENOSPC)
- [x] Docs finalizadas; spec interno pronto, build pendente em host Fedora
- [x] Parecer seguranca-qa: APROVAR v0.1 como candidata
- [ ] Aprovação humana explícita (v0.1): PENDENTE

## v0.1 — Sprints 00–07 completas (aguardando aceite humano final)
- 190 testes verdes; 0 bloqueantes; riscos residuais documentados em ARCHITECTURE/SECURITY.
- Para build Fedora: host com `rpm-build`, `rpmbuild -ba packaging/hexavante-file-organizer.spec` (não executado aqui: sem rpmbuild/sudo).
- Aprovação humana solicitada: SIM (não presumir aceite)
- Escopo: persistência, execução sem sobreposição e apenas dry-run.
- Agentes: @automacao (implementação) + @coordenador (gate). Revisão: @seguranca-qa.

### Arquivos criados/alterados
- CRIADOS: `src/hexavante/services/scheduler.py` (DryRunScheduler), `tests/test_scheduler.py` (24 funções/34 casos)
- ALTERADOS: `docs/{ARCHITECTURE,SECURITY,SPRINT_STATUS}.md`

### Comandos e resultados reais
- `/tmp/opencode/venv-sprint00/bin/pytest -v` → `183 passed in 0.22s` (149 + 34), EXIT 0 — confirmado pelo coordenador
- `grep move_service|MoveService|.execute( scheduler.py` → só `conn.execute` (SQL); sem `import gi`

### Aceite
- [x] Persistência upsert/enable/disable, relógio injetável
- [x] Sem sobreposição (trava por instância, skip auditado)
- [x] dry-run False recusado; nunca chama execute de movimentação
- [x] Erros auditados sem crash; restart sem trava fantasma
- [x] 34 casos verdes + regressão verde
- [x] Parecer seguranca-qa: APROVAR como candidata (sem autorizar Sprint 07; 183 passed)
- [x] Aprovação humana explícita: SIM
- Escopo: SHA-256 streaming, Gio.FileMonitor, estabilização e debounce; só sugestões.
- Agentes: @filesystem (implementação) + @coordenador (gate/arbitragem). Revisão: @seguranca-qa.

### Arquivos criados/alterados
- CRIADOS: `src/hexavante/services/duplicates.py`, `src/hexavante/services/monitor.py`, `tests/test_duplicates.py` (13), `tests/test_monitor.py` (15+4 param)
- AJUSTE coordenador: `tests/test_headless.py` (invariante gi evoluído: Gtk/Adw só app.py; Gio tardio só monitor.py)
- ALTERADOS: `docs/{ARCHITECTURE,SECURITY,SPRINT_STATUS}.md`

### Comandos e resultados reais
- `/tmp/opencode/venv-sprint00/bin/pytest -v` → `149 passed in 0.19s` (117 + 32), EXIT 0 — confirmado pelo coordenador
- Falha intermediária real: `test_apenas_app_py_importa_gi` (invariante 00 vs Gio tardio na 05) → arbitrada e corrigida, sem evasão via importlib

### Aceite
- [x] SHA-256 streaming com revalidação, sem falso positivo, nunca apaga
- [x] FileMonitor com debounce injetável, estabilidade, só sugere
- [x] Isolamento runtime gi intacto (nada na importação)
- [x] 32 testes novos verdes + regressão verde
- [x] Parecer seguranca-qa: APROVAR como candidata (sem autorizar Sprint 06; 149 passed)
- [x] Aprovação humana explícita: SIM
- Escopo: dashboard, sugestões, aprovação explícita, regras, histórico, estados de erro.
- Agentes: @interface-gtk (implementação) + @coordenador (gate). Revisão: @seguranca-qa.

### Arquivos criados/alterados
- CRIADOS: `src/hexavante/views/models.py` (puro), `src/hexavante/views/dashboard.py` (gi tardio), `tests/test_views_logic.py` (27 testes headless)
- ESTENDIDO: `src/hexavante/controllers/__init__.py` (list/describe/request_approval/history, sem escrita)
- ALTERADOS: `docs/{ARCHITECTURE,SECURITY,SPRINT_STATUS}.md`

### Comandos e resultados reais
- `/tmp/opencode/venv-sprint00/bin/pytest -v` → `117 passed in 0.15s` (90 + 27), EXIT 0 — confirmado pelo coordenador
- `grep ^import gi src/` → só `views/app.py:14,18`; views sem escrita direta

### Aceite
- [x] Lógica de apresentação pura e testável sem display
- [x] Seleção nunca autoriza sozinha; aprovação exige seleções + hash
- [x] Nenhum clique de navegação escreve; plan_hash visível no lote
- [x] Histórico via SELECT, estados vazios/erro compreensíveis
- [x] 27 testes headless verdes + regressão verde
- [x] Parecer seguranca-qa: APROVAR como candidata (sem autorizar Sprint 05; 117 passed)
- [x] Aprovação humana explícita: SIM
- Escopo: preview, aprovação vinculada ao plano, journal, crash recovery, undo, testes de falha.
- Agentes: @filesystem (implementação) + @coordenador (gate). Revisão: @seguranca-qa.

### Arquivos criados/alterados
- CRIADOS: `src/hexavante/services/move_service.py` (JournaledMoveService + recover_pending), `tests/test_move_service.py` (24 testes tmp_path)
- ALTERADOS: `docs/{ARCHITECTURE,SECURITY,SPRINT_STATUS}.md`

### Comandos e resultados reais
- `/tmp/opencode/venv-sprint00/bin/pytest -v` → `90 passed in 0.12s` (66 + 24), EXIT 0 — confirmado pelo coordenador
- `grep os.replace|os.rename|shutil.move|O_TRUNC|mkdir move_service.py` → só docstring, nenhum uso; sem `import gi`

### Aceite
- [x] preview sem escrita, valida escopo/symlink/colisão/temporários
- [x] execute exige approval vinculada, recusa divergência com journal falhou
- [x] journal em operations_log/move_items, crash recovery via recover_pending
- [x] undo com verificação, recusa com explicação
- [x] 24 testes de falha verdes + regressão verde
- [x] Parecer seguranca-qa: APROVAR como candidata (sem autorizar Sprint 04; 90 passed + 12 sondas adversárias recusadas)
- [x] Aprovação humana explícita: SIM
