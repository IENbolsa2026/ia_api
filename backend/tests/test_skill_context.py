"""RAG context must preserve factual CV extraction and cover mixed profiles."""

import unittest

from app.services.skill_catalog_store import SkillCatalogStore
from app.services.skill_context import _segments, build_cv_skill_context, seed_concepts


class SkillContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_cv_retrieves_distinct_tools_and_domain_guidance(self):
        text = (
            "Soy programador en Python y Django.\n"
            "Atendí clientes y usé Excel para registrar pedidos.\n"
            "Planifiqué campañas de marketing y registré ventas en CRM."
        )
        context = await build_cv_skill_context(text, store=SkillCatalogStore(concepts=seed_concepts()))
        for name in ("Python [tech.python", "Django [tech.django",
                     "Microsoft Excel [tech.excel", "CRM [sales.crm"):
            self.assertIn(name, context)
        self.assertIn("Competencias técnicas", context)
        self.assertIn("marketing", context)
        self.assertIn("Pensamiento crítico", context)
        self.assertIn("NO evidencia del postulante", context)

    async def test_retrieval_keeps_long_cv_tail_and_limits_context(self):
        text = ("Experiencia administrativa y atención al público. " * 100) + "\nProgramé con Python."
        self.assertTrue(any("Python" in segment for segment in _segments(text)))
        context = await build_cv_skill_context(text, max_chars=16000,
                                               store=SkillCatalogStore(concepts=seed_concepts()))
        self.assertIn("Python [tech.python", context)
        self.assertLessEqual(len(context), 16000)

    async def test_training_only_hit_is_guidance_not_attributed_skill(self):
        context = await build_cv_skill_context("Curso de Excel en progreso.",
                                               store=SkillCatalogStore(concepts=seed_concepts()))
        self.assertIn("Un curso de Excel por sí solo no demuestra dominio", context)
        self.assertIn("Una habilidad entra al resultado solo con una cita literal del CV", context)

    async def test_isolated_tool_line_survives_long_document_sampling(self):
        lines = ["Atendí a clientes y registré pedidos." for _ in range(90)]
        lines[37] = "Usé Excel para controlar las ventas diarias."
        text = "\n".join(f"{index}: {line}" for index, line in enumerate(lines))
        self.assertTrue(any("Excel" in segment for segment in _segments(text)))
        context = await build_cv_skill_context(text,
                                               store=SkillCatalogStore(concepts=seed_concepts()))
        self.assertIn("Microsoft Excel [tech.excel", context)


if __name__ == "__main__":
    unittest.main()
