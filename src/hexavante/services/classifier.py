"""Classificação determinística — Sprint 02.

Escopo ESTRITO Sprint 02 (cf. AGENTS.md + docs/SPRINTS.md):
- Regras persistentes, heurísticas determinísticas, destinos e justificativas.
- SEM movimentação real, SEM criar pastas, SEM scanner, SEM monitor, SEM UI.
- SEM LLM, SEM inferência de conteúdo (ex.: PDF=livro, ZIP=código).
  Apenas extensão minúscula + MIME sugerem destino via tabela conservadora.
- Destinos são SUGESTÕES: nunca criar diretório, nunca escrever no filesystem.
- Dry-run é o padrão; baixa confiança (<0.5) significa REVISÃO, nunca mover.

Módulo puro Python: sem `gi`, sem I/O, sem efeitos na importação.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Sequence

from hexavante.models.entities import ClassificationResult, FileRecord, Rule

__all__ = [
    "DeterministicClassifier",
    "DeterministicRuleEngine",
    "EXTENSION_DESTINATIONS",
    "MIME_CATEGORY_FALLBACK",
]


# Tabela embutida conservadora: extensão minúscula -> destino sugerido.
# Apenas sinais de extensão/MIME; NUNCA infere conteúdo/semântica
# (ex.: não distingue "livro" de "documento", nem "código" dentro de ZIP).
EXTENSION_DESTINATIONS: dict[str, str] = {
    # Documentos
    ".pdf": "Documentos",
    ".txt": "Documentos",
    ".md": "Documentos",
    ".rst": "Documentos",
    ".odt": "Documentos",
    ".doc": "Documentos",
    ".docx": "Documentos",
    ".ods": "Documentos",
    ".odp": "Documentos",
    ".csv": "Documentos",
    ".xls": "Documentos",
    ".xlsx": "Documentos",
    ".ppt": "Documentos",
    ".pptx": "Documentos",
    ".epub": "Documentos",
    # Imagens
    ".jpg": "Imagens",
    ".jpeg": "Imagens",
    ".png": "Imagens",
    ".gif": "Imagens",
    ".bmp": "Imagens",
    ".webp": "Imagens",
    ".svg": "Imagens",
    ".tif": "Imagens",
    ".tiff": "Imagens",
    ".heic": "Imagens",
    # Áudio
    ".mp3": "Áudio",
    ".wav": "Áudio",
    ".flac": "Áudio",
    ".ogg": "Áudio",
    ".m4a": "Áudio",
    ".opus": "Áudio",
    ".aac": "Áudio",
    # Vídeos
    ".mp4": "Vídeos",
    ".mkv": "Vídeos",
    ".avi": "Vídeos",
    ".mov": "Vídeos",
    ".webm": "Vídeos",
    ".mpg": "Vídeos",
    ".mpeg": "Vídeos",
    # Arquivos (compactados/arquivos genéricos por extensão explícita)
    ".zip": "Arquivos",
    ".tar": "Arquivos",
    ".gz": "Arquivos",
    ".tgz": "Arquivos",
    ".tar.gz": "Arquivos",
    ".bz2": "Arquivos",
    ".xz": "Arquivos",
    ".7z": "Arquivos",
    ".rar": "Arquivos",
    # Código (apenas por extensão explícita de fonte; nunca por conteúdo)
    ".py": "Código",
    ".js": "Código",
    ".ts": "Código",
    ".rs": "Código",
    ".go": "Código",
    ".java": "Código",
    ".c": "Código",
    ".h": "Código",
    ".hpp": "Código",
    ".cpp": "Código",
    ".cs": "Código",
    ".rb": "Código",
    ".php": "Código",
    ".sh": "Código",
    ".html": "Código",
    ".css": "Código",
    ".json": "Código",
    ".xml": "Código",
    ".yml": "Código",
    ".yaml": "Código",
    ".toml": "Código",
}

# Fallback conservador por categoria MIME (só quando extensão é desconhecida).
MIME_CATEGORY_FALLBACK: dict[str, str] = {
    "image": "Imagens",
    "audio": "Áudio",
    "video": "Vídeos",
    "text": "Documentos",
}

_EXACT_CONFIDENCE = 0.9
_FALLBACK_CONFIDENCE = 0.5
_UNKNOWN_CONFIDENCE = 0.0
_NO_MATCH_REASON = "sem regra correspondente; requer revisão"


def _extension_of(path: str) -> str:
    """Retorna a extensão minúscula com ponto, com suporte a `.tar.gz`.

    Retorna "" quando não há extensão. Nunca toca no filesystem.
    """
    lowered = path.lower()
    # Composta primeiro: "backup.TAR.GZ" -> ".tar.gz".
    if lowered.endswith(".tar.gz"):
        return ".tar.gz"
    _root, ext = os.path.splitext(lowered)
    return ext


def _apply_allowed_penalty(
    destination: str | None,
    confidence: float,
    reason: str,
    allowed_destinations: Sequence[str] | None,
) -> tuple[float, str]:
    """Aplica penalidade quando o destino sugerido não está entre os permitidos.

    Se `allowed_destinations` é None, nada muda (sem restrição).
    Se destino fora da lista, `confidence *= 0.5` e a razão registra
    "destino não autorizado; requer aprovação". Não cria nada no FS.
    """
    if destination is None or allowed_destinations is None:
        return confidence, reason
    allowed = set(allowed_destinations)
    if destination in allowed:
        return confidence, reason
    penalized = confidence * 0.5
    # Garante intervalo válido mesmo após penalidade.
    penalized = max(0.0, min(1.0, penalized))
    reason = reason + " Destino não autorizado; requer aprovação explícita."
    return penalized, reason


class DeterministicClassifier:
    """Sugere destino por extensão minúscula + MIME. Não executa I/O.

    Conformidade com o Protocol `Classifier`
    (`classify(file) -> ClassificationResult`).

    - Match exato em tabela conservadora -> confidence 0.9.
    - Extensão desconhecida + categoria MIME conhecida
      (image/*, audio/*, video/*, text/*) -> confidence 0.5.
    - Desconhecido -> destination None, confidence 0.0, reason explicando.
    - NUNCA infere conteúdo (PDF=livro, ZIP=código) sem indícios:
      só extensão/MIME contam.
    - Destinos são SUGESTÕES: nunca criar diretório, nunca escrever.
    - `allowed_destinations` opcional (construtor e/ou por chamada):
      se destino fora dele, confidence *= 0.5 e reason indica
      "destino não autorizado; requer aprovação".
    - Baixa confiança (<0.5) significa REVISÃO, nunca movimentação.
    """

    def __init__(
        self, allowed_destinations: Sequence[str] | None = None
    ) -> None:
        self._allowed: tuple[str, ...] | None = (
            tuple(allowed_destinations) if allowed_destinations is not None else None
        )

    def classify(
        self,
        file: FileRecord,
        *,
        allowed_destinations: Sequence[str] | None = None,
    ) -> ClassificationResult:
        """Retorna sugestão determinística com justificativa em pt-BR."""
        effective = (
            allowed_destinations if allowed_destinations is not None else self._allowed
        )
        ext = _extension_of(file.path)
        mime = (file.mime_type or "").strip().lower()

        if ext and ext in EXTENSION_DESTINATIONS:
            dest = EXTENSION_DESTINATIONS[ext]
            conf = _EXACT_CONFIDENCE
            rule_id: str | None = f"ext:{ext.lstrip('.')}"
            reason = (
                f"extensão '{ext}' reconhecida "
                f"(MIME '{mime or 'desconhecido'}'); "
                f"destino sugerido '{dest}'."
            )
        else:
            category = mime.split("/")[0].strip() if "/" in mime else ""
            if category in MIME_CATEGORY_FALLBACK:
                dest = MIME_CATEGORY_FALLBACK[category]
                conf = _FALLBACK_CONFIDENCE
                rule_id = f"mime:{category}"
                shown_ext = ext if ext else "(sem extensão)"
                reason = (
                    f"extensão '{shown_ext}' desconhecida; "
                    f"categoria MIME '{category}/*' sugere '{dest}' "
                    f"com confiança parcial; requer revisão."
                )
            else:
                dest = None
                conf = _UNKNOWN_CONFIDENCE
                rule_id = None
                shown_ext = ext if ext else "(sem extensão)"
                reason = (
                    f"extensão '{shown_ext}' e MIME '{mime or 'desconhecido'}' "
                    f"desconhecidos; sem destino sugerido; "
                    f"requer revisão humana. "
                    f"Nenhuma inferência de conteúdo foi feita."
                )

        conf, reason = _apply_allowed_penalty(dest, conf, reason, effective)
        return ClassificationResult(
            file_path=file.path,
            rule_id=rule_id,
            destination=dest,
            confidence=conf,
            reason=reason,
        )


def _rule_matches(file: FileRecord, rule: Rule) -> bool:
    """Testa pattern como glob fnmatch no basename minúsculo OU substring no path.

    - `fnmatch(basename_lower, pattern_lower)` cobre "*.pdf", "foto*".
    - `pattern_lower in path_lower` cobre substring sem wildcards
      (ex.: pattern "relatorio" casa "/tmp/synth/relatorio_2024.pdf").
    - Case-insensitive (tudo em minúsculas). Puro, sem I/O.
    """
    raw = (rule.pattern or "").strip()
    if not raw:
        return False
    pat = raw.lower()
    basename = os.path.basename(file.path).lower()
    full = file.path.lower()
    if fnmatch.fnmatch(basename, pat):
        return True
    return pat in full


class DeterministicRuleEngine:
    """Avalia regras persistentes por prioridade. Apenas retorna dados.

    Conformidade com o Protocol `RuleEngine`
    (`suggest(file, rules)` + `list_rules()`).

    Precedência determinística:
    - Regras ordenadas por (priority desc, rule_id asc).
    - Regras com `enabled=False` são ignoradas.
    - Pattern como glob fnmatch no basename minúsculo OU substring no path.
    - Primeira match vence; empate resolve por maior priority e, em seguida,
      menor rule_id (ordem total, repetível).
    - Sem match -> `ClassificationResult(file_path, None, None, 0.0,
      "sem regra correspondente; requer revisão")`.
    - Match -> confidence 0.9 (0.45 se destino fora de `allowed_destinations`).

    SEGURANÇA — Baixa confiança (<0.5) significa REVISÃO, nunca movimentação:
    nenhum chamador deve mover/renomear/criar pasta a partir de um resultado
    com `destination is None` ou `confidence < 0.5`. Movimentação real exige
    aprovação explícita em outra camada (Sprint 03, fora deste escopo).
    Destinos aqui são SUGESTÕES: nunca criar diretório, nunca escrever.

    `allowed_destinations` opcional (construtor e/ou por chamada): se o
    destino vencedor estiver fora dele, `confidence *= 0.5` e a razão indica
    "destino não autorizado; requer aprovação".
    """

    def __init__(
        self,
        rules: Sequence[Rule] | None = None,
        allowed_destinations: Sequence[str] | None = None,
    ) -> None:
        self._rules: tuple[Rule, ...] = tuple(rules) if rules is not None else ()
        self._allowed: tuple[str, ...] | None = (
            tuple(allowed_destinations) if allowed_destinations is not None else None
        )

    def suggest(
        self,
        file: FileRecord,
        rules: Sequence[Rule],
        *,
        allowed_destinations: Sequence[str] | None = None,
    ) -> ClassificationResult:
        """Sugere destino aplicando regras por prioridade (determinístico)."""
        effective = (
            allowed_destinations if allowed_destinations is not None else self._allowed
        )
        ordered = sorted(rules, key=lambda r: (-r.priority, r.rule_id))
        for rule in ordered:
            if not rule.enabled:
                continue
            if not _rule_matches(file, rule):
                continue
            conf = _EXACT_CONFIDENCE
            reason = (
                f"regra '{rule.name}' (id='{rule.rule_id}', "
                f"pattern='{rule.pattern}', priority={rule.priority}) "
                f"aplicada ao arquivo '{file.path}'; "
                f"destino sugerido '{rule.destination}'."
            )
            conf, reason = _apply_allowed_penalty(
                rule.destination, conf, reason, effective
            )
            return ClassificationResult(
                file_path=file.path,
                rule_id=rule.rule_id,
                destination=rule.destination,
                confidence=conf,
                reason=reason,
            )
        return ClassificationResult(
            file_path=file.path,
            rule_id=None,
            destination=None,
            confidence=_UNKNOWN_CONFIDENCE,
            reason=_NO_MATCH_REASON,
        )

    def list_rules(self) -> Sequence[Rule]:
        """Lista regras internas ordenadas por (priority desc, rule_id asc)."""
        return tuple(sorted(self._rules, key=lambda r: (-r.priority, r.rule_id)))
