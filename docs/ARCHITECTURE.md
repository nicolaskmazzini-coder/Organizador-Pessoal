# Arquitetura — Hexavante File Organizer (Sprints 00–07, v0.1)

## Decisões
- MVC com núcleo puro Python testável sem display: `models/`, `services/` (Protocols), `storage/`, `controllers/` nunca importam `gi`. Todo GTK vive em `views/app.py` com guard headless.
- Entidades frozen (`dataclasses`): `FileRecord, FolderRecord, Rule, ClassificationResult, MovePlan, OperationRecord, Schedule` + `OperationState` (7 estados: planejada, aprovada, em_andamento, verificada, concluída, falhou, recuperação pendente).
- Valores persistidos ASCII (`concluida`, `recuperacao_pendente`); exibição com acentos resolvida na view.
- `MovePlan.plan_hash` SHA-256 puro (`compute_plan_hash`); approval vinculada ao hash, nunca reutilizada após mudança.
- SQLite via `sqlite3`: `connect()` com `foreign_keys=ON`, `journal_mode=WAL` (só arquivo); `migrate()` versionado via `PRAGMA user_version`, transacional por arquivo, idempotente.
- Migração `001_init.sql`: `schema_version, folders, rules, operations_log (CHECK estados), move_items (FK CASCADE), schedules`.
- XDG: produção `~/.local/share/hexavante/organizer.db` (`$XDG_DATA_HOME` respeitado); testes usam `:memory:`/`tmp_path`, nunca criam DB real.
- `main.py`: `--headless --db :memory:` para CI; caminho gráfico delega a `views/app.py:run()`.
- `AppController` fino, `dry_run=True` default, sem MoveService real na 00 (`execute` levanta `RuntimeError` by design).

## Invariantes (cf. AGENTS.md)
- Só `MoveService` escreve em destino; demais camadas retornam dados/planos.
- API separa `preview` / `execute(approval)` / `undo`; `execute` recusa se `approval.plan_hash != plan.plan_hash`.
- Dry-run padrão; nenhuma aprovação implícita/global.
- Sem overwrite silencioso, sem seguir symlinks, sem varrer `/home` por padrão.

## Estrutura
```
src/hexavante/{models/entities,services/{interfaces,scanner},storage/{db,schema,migrations},controllers,views/app,main}
tests/test_{smoke,entities,storage,interfaces_contract,headless,scanner}
```

## Sprint 01 — Scanner (SafeScanner)
- `services/scanner.py`: `SafeScanner(authorized_roots)` + `ScanStats`; `scan()` conforme Protocol (só leitura), `scan_with_stats()` com contadores.
- Autorização: `authorized_roots` obrigatório, sem default; validação lexical `abspath` + contenção `realpath` + `lstat` recusando raiz symlink/não-dir. Nunca `/home` por padrão.
- Travessia: pilha + `os.scandir`, `is_symlink()` primeiro, `stat(follow_symlinks=False)` (lstat); só `S_ISREG` entra; fifo/socket/device → `skipped_special`; `*.part/*.crdownload/*.tmp/~*/.~lock.*` → `skipped_temp`; `PermissionError/FileNotFoundError` em subdir → `skipped_errors`.
- Limites: `max_depth` default 5 (0 = só raiz), `max_files` default 10000 com `truncated=True`.
- MIME via `mimetypes` + fallback `octet-stream`; `sha256=None`, `is_stable=False` (hash/estabilização nas Sprints 05).

## Sprint 02 — Classificação (determinística, sem LLM)
- `services/classifier.py`: `DeterministicClassifier` (ext+MIME→tabela conservadora; 0.9 exato, 0.5 fallback categoria, 0.0 desconhecido; `reason` pt-BR; nunca infere conteúdo) + `DeterministicRuleEngine` (ordem `priority desc, rule_id asc`; glob basename ou substring; disabled ignorada; sem-match→revisão).
- `services/rule_store.py`: `SqliteRuleStore` (upsert `ON CONFLICT`, `ORDER BY priority DESC, rule_id ASC`, valida sem criar pastas).
- Destinos são sugestões dry-run; fora de `allowed_destinations` → `confidence*=0.5` + aviso; `<0.5` ou `None` exige revisão, nunca mover.

## Sprint 03 — Movimentação segura (JournaledMoveService)
- `services/move_service.py`: única camada que escreve; `preview` sem escrita (valida traversal/symlink/colisão/allowed/temporários); `execute` exige approval vinculada ao hash, journal `aprovada→em_andamento→verificada→concluída`/`falhou`; mesmo-fs via `os.link`+unlink (sem overwrite), cross-fs via tmp `O_EXCL`+streaming+fsync+publicação sem overwrite, remoção da origem só após journal+verificação; `undo` cria nova operação `-undo` com verificação; `recover_pending` marca `recuperacao_pendente`.

## Sprint 04 — Interface nativa (GTK4/Libadwaita, headless-testável)
- `views/models.py` (puro, sem gi): SuggestionRow/PlanSummary/HistoryRow/ErrorState + format/validate/filter.
- `views/dashboard.py` (gi tardio, fallback headless): abas Sugestões/Regras/Histórico/Agendamentos; `approve_selected` só cria Approval, nunca executa; `execute_approved` revalida e delega; `build_ui` sem display levanta RuntimeError.
- `controllers`: `list_suggestions/describe_plan/request_approval/history` finos, sem escrita; seleção nunca autoriza sozinha.

## Sprint 05 — Duplicados + monitor (só sugestões)
- `services/duplicates.py`: `StreamingDuplicateDetector` (SHA-256 streaming 64KB, revalida lstat antes/depois, `skipped_changed`/`skipped_large`, grupos ≥2 determinísticos, nunca apaga).
- `services/monitor.py`: `DownloadMonitor` (Gio.FileMonitor `WATCH_MOVES` + debounce com relógio injetável + `is_stable_candidate` via lstat duplo; `dry_run=False` recusado; callback contido; headless cai em `polling-stub`).
- DECISÃO coordenador: invariante gi evoluído — `Gtk`/`Adw` exclusivos de `views/app.py`; `Gio` (I/O, não UI) admitido como import TARDIO dentro de função em `services/monitor.py` com guard; topo continua proibido (teste AST atualizado).

## Sprint 06 — Agendamentos (só dry-run)
- `services/scheduler.py`: `DryRunScheduler` (persistência upsert, enable/disable, `interval:<s>/@hourly/@daily`, relógio injetável, trava `_running` anti-sobreposição, auditoria `verificada`/`falhou`, `recover_after_restart` sem trava fantasma, catch-up único, nunca importa MoveService).

## Sprint 07 — QA + pacote Fedora (v0.1)
- `tests/test_failure_modes.py`: ENOSPC/EACCES simulados, corrida concorrente (FS como árbitro, 1 vencedora), approval antiga pós-restart recusada, crash→recovery sem perda com reexecução recusada por colisão, integração scheduler+scanner real sem mover, spec com campos obrigatórios.
- `packaging/hexavante-file-organizer.spec`: pacote interno (Proprietary, fora do Fedora oficial), `pyproject` macros, `%check` com pytest; console `hexavante` via `[project.scripts]`; build (`rpmbuild`/`mock`) pendente em host Fedora com privilégios.
- Limitações assumidas na v0.1: trava scheduler por instância, integridade cross-fs por tamanho, multi-item não-atômico, estabilidadade por rechecagem, Python validado 3.14 vs alvo 3.10, UI com display pendente.
