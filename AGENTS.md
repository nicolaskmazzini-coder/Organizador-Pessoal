# Hexavante File Organizer — Instruções gerais para agentes

## Objetivo
Aplicativo **desktop nativo Fedora Linux**, offline, sem frontend web, para reconhecer pastas existentes e sugerir a organização segura de arquivos. Repositório: `nicolaskmazzini-coder/Organizador-Pessoal`.

## Stack fixa
- Python 3, PyGObject, GTK 4, Libadwaita.
- SQLite via `sqlite3`; pytest para testes.
- Gio.FileMonitor para eventos de Downloads; SHA-256 para duplicados.
- MVC com `models/`, `views/`, `controllers/`, `services/`, `storage/`, mais `tests/` e `docs/`.
- Nenhum Electron, React, Tauri, servidor HTTP, serviço remoto ou LLM na v0.1.

## Processo de trabalho
1. Leia este arquivo, `docs/SPRINTS.md`, e os documentos existentes em `docs/`.
2. Inspecione o código e o Git antes de alterações; não sobrescreva trabalho alheio.
3. Execute **somente a sprint autorizada**. Planeje interfaces, riscos e arquivos a alterar.
4. Faça alterações pequenas e testes reproduzíveis; não introduza dependências sem necessidade.
5. Documente decisões em `docs/ARCHITECTURE.md`, segurança em `docs/SECURITY.md` e progresso em `docs/SPRINT_STATUS.md`, criando-os na Sprint 00.
6. Entregue relatório com arquivos alterados, comandos de teste e resultados reais. Aguarde aprovação para continuar.

## Política de segurança inegociável
- **Dry-run por padrão**. Nenhum watcher ou agendador executa movimentação real.
- Operações reais exigem confirmação explícita do usuário sobre origem e destino. Nenhuma confirmação implícita ou global.
- Nunca sobrescrever, apagar, renomear silenciosamente, limpar duplicados ou criar pasta não autorizada.
- Nunca executar `sudo`, instalar serviços privilegiados ou alterar arquivos pessoais durante testes.
- Varredura apenas em raízes escolhidas e explicitamente autorizadas; não escanear todo `/home` por padrão.
- Não seguir symlinks; validar escopo, tipo de arquivo, caminhos e alterações concorrentes. Tratar `..`, travessia de caminho, permissões e arquivos especiais.
- Não mover arquivos em download: exigir estabilidade, checar marcadores temporários e revalidar imediatamente antes da operação.
- O serviço de movimentação é a **única** camada autorizada a escrever nos destinos. Views, controllers, scanners e schedulers não escrevem diretamente.
- A API deve separar `preview`, `execute(approval)` e `undo`, com plano e aprovação rastreáveis; nunca reutilizar aprovação após mudança no arquivo/plano.
- Em mesmo filesystem, preferir operações atômicas sem overwrite com proteção a colisão; não presumir que rename comum impede overwrite.
- Entre filesystems: cópia para temporário exclusivo, verificação, sincronização, publicação sem overwrite, registro de estados recuperáveis e remoção da origem **somente após** confirmação de integridade e persistência do estado. Se a segurança não puder ser demonstrada, bloquear a operação.
- Persistir trilha de auditoria e estados parciais; `undo` verifica origem/destino e conflitos antes de reverter, e pode recusar com explicação.
- Todas as operações destrutivas ficam fora dos testes; usar `tmp_path` e simular falhas, interrupções, colisões e concorrência.
- Não fazer commits/push/deploy remotos nem instalar pacotes no host sem autorização explícita.

## Interfaces mínimas (a refinar na Sprint 00)
- `FileRecord`, `FolderRecord`, `Rule`, `ClassificationResult`, `MovePlan`, `OperationRecord`, `Schedule`.
- Serviços: scanner, classifier, rule_engine, move_service, duplicate_detector, monitor, scheduler.
- Preferir núcleo puro Python testável sem display GTK; camada UI delega ao controller.

## Agentes especializados
- `@coordenador`: planejamento, integração e gate de sprint.
- `@arquiteto`: contratos MVC, banco e dependências.
- `@filesystem`: scanner, monitor, hash e suporte à movimentação.
- `@classificacao`: regras e sugestões determinísticas.
- `@interface-gtk`: UI nativa acessível.
- `@automacao`: execução programada apenas em dry-run.
- `@seguranca-qa`: threat model, revisão e testes de falha.

## Condição de encerramento
Uma sprint só é candidata a aprovação com funcionalidade demonstrada, testes executados, documentação, riscos restantes e revisão de segurança. O agente **não** decide sozinho avançar.
