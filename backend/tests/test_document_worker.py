import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.document_extractor_runner import extract_in_subprocess


class DocumentWorkerTests(unittest.TestCase):
    def test_txt_extraction_runs_in_resource_limited_child(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.txt"
            path.write_text("Experiencia laboral como chofer.", encoding="utf-8")
            self.assertEqual(extract_in_subprocess(path, "txt"), "Experiencia laboral como chofer.")

    def test_multilingual_text_roundtrips_with_cp1252_strict_subprocess_stdout(self):
        text = "● • Café y conducción 🚚 — 中文 résumé 😀"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.txt"
            path.write_text(text, encoding="utf-8")
            allowed_keys = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH"}
            child_env = {key: value for key, value in os.environ.items() if key.upper() in allowed_keys}
            child_env.update({"CV_EXTRACTOR_PROCESS": "1", "PYTHONIOENCODING": "cp1252:strict"})
            process = subprocess.run(
                [sys.executable, "-m", "app.services.document_worker", str(path), "txt"],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                shell=False,
                env=child_env,
            )
            self.assertEqual(process.returncode, 0)
            self.assertEqual(json.loads(process.stdout.decode("utf-8"))["text"], text)

    def test_large_emoji_text_stays_within_utf8_output_limit(self):
        text = "😀" * 35_000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.txt"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(extract_in_subprocess(path, "txt"), text)

    def test_error_json_escapes_characters_not_supported_by_cp1252(self):
        from app.services import document_worker

        class Cp1252StrictStream:
            encoding = "cp1252"

            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, value):
                raise AssertionError("El protocolo debe escribir bytes UTF-8, no texto a la consola.")

        stream = Cp1252StrictStream()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.txt"
            path.write_text("contenido", encoding="utf-8")
            with patch.object(document_worker, "configure_process_limits"), \
                 patch("app.services.document_text.extract_document_text", side_effect=ValueError("Error ● 🚚 中文")), \
                 patch.object(document_worker.sys, "argv", ["document_worker", str(path), "txt"]), \
                 patch.object(document_worker.sys, "stdout", stream):
                exit_code = document_worker.main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stream.buffer.getvalue().decode("utf-8"))["error"], "Error ● 🚚 中文")

    def test_unicode_encode_error_is_sanitized_in_error_response(self):
        from app.services import document_worker

        class BinaryStream:
            def __init__(self):
                self.buffer = io.BytesIO()

        stream = BinaryStream()
        encoding_error = UnicodeEncodeError("charmap", "●", 0, 1, "character maps to <undefined>")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.txt"
            path.write_text("contenido", encoding="utf-8")
            with patch.object(document_worker, "configure_process_limits"), \
                 patch("app.services.document_text.extract_document_text", side_effect=encoding_error), \
                 patch.object(document_worker.sys, "argv", ["document_worker", str(path), "txt"]), \
                 patch.object(document_worker.sys, "stdout", stream):
                exit_code = document_worker.main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(stream.buffer.getvalue().decode("utf-8"))["error"],
            "No se pudo extraer el texto del documento.",
        )


if __name__ == "__main__":
    unittest.main()
