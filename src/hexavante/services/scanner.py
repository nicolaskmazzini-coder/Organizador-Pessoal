"""Scanner seguro de inventário — Sprint 01.

Escopo ESTRITO Sprint 01 (cf. AGENTS.md + docs/SPRINTS.md):
- Varredura SOMENTE em raízes explicitamente autorizadas. Nunca `/home`
  por padrão. Fora do escopo: `PermissionError`.
- NUNCA segue symlinks (`os.scandir` + `DirEntry.is_symlink()` primeiro;
  sem `os.walk(followlinks=True)`, sem `Path.resolve()` cego).
- Exclui arquivos especiais (fifos, sockets, devices): só arquivos
  regulares entram no inventário. Erros de permissão viram contadores,
  nunca crash.
- MIME via `mimetypes.guess_type` (stdlib, sem dependência nova).
- Limites `max_depth` / `max_files` com flag `truncated`.
- SEM hash na Sprint 01 (`sha256=None`; streaming SHA-256 na Sprint 05).
- `is_stable=False` por padrão (estabilização real no monitor, Sprint 05).
- Pula marcadores temporários (`*.part`, `*.crdownload`, `*.tmp`,
  `~*`, `.~lock.*`).
- LEITURA APENAS: nunca escreve em destino. O parâmetro `dry_run`
  existe por compatibilidade com o Protocol `Scanner` e é ignorado
  (o scanner não escreve em nenhum modo).

Módulo puro Python: sem `gi`, sem escrita, sem efeitos na importação.
"""

from __future__ import annotations

import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from hexavante.models.entities import FileRecord

__all__ = ["SafeScanner", "ScanStats"]


@dataclass
class ScanStats:
    """Contadores explicáveis de uma varredura (Sprint 01)."""

    scanned: int = 0
    skipped_symlinks: int = 0
    skipped_special: int = 0
    skipped_temp: int = 0
    skipped_errors: int = 0
    truncated: bool = False


_TEMP_SUFFIXES = (".part", ".crdownload", ".tmp")
_TEMP_PREFIXES = ("~", ".~lock.")


def _is_temp_marker(name: str) -> bool:
    """Detecta marcadores de download/temporários pelo nome do arquivo."""
    lowered = name.lower()
    if lowered.endswith(_TEMP_SUFFIXES):
        return True
    # "~*" (backup/temporário) e ".~lock.*" (lock do LibreOffice).
    return name.startswith(_TEMP_PREFIXES)


def _guess_mime(path: str) -> str:
    """MIME por extensão, com fallback determinístico (stdlib apenas)."""
    mime, _encoding = mimetypes.guess_type(path, strict=False)
    return mime or "application/octet-stream"


class SafeScanner:
    """Inventaria raízes autorizadas sem seguir symlinks. Nunca escreve.

    `authorized_roots` é obrigatório e explícito: não há default (em
    particular, nunca `/home`). Conformidade com o Protocol `Scanner`
    (`scan(root, *, dry_run=True)`) mais `scan_with_stats` que expõe
    os contadores sem quebrar o contrato da Sprint 00.
    """

    def __init__(
        self,
        authorized_roots: Sequence[Path],
        *,
        max_depth: int = 5,
        max_files: int = 10000,
    ) -> None:
        if max_depth < 0:
            raise ValueError("max_depth não pode ser negativo")
        if max_files <= 0:
            raise ValueError("max_files deve ser positivo")
        # Normalização LEXICAL (abspath): resolve `..` e relativiza sem
        # tocar no filesystem e sem seguir symlinks.
        self._authorized_roots = tuple(
            os.path.abspath(os.fspath(p)) for p in authorized_roots
        )
        self._max_depth = max_depth
        self._max_files = max_files

    # -- autorização -----------------------------------------------------

    def _validate_root(self, root: Path) -> str:
        """Valida escopo e retorna a raiz absoluta normalizada.

        Levanta `PermissionError` se a raiz estiver fora do conjunto
        autorizado (inclui escape via `..` e escape via symlink, este
        detectado comparando `os.path.realpath`).
        """
        abs_root = os.path.abspath(os.fspath(root))
        lexical_ok = any(
            abs_root == auth or abs_root.startswith(auth + os.sep)
            for auth in self._authorized_roots
        )
        if not lexical_ok:
            raise PermissionError(
                f"raiz fora do escopo autorizado: {os.fspath(root)!r}"
            )
        # Escape via symlink: o caminho REAL também precisa estar contido
        # no caminho REAL de alguma raiz autorizada.
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
        # `os.lstat` == `Path.lstat`: NÃO segue symlink do componente final.
        st = os.lstat(abs_root)  # FileNotFoundError propaga (erro do chamador).
        if stat.S_ISLNK(st.st_mode):
            raise PermissionError(
                f"raiz é um symlink; escopo deve ser diretório real: {abs_root!r}"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(f"raiz não é um diretório: {abs_root!r}")
        return abs_root

    # -- API pública ------------------------------------------------------

    def scan(self, root: Path, *, dry_run: bool = True) -> Sequence[FileRecord]:
        """Retorna registros observados (Protocol `Scanner`). Só leitura."""
        records, _stats = self.scan_with_stats(root, dry_run=dry_run)
        return records

    def scan_with_stats(
        self,
        root: Path,
        *,
        dry_run: bool = True,  # noqa: ARG002 - aceito p/ compat.; scanner nunca escreve
        max_depth: int | None = None,
        max_files: int | None = None,
    ) -> tuple[list[FileRecord], ScanStats]:
        """Varre a raiz e retorna `(registros, estatísticas)`.

        `max_depth`/`max_files` por chamada sobrescrevem os defaults do
        construtor. `max_depth=0` lista só arquivos no nível da raiz;
        arquivos a `k` níveis de subdirs exigem `max_depth >= k`.
        """
        depth_limit = self._max_depth if max_depth is None else max_depth
        file_limit = self._max_files if max_files is None else max_files
        if depth_limit < 0:
            raise ValueError("max_depth não pode ser negativo")
        if file_limit <= 0:
            raise ValueError("max_files deve ser positivo")

        abs_root = self._validate_root(root)
        stats = ScanStats()
        records: list[FileRecord] = []

        # Pilha explícita (dirpath, depth). Sem recursão, sem os.walk,
        # sem followlinks: symlink nunca entra na pilha.
        stack: list[tuple[str, int]] = [(abs_root, 0)]
        while stack:
            if len(records) >= file_limit:
                stats.truncated = True
                break
            dirpath, depth = stack.pop()
            try:
                it = os.scandir(dirpath)
            except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
                # Dir protegido/sem permissão ou removido em corrida: conta, não crash.
                stats.skipped_errors += 1
                continue
            with it:
                for entry in it:
                    if len(records) >= file_limit:
                        stats.truncated = True
                        break
                    # 1) Symlink SEMPRE primeiro (arquivo ou dir): pula e conta.
                    try:
                        if entry.is_symlink():
                            stats.skipped_symlinks += 1
                            continue
                    except OSError:
                        # Entrada sumiu / lstat falhou em corrida: conta, não crash.
                        stats.skipped_errors += 1
                        continue
                    # 2) Marcadores temporários: pula e conta (não desce se for dir).
                    if _is_temp_marker(entry.name):
                        stats.skipped_temp += 1
                        continue
                    # 3) lstat (follow_symlinks=False): NÃO segue links.
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
                        stats.skipped_errors += 1
                        continue
                    mode = st.st_mode
                    if stat.S_ISDIR(mode):
                        # Dir real (não-symlink, já filtrado acima). Poda além do limite.
                        if depth < depth_limit:
                            stack.append((entry.path, depth + 1))
                        continue
                    if not stat.S_ISREG(mode):
                        # fifo, socket, device, etc.: fora do inventário.
                        stats.skipped_special += 1
                        continue
                    # 4) Arquivo regular: inventaria (sem hash na Sprint 01).
                    if len(records) >= file_limit:
                        stats.truncated = True
                        break
                    records.append(
                        FileRecord(
                            path=entry.path,
                            size_bytes=st.st_size,
                            mtime_ns=st.st_mtime_ns,
                            mime_type=_guess_mime(entry.path),
                            sha256=None,  # Sprint 05: SHA-256 streaming.
                            is_stable=False,  # Sprint 05: estabilização no monitor.
                        )
                    )
        stats.scanned = len(records)
        return records, stats
