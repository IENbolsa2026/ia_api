"""Escaneo de CV en Linux y rechazo ante errores del antivirus."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import settings
from app.services.cv_artifacts import CVArtifactError, scan_artifact, scanner_available


class CVScannerTests(unittest.TestCase):
    def test_clamav_uses_file_scan(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'cv.pdf'
            path.write_bytes(b'archivo de prueba')
            with patch.object(settings, 'CV_SCAN_ENABLED', True), \
                 patch.object(settings, 'CV_SCAN_BACKEND', 'clamav'), \
                 patch.object(settings, 'CV_SCAN_COMMAND', sys.executable), \
                 patch('app.services.cv_artifacts.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
                self.assertTrue(scanner_available())
                scan_artifact(path)
            self.assertEqual(run.call_args.args[0], [sys.executable, '--no-summary', str(path)])

    def test_clamav_rejects_infected_and_failed_scan(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'cv.pdf'
            path.write_bytes(b'archivo de prueba')
            with patch.object(settings, 'CV_SCAN_ENABLED', True), \
                 patch.object(settings, 'CV_SCAN_BACKEND', 'clamav'), \
                 patch.object(settings, 'CV_SCAN_COMMAND', sys.executable):
                for code in (1, 2):
                    with self.subTest(code=code):
                        with patch('app.services.cv_artifacts.subprocess.run', return_value=SimpleNamespace(returncode=code)):
                            with self.assertRaises(CVArtifactError):
                                scan_artifact(path)


if __name__ == '__main__':
    unittest.main()
