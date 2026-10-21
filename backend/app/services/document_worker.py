"""Punto de entrada del proceso restringido que parsea un CV."""

from __future__ import annotations

import json
import sys

from ien_contracts.process_limits import configure_process_limits


def _write_json(payload: dict[str, str]) -> None:
    """Escribe el protocolo como UTF-8, sin depender de la consola del worker."""
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(encoded)


def main() -> int:
    try:
        configure_process_limits()
        from app.services.document_text import extract_document_text

        file_path, document_format = sys.argv[1], sys.argv[2]
        data = open(file_path, "rb").read()
        result = extract_document_text(data, document_format)
        _write_json({"text": result})
        return 0
    except Exception as exc:
        # No se devuelve ruta, contenido de CV ni excepción cruda.
        message = (
            str(exc)
            if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError)
            else "No se pudo extraer el texto del documento."
        )
        _write_json({"error": message})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
