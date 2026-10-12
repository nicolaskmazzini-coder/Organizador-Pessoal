# Segurança — Threat model (Sprints 00–07, v0.1)

## Sprint 07 — veredito final
- Regressão 190 passed; falhas físicas simuladas sem perda (ENOSPC/EACCES/crash/concorrência); approval nunca reutilizável (pós-restart inclusive); pacote sem privilégios no %install.
- Riscos aceitos na v0.1: TOCTOU residual estreito (EEXIST impede overwrite), cross-process paralelo, integridade por tamanho, scripts-fonte 0.9, callback silencioso do monitor, run_log volátil.

## Sprint 06 — agendador incapaz de mover
- `dry_run=False` recusado; sem import de MoveService (por construção); overlap recusado; erros auditados sem propagar crash; restart sem trava fantasma.

## Sprint 05 — duplicados/monitor sem destruição
- Hash com revalidação (sem falso positivo); arquivos grandes pulados; duplicados nunca apagados; monitor só sugere com debounce+estabilidade; Gio tardio com stub headless.

## Sprint 04 — UI sem autorização implícita
- Seleção/navegação nunca escreve; aprovação por lote exige seleções + plan_hash visível; sem aprovação global; estados vazios/erro compreensíveis.

## Sprint 03 — movimentação com journal
- Approval vinculada ao hash, nunca reutilizada; colisão aborta sem overwrite; crash deixa `em_andamento` recuperável; `undo` verifica e pode recusar; integridade cross-fs por tamanho (SHA-256 na 05); multi-item não-atômico por decisão segura.

## Sprint 02 — controles da classificação
- Sem inferência de conteúdo; executáveis propositadamente 0.0; destinos nunca criados; `allowed_destinations` penaliza; baixa confiança = revisão.

## Sprint 01 — controles implementados no SafeScanner
- Autorização: `authorized_roots` obrigatório; `abspath` + contenção `realpath` + `lstat` barram `..` e symlink no caminho; raiz symlink/não-dir recusada.
- Symlinks: `is_symlink()` primeiro, nunca `followlinks`; contados em `skipped_symlinks`. Corrida entre validação e `scandir` documentada (mitigação `openat2`/fd relativo fora da 01).
- Especiais/temporários: fifo/socket/device → `skipped_special`; `*.part/*.crdownload/*.tmp/~*/.~lock.*` → `skipped_temp`; `PermissionError/FileNotFoundError` em subdir → `skipped_errors` sem crash.
- Limites anti-DoS: `max_depth`/`max_files` com `truncated`.
- Sem escrita: scanner só lê via `lstat`; `sha256=None`, `is_stable=False` (hash/estabilização na 05).

| Ameaça | Controle na 00 | Controle na 01 | Sprint de mitigação completa |
|---|---|---|
| Path traversal (`..`, `/`) | `FileRecord/FolderRecord` validam path não-vazio; `MoveService.preview` deve validar escopo | 01, 03 |
| Symlink escape | Contrato: nunca seguir symlinks; teste de isolamento garante núcleo sem I/O arbitrário | 01 |
| TOCTOU (arquivo muda entre preview e execute) | `Approval.plan_hash` vinculada; `execute` recusa em divergência | 03 |
| Overwrite/colisão | Exigido: operações atômicas sem overwrite + proteção a colisão; `undo` verifica antes de reverter | 03 |
| Cross-device (rename inválido) | Exigido: cópia→temporário→verificação→publicação sem overwrite→remoção só após integridade | 03 |
| Arquivo em download (instável) | Exigido: estabilidade + marcadores temporários + revalidação pré-operação | 01, 03 |
| Falta de espaço / crash entre estados | `operations_log` + `OperationState` (7 estados) + journal persistido; `migrate` transacional | 03 |
| Permissão / arquivos especiais | Scanner deve excluir especiais; testes só `tmp_path` | 01 |
| Aprovação reutilizada | `Approval{plan_id,plan_hash}`; nunca reutilizar após mudança | 00 (contrato + teste) |
| Escrita por camada errada | Só `MoveService` escreve; views/controllers/scanners não escrevem | 00 (contrato + teste) |

Dry-run é o padrão. Nenhum watcher/agendador executa movimentação real. Testes destrutivos proibidos fora de `tmp_path`.
