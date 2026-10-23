import io
import unittest
import subprocess
import zipfile
from unittest.mock import patch
from types import SimpleNamespace

from app.core.config import settings
from app.services.document_text import (
    DocumentRejected,
    _extract_pdf_page,
    detect_format,
    extract_document_text,
)


def office_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return buffer.getvalue()


class DocumentTextTests(unittest.TestCase):
    def test_pdf_side_by_side_sections_are_not_interleaved(self):
        words = []
        for row in range(12):
            top = row * 14
            words.extend({'x0': x, 'x1': x + 20, 'top': top} for x in (40, 90, 140, 330, 380, 430))

        class Page:
            bbox = (0, 0, 600, 800)

            def extract_words(self):
                return words

            def extract_text(self):
                return 'education experience education experience'

            def crop(self, bounds):
                return SimpleNamespace(extract_text=lambda: 'education and courses' if bounds[0] == 0
                                       else 'experience and skills')

        self.assertEqual(_extract_pdf_page(Page()), 'education and courses\n\nexperience and skills')

    def test_pdf_with_full_width_lines_keeps_normal_reading_order(self):
        words = []
        for row in range(12):
            words.extend({'x0': x, 'x1': x + 25, 'top': row * 14}
                         for x in range(40, 540, 35))

        class Page:
            bbox = (0, 0, 600, 800)

            def extract_words(self):
                return words

            def extract_text(self):
                return 'single-column text'

            def crop(self, bounds):
                raise AssertionError('Full-width text must not be split')

        self.assertEqual(_extract_pdf_page(Page()), 'single-column text')

    def test_txt_accepts_utf8_and_windows_1252(self):
        self.assertEqual(detect_format("cv.txt", "Experiencia en logística".encode()), "txt")
        cp = "Experiencia en logística".encode("cp1252")
        self.assertEqual(extract_document_text(cp, "txt"), "Experiencia en logística")

    def test_docx_hyperlinks_and_external_templates_are_ignored_without_io(self):
        document = '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>LinkedIn</w:t></w:r></w:p></w:body></w:document>'
        targets = ('https://example.invalid/template.dotx', 'file:///C:/plantillas/cv.dotm', r'\\servidor\plantillas\cv.dotx')
        for relationship_type in ('hyperlink', 'attachedTemplate'):
            for target in targets:
                with self.subTest(relationship_type=relationship_type, target=target):
                    relationship = ('<Relationships xmlns="r"><Relationship '
                        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{relationship_type}" '
                        f'TargetMode="External" Target="{target}"/></Relationships>')
                    payload = office_zip({"[Content_Types].xml": "<Types/>", "word/document.xml": document,
                        "word/settings.xml": '<w:settings xmlns:w="w" xmlns:r="r"><w:attachedTemplate r:id="rId1"/></w:settings>',
                        "word/_rels/settings.xml.rels": relationship})
                    with patch('socket.socket.connect', side_effect=AssertionError('External network access')), \
                         patch('pathlib.Path.open', side_effect=AssertionError('External file access')):
                        self.assertEqual(detect_format('cv.docx', payload), 'docx')
                        self.assertEqual(extract_document_text(payload, 'docx'), 'LinkedIn')

    def test_docx_other_external_resources_remain_rejected(self):
        for relationship_type in ('image', 'oleObject', 'aFChunk'):
            with self.subTest(relationship_type=relationship_type):
                relationship = ('<Relationships xmlns="r"><Relationship '
                    f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{relationship_type}" '
                    'TargetMode="External" Target="https://example.invalid/resource"/></Relationships>')
                payload = office_zip({'[Content_Types].xml': '<Types/>',
                    'word/document.xml': '<w:document xmlns:w="w"><w:t>Ventas</w:t></w:document>',
                    'word/_rels/document.xml.rels': relationship})
                with self.assertRaisesRegex(DocumentRejected, 'recursos externos'):
                    extract_document_text(payload, 'docx')

    def test_docx_template_does_not_allow_macros_or_unsafe_relationship_xml(self):
        entries = {'[Content_Types].xml': '<Types/>',
            'word/document.xml': '<w:document xmlns:w="w"><w:t>Ventas</w:t></w:document>',
            'word/_rels/settings.xml.rels': '<Relationships><Relationship Type="r/attachedTemplate" TargetMode="External" Target="file:///missing.dotx"/></Relationships>'}
        with self.assertRaises(DocumentRejected):
            extract_document_text(office_zip({**entries, 'word/vbaProject.bin': b'macro'}), 'docx')
        for xml in ('<Relationships><broken>', '<!DOCTYPE x [<!ENTITY e "bad">]><Relationships>&e;</Relationships>'):
            with self.subTest(xml=xml), self.assertRaises(DocumentRejected):
                extract_document_text(office_zip({**entries, 'word/_rels/settings.xml.rels': xml}), 'docx')

    def test_docx_dtd_and_archive_limits_are_rejected(self):
        malicious = office_zip({"[Content_Types].xml": "<Types/>", "word/document.xml": '<!DOCTYPE x [<!ENTITY e "bad">]><x>&e;</x>'})
        with self.assertRaises(DocumentRejected):
            extract_document_text(malicious, "docx")

    def test_doc_and_rtf_are_only_detected_with_configured_converter(self):
        with patch("app.services.document_text.legacy_converter_available", return_value=False):
            with self.assertRaisesRegex(DocumentRejected, "conversor aislado"):
                detect_format("cv.rtf", b"{\\rtf1 texto}")

    @patch("app.services.document_text.shutil.which", return_value="docker")
    @patch("app.services.document_text.subprocess.run", return_value=SimpleNamespace(returncode=1))
    def test_converter_is_not_advertised_when_image_is_missing(self, run_mock, which_mock):
        with patch.object(settings, "CV_LEGACY_CONVERTER_IMAGE", "ien-cv-converter:missing"):
            from app.services.document_text import enabled_formats
            self.assertNotIn("doc", enabled_formats())
            run_mock.assert_called_once()

    @patch("app.services.document_text.legacy_converter_available", return_value=True)
    @patch("app.services.document_text.shutil.which", return_value="docker")
    @patch("app.services.document_text.subprocess.run")
    def test_legacy_conversion_uses_named_container_and_removes_it(self, run_mock, which_mock, available):
        def docker_run(command, **kwargs):
            if command[1] == "run":
                output_mount = next(part for part in command if ":/output:rw" in part)
                output_dir = output_mount.removesuffix(":/output:rw")
                with open(f"{output_dir}/document.txt", "w", encoding="utf-8") as output:
                    output.write("Experiencia como chofer")
            return SimpleNamespace(returncode=0)

        run_mock.side_effect = docker_run
        with patch.object(settings, "CV_LEGACY_CONVERTER_IMAGE", "ien-cv-converter:local"):
            self.assertEqual(extract_document_text(b"{\\rtf1 texto}", "rtf"), "Experiencia como chofer")
        run, cleanup = [call.args[0] for call in run_mock.call_args_list]
        self.assertIn("--name", run)
        self.assertIn("ien-cv-convert-", run[run.index("--name") + 1])
        self.assertEqual(cleanup[1:3], ["rm", "--force"])

    @patch("app.services.document_text.legacy_converter_available", return_value=True)
    @patch("app.services.document_text.shutil.which", return_value="docker")
    @patch("app.services.document_text.subprocess.run")
    def test_timed_out_converter_still_attempts_container_cleanup(self, run_mock, which_mock, available):
        run_mock.side_effect = [subprocess.TimeoutExpired("docker", 30), SimpleNamespace(returncode=0)]
        with patch.object(settings, "CV_LEGACY_CONVERTER_IMAGE", "ien-cv-converter:local"):
            with self.assertRaisesRegex(DocumentRejected, "tiempo máximo"):
                extract_document_text(b"{\\rtf1 texto}", "rtf")
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(run_mock.call_args_list[1].args[0][1:3], ["rm", "--force"])
