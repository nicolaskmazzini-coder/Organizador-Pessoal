"""Detector de duplicados por SHA-256 em streaming — Sprint 05.

Escopo ESTRITO Sprint 05 (cf. AGENTS.md + docs/SPRINTS.md):
- APENAS SUGESTÕES. Nunca apaga, limpa, renomeia, move ou sobrescreve.
  Este módulo não contém nenhuma chamada de escrita (`remove`/`unlink`/
  `rename`/`rmdir`/`mkdir`/`write` em modo escrita); só leitura (`lstat`,
  `open(..., "rb")`).
- Agrupa por `(size_bytes, sha256)`. Entradas com `sha256=None` têm o
  hash calculado em streaming (chunks de 64KB) SOMENTE se forem arquivos
  regulares não-symlink dentro de `authorized_roots` (quando raízes são
  informadas).
- Se o arquivo sumir ou mudar durante o hash (`size`/`mtime` via `lstat`
  antes e depois diferem) ele é EXCLUÍDO do grupo: mudança detectada, sem
  falso positivo (TOCTOU nunca gera grupo espúrio).
- Arquivos maiores que `max_read_bytes` (default 64MB) são PULADOS com
  motivo registrado em `skipped_large` — decisão consciente: NÃO usar
  "size + primeiros N bytes" como hash parcial, pois prefixos iguais em
  arquivos diferentes gerariam falsos positivos. Pular é seguro; parcial
  não é.
- `base_dir` opcional resolve `FileRecord.path` relativos sem tocar em
  diretórios pessoais reais (testes usam `tmp_path`).

Módulo puro Python: sem `gi`, sem I/O de escrita, sem efeitos na importação.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
from pathlib import Path
from typing import Sequence

from hexavante.models.entities import FileRecord

__all__ = ["StreamingDuplicateDetector", "hash_file_streaming"]

_CHUNK_SIZE = 64 * 1024  # 64KB por leitura (streaming, sem carregar o arquivo).
_DEFAULT_MAX_READ_BYTES = 64 * 1024 * 1024  # 64MB por arquivo (anti-DoS).


def hash_file_streaming(
    path: str | Path,
    *,
    chunk_size: int = _CHUNK_SIZE,
    max_read_bytes: int | None = None,
) -> str:
    """Calcula o SHA-256 de um arquivo em streaming (`open(rb)`, chunks).

    - `chunk_size` deve ser positivo.
    - `max_read_bytes=None` (default) lê o arquivo inteiro; se informado e
      o arquivo exceder o limite, levanta `ValueError` (chamador registra
      `skipped_large` e pula — nunca hash parcial).
    - Levanta `FileNotFoundError`/`PermissionError`/`OSError` se a leitura
      falhar (chamador conta como ausente/erro, nunca crasha o lote).
    - Não segue lógica própria de symlink: o chamador valida com `lstat`
      antes/depois (TOCTOU). `open()` em symlink seria seguir o alvo, por
      isso `find_duplicates` recusa symlinks ANTES de chamar esta função.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size deve ser positivo")
    if max_read_bytes is not None and max_read_bytes <= 0:
        raise ValueError("max_read_bytes deve ser positivo")
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if max_read_bytes is not None and total > max_read_bytes:
                raise ValueError(
                    f"arquivo excede max_read_bytes={max_read_bytes}: {os.fspath(path)!r}"
                )
            digest.update(chunk)
    return digest.hexdigest()


class StreamingDuplicateDetector:
    """Agrupa `FileRecord`s com mesmo `(size_bytes, sha256)`. Só sugere.

    - Conformidade estrutural com o Protocol `DuplicateDetector`
      (`find_duplicates(files)`).
    - `authorized_roots`: sequência explícita de raízes (mesma semântica do
      `SafeScanner`). Se vazia/`None`, nenhuma checagem de escopo é feita
      (modo puro para agrupar registros já com `sha256` sem tocar no FS).
      Se informada, entradas com `sha256=None` fora do escopo são puladas
      em `skipped_out_of_scope` (barram `..` e escape via symlink pela
      comparação `abspath` + `realpath`).
    - `base_dir` opcional: base para resolver `FileRecord.path` relativos.
      Caminhos absolutos ignoram `base_dir`.
    - Listas de motivos (`skipped_large`, `skipped_changed`,
      `skipped_missing`, `skipped_symlink`, `skipped_special`,
      `skipped_out_of_scope`) são resetadas a cada chamada de
      `find_duplicates` e expõem o porquê de cada exclusão (explicável).
    """

    def __init__(
        self,
        authorized_roots: Sequence[Path] | None = (),
        *,
        max_read_bytes: int = _DEFAULT_MAX_READ_BYTES,
        chunk_size: int = _CHUNK_SIZE,
        base_dir: Path | None = None,
    ) -> None:
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes deve ser positivo")
        if chunk_size <= 0:
            raise ValueError("chunk_size deve ser positivo")
        self._authorized_roots = tuple(
            os.path.abspath(os.fspath(p)) for p in (authorized_roots or ())
        )
        self._max_read_bytes = max_read_bytes
        self._chunk_size = chunk_size
        self._base_dir = (
            os.path.abspath(os.fspath(base_dir)) if base_dir is not None else None
        )
        self.skipped_large: list[str] = []
        self.skipped_changed: list[str] = []
        self.skipped_missing: list[str] = []
        self.skipped_symlink: list[str] = []
        self.skipped_special: list[str] = []
        self.skipped_out_of_scope: list[str] = []

    # -- resolução e escopo -------------------------------------------------

    def _resolve(self, record_path: str) -> str:
        """Resolve o caminho do registro para absoluto sem seguir symlinks."""
        if os.path.isabs(record_path):
            return os.path.abspath(record_path)
        if self._base_dir is not None:
            return os.path.abspath(os.path.join(self._base_dir, record_path))
        # Sem base: normalização lexical contra o cwd; fora de escopo será
        # recusado quando `authorized_roots` estiver configurado.
        return os.path.abspath(record_path)

    def _within_scope(self, abs_path: str) -> bool:
        """Checa contenção lexical (`abspath`) + real (`realpath`)."""
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

    # -- API pública --------------------------------------------------------

    def find_duplicates(
        self, files: Sequence[FileRecord]
    ) -> Sequence[Sequence[FileRecord]]:
        """Agrupa arquivos com mesmo hash. Sem efeitos destrutivos.

        - Registros com `sha256` informado são agrupados de forma pura, sem
          tocar no filesystem (nenhum I/O): útil para inventários já
          hasheados e para testes sintéticos.
        - Registros com `sha256=None` exigem validação no disco (`lstat` +
          escopo + regular + não-symlink + limite de tamanho) e hashing em
          streaming; qualquer divergência `lstat` antes/depois exclui o
          arquivo (`skipped_changed`).
        - Retorna SOMENTE grupos com >= 2 membros, ordenados
          deterministicamente por `(size, sha)` com membros ordenados por
          `path` (chamadas repetidas sobre a mesma entrada retornam a mesma
          estrutura).
        """
        self.skipped_large = []
        self.skipped_changed = []
        self.skipped_missing = []
        self.skipped_symlink = []
        self.skipped_special = []
        self.skipped_out_of_scope = []

        groups: dict[tuple[int, str], list[FileRecord]] = {}
        for record in files:
            # Caminho 1: hash já conhecido — agrupamento puro, sem I/O.
            if record.sha256 is not None:
                key = (record.size_bytes, record.sha256)
                groups.setdefault(key, []).append(record)
                continue
            # Caminho 2: precisa hashear — validação total no disco.
            abs_path = self._resolve(record.path)
            if self._authorized_roots and not self._within_scope(abs_path):
                self.skipped_out_of_scope.append(record.path)
                continue
            try:
                before = os.lstat(abs_path)
            except OSError:
                self.skipped_missing.append(record.path)
                continue
            if stat.S_ISLNK(before.st_mode):
                # NUNCA seguir symlink: `open()` seguiria o alvo.
                self.skipped_symlink.append(record.path)
                continue
            if not stat.S_ISREG(before.st_mode):
                self.skipped_special.append(record.path)
                continue
            if before.st_size > self._max_read_bytes:
                self.skipped_large.append(record.path)
                continue
            try:
                digest = hash_file_streaming(
                    abs_path,
                    chunk_size=self._chunk_size,
                    max_read_bytes=self._max_read_bytes,
                )
            except ValueError:
                # Cresceu além do limite durante a leitura: trata como grande.
                self.skipped_large.append(record.path)
                continue
            except OSError:
                self.skipped_missing.append(record.path)
                continue
            try:
                after = os.lstat(abs_path)
            except OSError:
                # Sumiu durante o hash: mudança detectada, sem falso positivo.
                self.skipped_changed.append(record.path)
                continue
            if (
                after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                self.skipped_changed.append(record.path)
                continue
            enriched = dataclasses.replace(
                record, sha256=digest, size_bytes=before.st_size
            )
            groups.setdefault((before.st_size, digest), []).append(enriched)

        ordered: list[list[FileRecord]] = [
            sorted(members, key=lambda r: r.path)
            for key, members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1]))
            if len(members) >= 2
        ]
        return ordered
