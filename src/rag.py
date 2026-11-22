"""Motor RAG: ingesta de documentos locales + búsqueda semántica.

Encapsula la conexión a una base vectorial Qdrant (en local, persistente
on-disk) y la generación de embeddings con `fastembed`. Se usa en dos fases:

  1. Ingesta  -> `ingest_directory()` / `ingest_file()`: parte cada documento
     en fragmentos (*chunks*), los vectoriza y los indexa en Qdrant.
  2. Consulta -> `query()`: dada una pregunta, recupera los fragmentos más
     relevantes de TODO el corpus por significado (no por palabra clave).

La ingesta es **idempotente**: cada fragmento tiene un id determinista
(`uuid5(fuente#n)`) y guarda el SHA-256 del fichero del que sale. Volver a
ingerir un documento sin cambios no hace nada; si el fichero cambió, se borran
sus fragmentos antiguos y se reindexa; si desapareció de `knowledge_base/`,
`ingest_directory()` lo quita del índice.

El modelo de embedding por defecto es multilingüe (español + inglés) para
cubrir tanto papers técnicos como normativas clínicas.

Usa la API de inferencia local de qdrant-client (`models.Document`), que
vectoriza el texto dentro del cliente. Qdrant en modo `path=` bloquea el
directorio para un único proceso a la vez; para concurrencia real se migraría
a Qdrant en servidor (Docker) sin cambiar esta interfaz.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path

from qdrant_client import QdrantClient, models

from . import config, tools

# --- Configuración por defecto ------------------------------------------------

DEFAULT_QDRANT_PATH = config.QDRANT_PATH
DEFAULT_COLLECTION = "papers"
# Modelo pequeño y multilingüe (ES + EN), 384 dim, ~0.22 GB.
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Troceado por caracteres con solape (mantiene contexto entre fragmentos).
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

# Formatos que sabemos ingerir.
_INGESTIBLE_SUFFIXES = {".pdf", *tools._TEXT_SUFFIXES}

# Espacio de nombres fijo para los ids deterministas de los fragmentos.
_ID_NAMESPACE = uuid.UUID("6f1d9910-6a2b-5c1e-9f4e-6a65696306b0")

# Callback de progreso: (índice actual empezando en 0, total, fichero).
ProgressCallback = Callable[[int, int, str], None]


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _source_filter(source: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="source", match=models.MatchValue(value=source))
        ]
    )


class RAGEngine:
    """Base vectorial local para ingerir y consultar literatura científica."""

    def __init__(
        self,
        qdrant_path: str | Path | None = None,
        collection_name: str = DEFAULT_COLLECTION,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        kb_dir: str | Path | None = None,
    ) -> None:
        path = Path(qdrant_path) if qdrant_path else DEFAULT_QDRANT_PATH
        self.client = QdrantClient(path=str(path))
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.kb_dir = Path(kb_dir).resolve() if kb_dir else tools.KNOWLEDGE_BASE_DIR

    # --- Utilidades internas --------------------------------------------------

    def _exists(self) -> bool:
        return self.client.collection_exists(self.collection_name)

    def _ensure_collection(self) -> None:
        """Crea la colección con la dimensión del modelo si aún no existe."""
        if self._exists():
            return
        dim = self.client.get_embedding_size(self.embedding_model)
        self.client.create_collection(
            self.collection_name,
            vectors_config=models.VectorParams(
                size=dim, distance=models.Distance.COSINE
            ),
        )

    def _chunk(self, texto: str) -> list[str]:
        """Trocea el texto en ventanas solapadas de tamaño fijo."""
        texto = " ".join(texto.split())  # normaliza espacios y saltos de línea
        if not texto:
            return []
        paso = CHUNK_SIZE - CHUNK_OVERLAP
        fragmentos = [texto[i : i + CHUNK_SIZE] for i in range(0, len(texto), paso)]
        return [f for f in fragmentos if f.strip()]

    def _point_id(self, source: str, chunk: int) -> str:
        return str(uuid.uuid5(_ID_NAMESPACE, f"{source}#{chunk}"))

    def _indexed_sha(self, source: str) -> str | None:
        """SHA-256 con el que se indexó `source`, o None si no está indexado."""
        puntos, _ = self.client.scroll(
            self.collection_name,
            scroll_filter=_source_filter(source),
            limit=1,
            with_payload=["sha256"],
            with_vectors=False,
        )
        if not puntos:
            return None
        return (puntos[0].payload or {}).get("sha256") or ""

    def _count_source(self, source: str) -> int:
        return self.client.count(
            self.collection_name, count_filter=_source_filter(source)
        ).count

    def _delete_source(self, source: str) -> None:
        self.client.delete(
            self.collection_name,
            points_selector=models.FilterSelector(filter=_source_filter(source)),
        )

    # --- Ingesta --------------------------------------------------------------

    def list_ingestible(self) -> list[str]:
        """Rutas (relativas a `knowledge_base/`) de los documentos ingeribles."""
        rutas: list[str] = []
        if not self.kb_dir.exists():
            return rutas
        for ruta in sorted(self.kb_dir.rglob("*")):
            if ruta.is_dir() or ruta.name.startswith("."):
                continue
            if ruta.suffix.lower() not in _INGESTIBLE_SUFFIXES:
                continue
            if not tools._is_within(self.kb_dir, ruta):
                continue  # symlink que sale del sandbox
            rutas.append(str(ruta.relative_to(self.kb_dir)))
        return rutas

    def indexed_sources(self) -> set[str]:
        """Conjunto de documentos que tienen fragmentos en el índice."""
        if not self._exists():
            return set()
        fuentes: set[str] = set()
        offset = None
        while True:
            puntos, offset = self.client.scroll(
                self.collection_name,
                limit=512,
                offset=offset,
                with_payload=["source"],
                with_vectors=False,
            )
            fuentes.update(
                str((p.payload or {}).get("source")) for p in puntos if p.payload
            )
            if offset is None:
                return fuentes

    def ingest_file(self, filename: str, force: bool = False) -> str:
        """Ingiere (o reindexa) un documento de `knowledge_base/`.

        Si el documento ya está indexado y no ha cambiado, no hace nada
        (salvo con `force=True`).

        Args:
            filename: Ruta del documento relativa a `knowledge_base/`.
            force: Reindexar aunque el fichero no haya cambiado.

        Returns:
            Mensaje con el resultado (indexado, sin cambios o error).
        """
        try:
            ruta = tools._resolve_within(self.kb_dir, filename)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not ruta.is_file():
            return f"ERROR: '{filename}' no existe en knowledge_base/."

        rel = str(ruta.relative_to(self.kb_dir))
        try:
            sha = _sha256(ruta)
        except OSError as exc:
            return f"ERROR al leer '{filename}': {exc}"

        previo = self._indexed_sha(rel) if self._exists() else None
        if previo == sha and not force:
            n = self._count_source(rel)
            return f"Sin cambios '{rel}': ya estaba indexado ({n} fragmentos)."

        try:
            texto = tools.extract_text(ruta)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR al leer '{filename}': {exc}"

        fragmentos = self._chunk(texto)
        if previo is not None:
            self._delete_source(rel)
        if not fragmentos:
            return f"'{rel}' no contiene texto extraíble; nada que ingerir."

        self._ensure_collection()
        puntos = [
            models.PointStruct(
                id=self._point_id(rel, i),
                vector=models.Document(text=frag, model=self.embedding_model),
                payload={"source": rel, "chunk": i, "document": frag, "sha256": sha},
            )
            for i, frag in enumerate(fragmentos)
        ]
        self.client.upsert(self.collection_name, points=puntos)
        verbo = "Reindexado" if previo is not None else "Ingerido"
        return f"{verbo} '{rel}': {len(fragmentos)} fragmentos indexados."

    def ingest_directory(
        self, force: bool = False, progress: ProgressCallback | None = None
    ) -> str:
        """Sincroniza el índice con `knowledge_base/`.

        Ingiere los documentos nuevos, reindexa los modificados, deja intactos
        los que no cambiaron y elimina del índice los que ya no existen.

        Args:
            force: Reindexar todos los documentos aunque no hayan cambiado.
            progress: Callback opcional `(i, total, fichero)` llamado antes de
                procesar cada documento.
        """
        ficheros = self.list_ingestible()
        resultados: list[str] = []
        for i, rel in enumerate(ficheros):
            if progress is not None:
                progress(i, len(ficheros), rel)
            resultados.append(self.ingest_file(rel, force=force))

        for huerfano in sorted(self.indexed_sources() - set(ficheros)):
            self._delete_source(huerfano)
            resultados.append(
                f"Eliminado del índice '{huerfano}' (ya no está en knowledge_base/)."
            )

        if not resultados:
            return "No hay documentos ingeribles (.pdf/.md/.txt) en knowledge_base/."
        resultados.append(f"Total en el índice: {self.count()} fragmentos.")
        return "\n".join(resultados)

    # --- Consulta -------------------------------------------------------------

    def query(self, question: str, top_k: int = 5, source: str | None = None) -> str:
        """Recupera los fragmentos más relevantes del corpus para una pregunta.

        Args:
            question: Pregunta o tema a buscar en la literatura indexada.
            top_k: Número de fragmentos a devolver (por defecto 5, máximo 20).
            source: Si se indica, busca solo dentro de ese documento (ruta
                relativa a `knowledge_base/`).

        Returns:
            Los fragmentos relevantes con su fuente y puntuación, o un aviso
            si aún no hay nada indexado.
        """
        if not self._exists():
            return "Aún no hay nada indexado. Ejecuta ingest_corpus antes de consultar."

        top_k = max(1, min(top_k, 20))
        resultado = self.client.query_points(
            self.collection_name,
            query=models.Document(text=question, model=self.embedding_model),
            query_filter=_source_filter(source) if source else None,
            limit=top_k,
        )
        if not resultado.points:
            return "Sin resultados relevantes para esa consulta."

        bloques: list[str] = []
        for i, punto in enumerate(resultado.points, 1):
            payload = punto.payload or {}
            fuente = payload.get("source", "desconocida")
            texto = payload.get("document", "")
            bloques.append(
                f"[{i}] fuente: {fuente} | score: {punto.score:.3f}\n{texto}"
            )
        return "\n\n".join(bloques)

    def count(self) -> int:
        """Número de fragmentos indexados en la colección (0 si no existe)."""
        if not self._exists():
            return 0
        return self.client.count(self.collection_name).count

    def close(self) -> None:
        """Cierra el cliente Qdrant y libera el lock del directorio on-disk."""
        self.client.close()


def prefetch_model(embedding_model: str = DEFAULT_EMBEDDING_MODEL) -> str:
    """Descarga (si hace falta) el modelo de embeddings a la caché local.

    Conviene ejecutarlo una vez tras instalar: así la primera consulta no
    espera a una descarga de ~220 MB (que puede superar el timeout de un
    cliente MCP).
    """
    from fastembed import TextEmbedding

    TextEmbedding(model_name=embedding_model)
    return f"Modelo '{embedding_model}' listo en {config.FASTEMBED_CACHE_PATH}."


# --- Prueba de humo end-to-end ------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    if "--prefetch" in sys.argv:
        print(prefetch_model())
        sys.exit(0)

    # BD Qdrant y knowledge_base temporales: no toca los datos reales.
    with tempfile.TemporaryDirectory() as tmp:
        kb = Path(tmp) / "kb"
        kb.mkdir()
        rag = RAGEngine(
            qdrant_path=Path(tmp) / "qdrant", collection_name="_smoke", kb_dir=kb
        )
        a = kb / "a.md"
        try:
            a.write_text(
                "3D Gaussian Splatting es una técnica de renderizado en tiempo real "
                "que representa escenas mediante gaussianas 3D. El estándar DICOM "
                "define el formato de imágenes médicas y sus metadatos clínicos.",
                encoding="utf-8",
            )
            print(rag.ingest_directory())
            assert rag.count() == 1

            # Idempotencia: reingerir sin cambios no duplica.
            print(rag.ingest_directory())
            assert rag.count() == 1, "la reingesta ha duplicado fragmentos"

            # Fichero modificado (más largo): se reindexa.
            a.write_text("DICOM " * 500, encoding="utf-8")  # ~3000 chars -> 4 chunks
            print(rag.ingest_file("a.md"))
            n_largo = rag.count()
            assert n_largo > 1

            # Fichero acortado: desaparecen los fragmentos sobrantes.
            a.write_text("Texto corto sobre normativa clínica.", encoding="utf-8")
            print(rag.ingest_file("a.md"))
            assert rag.count() == 1, "quedaron fragmentos antiguos"

            # Documento borrado de la KB: se poda del índice.
            (kb / "b.txt").write_text("El estándar DICOM y PACS.", encoding="utf-8")
            a.unlink()
            print(rag.ingest_directory())
            assert rag.indexed_sources() == {"b.txt"}

            print("\n--- Consulta: '¿qué es el estándar DICOM?' ---")
            print(rag.query("¿qué es el estándar DICOM?", top_k=2, source="b.txt"))
            print("\nOK: ingesta idempotente verificada.")
        finally:
            rag.close()
