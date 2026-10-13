"""Monitor de Downloads via Gio.FileMonitor — Sprint 05.

Escopo ESTRITO Sprint 05 (cf. AGENTS.md + docs/SPRINTS.md):
- SÓ SUGESTÕES. Este módulo NUNCA move, executa, apaga, renomeia ou cria
  pastas. Não contém nenhuma chamada de escrita (`rename`/`unlink`/
  `remove`/`mkdir`/`write`); apenas observa (`lstat`) e registra
  `FileRecord`s na lista `pending`. `dry_run` é sempre `True`: `start()`
  com `dry_run=False` levanta `ValueError` — o monitor nunca executa
  movimentação.
- `Gio` é importado SOMENTE dentro de `start()` (import tardio + guard):
  sem display / sem `gi` / falha ao criar o monitor, registra o modo
  `polling-stub` (observação inativa, API pura continua testável) em vez
  de crashar. Nenhum `import gi` no topo deste módulo.
- Validação de raiz idêntica à do `SafeScanner`: `abspath` lexical +
  contenção `realpath` + `lstat` recusando raiz symlink/não-dir. Escapes
  via `..` ou symlink levantam `PermissionError`.
- Debounce: `dict path -> (last_event_time, count)`. Eventos duplicados
  (rajadas do FileMonitor para o mesmo caminho) são COALESCIDOS:
  `processable(path, now)` retorna `True` no primeiro evento e somente
  após `debounce_s` desde o último evento processado; eventos dentro da
  janela retornam `False` e apenas incrementam o contador. `now` é
  injetável (tempo do chamador), logo testável sem display.
- Estabilidade: `is_stable_candidate(path)` exige arquivo regular
  não-symlink, sem marcador temporário (`*.part`, `*.crdownload`,
  `*.tmp`, `~*`, `.~lock.*`) e `size` igual em `stable_checks` leituras
  `lstat`. Limitação documentada: leituras consecutivas sem espera não
  detectam downloads lentos — por isso o fluxo correto é rechecar a
  estabilidade APÓS a janela de debounce (`processable == True`), e o
  `MoveService` (Sprint 03) deve revalidar imediatamente antes de operar.
- `handle_event(gfile, event_type)` aceita `Gio.File` (via `get_path()`),
  `str` ou `Path` (testável sem display) e apenas REGISTRA a sugestão em
  `pending` (atualizando a entrada existente do mesmo caminho em vez de
  duplicar). `is_stable` do registro reflete `is_stable_candidate` no
  momento do evento (`False -> True` quando estabiliza e um novo evento
  coalescido passa pelo debounce).
- Flag do monitor: `Gio.FileMonitorFlags.WATCH_MOVES` (observa renomeações
  concluídas de downloads). Não há flag NOFOLLOW no `monitor_directory`:
  symlinks são filtrados no handler via `lstat`, e a raiz monitorada nunca
  é um symlink (validada).

Módulo sem escrita em destino; callbacks de usuário nunca podem deflagrar
movimentação a partir daqui (exceções de `callback` são contidas).
"""

from __future__ import annotations

import mimetypes
import os
import stat
import time
from pathlib import Path
from typing import Callable, Sequence

from hexavante.models.entities import FileRecord

__all__ = ["DownloadMonitor"]

_TEMP_SUFFIXES = (".part", ".crdownload", ".tmp")
_TEMP_PREFIXES = ("~", ".~lock.")


def _is_temp_marker(name: str) -> bool:
    """Detecta marcadores de download/temporários pelo nome (cf. scanner)."""
    lowered = name.lower()
    if lowered.endswith(_TEMP_SUFFIXES):
        return True
    return name.startswith(_TEMP_PREFIXES)


def _guess_mime(path: str) -> str:
    """MIME por extensão, com fallback determinístico (stdlib apenas)."""
    mime, _encoding = mimetypes.guess_type(path, strict=False)
    return mime or "application/octet-stream"


def _coerce_event_path(gfile: object) -> str:
    """Extrai o caminho de um evento: `Gio.File` (`get_path()`), `str`/`Path`.

    Retorna `""` quando o caminho não puder ser determinado (evento
    ignorado, nunca crash).
    """
    get_path = getattr(gfile, "get_path", None)
    if callable(get_path):
        try:
            value = get_path()
        except Exception:
            value = None
        if value:
            return os.fspath(value)
        get_parse_name = getattr(gfile, "get_parse_name", None)
        if callable(get_parse_name):
            try:
                parsed = get_parse_name()
            except Exception:
                parsed = None
            if parsed:
                return os.fspath(parsed)
        return ""
    try:
        return os.fspath(gfile)  # type: ignore[arg-type]
    except TypeError:
        return str(gfile)


class DownloadMonitor:
    """Observa um diretório de Downloads. Só sugere; nunca move/executa.

    `authorized_roots` é obrigatório e explícito (sem default): `start()`
    só aceita raízes contidas nele.
    """

    def __init__(
        self,
        authorized_roots: Sequence[Path],
        *,
        debounce_s: float = 1.0,
        stable_checks: int = 2,
        callback: Callable[[FileRecord], None] | None = None,
    ) -> None:
        if not authorized_roots:
            raise ValueError("authorized_roots não pode ser vazio")
        if debounce_s < 0:
            raise ValueError("debounce_s não pode ser negativo")
        if stable_checks < 1:
            raise ValueError("stable_checks deve ser >= 1")
        self._authorized_roots = tuple(
            os.path.abspath(os.fspath(p)) for p in authorized_roots
        )
        self._debounce_s = float(debounce_s)
        self._stable_checks = int(stable_checks)
        self._callback = callback
        # Sugestões registradas (somente leitura do FS as gerou).
        self.pending: list[FileRecord] = []
        # Debounce: caminho absoluto -> (last_event_time, count).
        self._debounce: dict[str, tuple[float, int]] = {}
        self._monitor: object | None = None
        self._handler_id: int | None = None
        self._root: str | None = None
        self._started = False
        # "disconnected" | "gio" | "polling-stub" (headless sem crash).
        self._mode = "disconnected"

    # -- propriedades de inspeção (testes/QA) --------------------------------

    @property
    def started(self) -> bool:
        """Indica se `start()` foi chamado sem `stop()` posterior."""
        return self._started

    @property
    def mode(self) -> str:
        """Modo atual: `disconnected`, `gio` ou `polling-stub`."""
        return self._mode

    @property
    def debounce_s(self) -> float:
        return self._debounce_s

    def debounce_count(self, path: str | Path) -> int:
        """Total de eventos vistos para o caminho (inclui coalescidos)."""
        key = os.path.abspath(os.fspath(path))
        entry = self._debounce.get(key)
        return entry[1] if entry else 0

    # -- autorização (mesma regra do scanner) ---------------------------------

    def _validate_root(self, root: Path) -> str:
        """Valida escopo e retorna a raiz absoluta normalizada."""
        abs_root = os.path.abspath(os.fspath(root))
        lexical_ok = any(
            abs_root == auth or abs_root.startswith(auth + os.sep)
            for auth in self._authorized_roots
        )
        if not lexical_ok:
            raise PermissionError(
                f"raiz fora do escopo autorizado: {os.fspath(root)!r}"
            )
        real_root = os.path.realpath(abs_root)
        real_ok = any(
            real_root == os.path.realpath(auth)
            or real_root.startswith(os.path.realpath(auth) + os.sep)
            for auth in self._authorized_roots
        )
        if not real_ok:
            raise PermissionError(
                f"raiz escapa do escopo autorizado via symlink: {os.fspath(root)!r}"
            )
        st = os.lstat(abs_root)  # FileNotFoundError propaga (erro do chamador).
        if stat.S_ISLNK(st.st_mode):
            raise PermissionError(
                f"raiz é um symlink; escopo deve ser diretório real: {abs_root!r}"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(f"raiz não é um diretório: {abs_root!r}")
        return abs_root

    def _within_scope(self, abs_path: str) -> bool:
        """Checa se um caminho de evento está dentro das raízes autorizadas."""
        lexical_ok = any(
            abs_path == auth or abs_path.startswith(auth + os.sep)
            for auth in self._authorized_roots
        )
        if not lexical_ok:
            return False
        real_path = os.path.realpath(abs_path)
        return any(
            real_path == os.path.realpath(auth)
            or real_path.startswith(os.path.realpath(auth) + os.sep)
            for auth in self._authorized_roots
        )

    # -- ciclo de vida (Gio tardio + guard headless) --------------------------

    def start(self, root: Path, *, dry_run: bool = True) -> None:
        """Inicia observação em dry-run. Nunca move nada.

        Levanta `ValueError` se `dry_run` não for `True` (o monitor nunca
        executa movimentação) e `PermissionError` se a raiz escapar do
        escopo autorizado. Sem `gi` disponível ou falha no backend, cai
        para `polling-stub` sem crashar (a API pura segue testável).
        """
        if dry_run is not True:
            raise ValueError(
                "DownloadMonitor opera apenas em dry-run: "
                "dry_run=False recusado (monitor nunca executa movimentação)"
            )
        abs_root = self._validate_root(root)
        if self._started:
            self.stop()
        try:
            from gi.repository import Gio  # import tardio: nada de gi no topo
        except Exception:
            self._root = abs_root
            self._started = True
            self._mode = "polling-stub"
            return
        try:
            gfile = Gio.File.new_for_path(abs_root)
            flags = Gio.FileMonitorFlags.WATCH_MOVES
            monitor = gfile.monitor_directory(flags, None)
            handler_id = monitor.connect("changed", self._on_changed)
        except Exception:
            # Headless/sem backend (ex.: sem sessão): stub, sem crash.
            self._monitor = None
            self._handler_id = None
            self._root = abs_root
            self._started = True
            self._mode = "polling-stub"
            return
        self._monitor = monitor
        self._handler_id = int(handler_id)
        self._root = abs_root
        self._started = True
        self._mode = "gio"

    def stop(self) -> None:
        """Encerra observação. Nunca crasha; sugestões `pending` persistem."""
        monitor = self._monitor
        handler_id = self._handler_id
        if monitor is not None:
            disconnect = getattr(monitor, "disconnect", None)
            if callable(disconnect) and handler_id is not None:
                try:
                    disconnect(handler_id)
                except Exception:
                    pass
            cancel = getattr(monitor, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    pass
        self._monitor = None
        self._handler_id = None
        self._root = None
        self._started = False
        self._mode = "disconnected"

    # -- debounce puro (tempo injetável, sem display) -------------------------

    def processable(self, path: str | Path, now: float) -> bool:
        """Diz se o evento deve gerar (re)checagem agora, coalescendo rajadas.

        - Primeiro evento de um caminho: `True` (registra `(now, 1)`).
        - Eventos dentro de `debounce_s` do último processado: `False`
          (coalescidos; só incrementam o contador).
        - Eventos após a janela: `True` (atualiza o tempo, incrementa).
        Função pura quanto ao tempo (parâmetro `now`); o relógio real
        (`time.monotonic()`) só é lido em `handle_event`.
        """
        key = os.path.abspath(os.fspath(path))
        entry = self._debounce.get(key)
        if entry is None:
            self._debounce[key] = (now, 1)
            return True
        last, count = entry
        if (now - last) < self._debounce_s:
            self._debounce[key] = (last, count + 1)
            return False
        self._debounce[key] = (now, count + 1)
        return True

    # -- estabilidade (lstat duplo + temporários + tipo) ----------------------

    def is_stable_candidate(self, path: str | Path) -> bool:
        """Checagem síncrona de estabilidade (sem espera, sem display).

        Retorna `True` somente se: nome sem marcador temporário, `lstat`
        sem erro, não-symlink, regular (`S_ISREG`), e `size` idêntico em
        `stable_checks` leituras. Qualquer outra condição (fifo/socket/
        device, sumiço em corrida, crescimento entre leituras) retorna
        `False` — download parcial nunca é sugerido como estável.
        """
        raw = os.fspath(path)
        if _is_temp_marker(os.path.basename(raw)):
            return False
        try:
            first = os.lstat(raw)
        except OSError:
            return False
        if stat.S_ISLNK(first.st_mode):
            return False
        if not stat.S_ISREG(first.st_mode):
            return False
        for _ in range(self._stable_checks - 1):
            try:
                again = os.lstat(raw)
            except OSError:
                return False
            if stat.S_ISLNK(again.st_mode):
                return False
            if not stat.S_ISREG(again.st_mode):
                return False
            if again.st_size != first.st_size:
                return False
        return True

    # -- eventos: só REGISTRAM sugestões --------------------------------------

    def handle_event(
        self, gfile: object, event_type: object = None
    ) -> FileRecord | None:
        """Registra sugestão para um evento. Nunca move/executa.

        `gfile` pode ser `Gio.File`, `str` ou `Path` (testes headless passam
        caminhos diretos; `event_type` é ignorado — qualquer tipo de evento
        passa pelo mesmo funil debounce + estabilidade). Retorna o
        `FileRecord` sugerido ou `None` quando coalescido/ignorado. Entradas
        repetidas do mesmo caminho atualizam `pending` em vez de duplicar.
        """
        raw = _coerce_event_path(gfile)
        if not raw:
            return None
        abs_path = os.path.abspath(raw)
        if not self._within_scope(abs_path):
            return None
        if _is_temp_marker(os.path.basename(abs_path)):
            return None
        now = time.monotonic()
        if not self.processable(abs_path, now):
            return None  # rajada coalescida: contada, sem nova sugestão
        try:
            st = os.lstat(abs_path)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        stable = self.is_stable_candidate(abs_path)
        record = FileRecord(
            path=abs_path,
            size_bytes=st.st_size,
            mtime_ns=st.st_mtime_ns,
            mime_type=_guess_mime(abs_path),
            sha256=None,  # hash SHA-256 sob demanda no detector (Sprint 05)
            is_stable=stable,
        )
        for index, existing in enumerate(self.pending):
            if existing.path == abs_path:
                self.pending[index] = record
                break
        else:
            self.pending.append(record)
        if self._callback is not None:
            try:
                self._callback(record)
            except Exception:
                pass  # callback nunca derruba o loop de eventos
        return record

    def _on_changed(
        self, monitor: object, gfile: object, other: object, event_type: object
    ) -> None:
        """Callback do sinal `changed` do Gio: delega a `handle_event`."""
        try:
            self.handle_event(gfile, event_type)
        except Exception:
            pass  # monitor nunca crasha o loop principal por evento ruim
