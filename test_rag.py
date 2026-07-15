"""Testes das funções puras do RAG: chunking, similaridade e guardrail
(sem chamar as APIs da Voyage/Anthropic de verdade)."""

import unittest
from unittest.mock import MagicMock, patch

import rag


class ChunkTextTest(unittest.TestCase):
    def test_keeps_short_text_as_single_chunk(self):
        text = "Um parágrafo curto."
        self.assertEqual(rag.chunk_text(text), [text])

    def test_splits_by_paragraph_boundaries(self):
        paragraphs = ["Parágrafo " + str(i) + " " + ("x" * 400) for i in range(4)]
        text = "\n\n".join(paragraphs)
        chunks = rag.chunk_text(text, min_size=200, max_size=500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 500)
        # nenhum parágrafo original deve ter sido cortado no meio
        rebuilt = "\n\n".join(chunks)
        for p in paragraphs:
            self.assertIn(p, rebuilt)

    def test_splits_single_long_paragraph_by_sentence(self):
        sentence = "Esta é uma frase de teste com tamanho razoável para o exemplo. "
        text = sentence * 30  # um único "parágrafo" bem longo
        chunks = rag.chunk_text(text, min_size=200, max_size=500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 500)
            self.assertTrue(chunk.strip().endswith("."))

    def test_empty_text_returns_no_chunks(self):
        self.assertEqual(rag.chunk_text(""), [])
        self.assertEqual(rag.chunk_text("   \n\n  "), [])


class CosineSimilarityTest(unittest.TestCase):
    def test_identical_vectors_have_similarity_one(self):
        self.assertAlmostEqual(rag.cosine_similarity([1, 2, 3], [1, 2, 3]), 1.0, places=6)

    def test_orthogonal_vectors_have_similarity_zero(self):
        self.assertAlmostEqual(rag.cosine_similarity([1, 0], [0, 1]), 0.0, places=6)

    def test_opposite_vectors_have_similarity_minus_one(self):
        self.assertAlmostEqual(rag.cosine_similarity([1, 0], [-1, 0]), -1.0, places=6)

    def test_zero_vector_does_not_crash(self):
        self.assertEqual(rag.cosine_similarity([0, 0], [1, 1]), 0.0)


class SearchTest(unittest.TestCase):
    def test_returns_top_k_sorted_by_similarity(self):
        index = [
            {"id": "a", "texto": "a", "url": "", "titulo": "", "embedding": [1, 0]},
            {"id": "b", "texto": "b", "url": "", "titulo": "", "embedding": [0, 1]},
            {"id": "c", "texto": "c", "url": "", "titulo": "", "embedding": [0.9, 0.1]},
        ]
        results = rag.search([1, 0], index, top_k=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][1]["id"], "a")
        self.assertEqual(results[1][1]["id"], "c")


class ShouldRespondTest(unittest.TestCase):
    def test_private_chat_always_responds(self):
        message = {"chat": {"type": "private"}, "text": "qualquer pergunta"}
        self.assertTrue(rag.should_respond(message, "meubot", 999))

    def test_private_chat_ignores_commands(self):
        message = {"chat": {"type": "private"}, "text": "/start"}
        self.assertFalse(rag.should_respond(message, "meubot", 999))

    def test_group_ignores_unrelated_message(self):
        message = {"chat": {"type": "group"}, "text": "conversa qualquer entre pessoas"}
        self.assertFalse(rag.should_respond(message, "meubot", 999))

    def test_group_responds_when_mentioned(self):
        message = {"chat": {"type": "group"}, "text": "@meubot qual sua opinião?"}
        self.assertTrue(rag.should_respond(message, "meubot", 999))

    def test_group_responds_when_reply_to_bot(self):
        message = {
            "chat": {"type": "supergroup"},
            "text": "e sobre isso?",
            "reply_to_message": {"from": {"id": 999}},
        }
        self.assertTrue(rag.should_respond(message, "meubot", 999))

    def test_group_ignores_reply_to_someone_else(self):
        message = {
            "chat": {"type": "supergroup"},
            "text": "e sobre isso?",
            "reply_to_message": {"from": {"id": 111}},
        }
        self.assertFalse(rag.should_respond(message, "meubot", 999))


class StripMentionTest(unittest.TestCase):
    def test_removes_mention_from_text(self):
        self.assertEqual(rag.strip_mention("@meubot qual sua opinião?", "meubot"), "qual sua opinião?")

    def test_keeps_text_unchanged_without_username(self):
        self.assertEqual(rag.strip_mention("pergunta normal", None), "pergunta normal")


class AnswerQuestionTest(unittest.TestCase):
    def test_refuses_without_calling_claude_when_below_threshold(self):
        index = [{"id": "a", "texto": "sobre filosofia", "url": "u", "titulo": "t", "embedding": [1, 0]}]
        fake_voyage = MagicMock()
        fake_voyage.embed.return_value = MagicMock(embeddings=[[0, 1]])  # ortogonal -> score 0
        fake_anthropic = MagicMock()

        answer = rag.answer_question(
            "pergunta bem fora do tema",
            index=index,
            voyage_client=fake_voyage,
            anthropic_client=fake_anthropic,
        )

        self.assertEqual(answer, rag.REFUSAL_MESSAGE)
        fake_anthropic.messages.create.assert_not_called()

    def test_calls_claude_when_above_threshold(self):
        index = [{"id": "a", "texto": "sobre filosofia", "url": "u", "titulo": "t", "embedding": [1, 0]}]
        fake_voyage = MagicMock()
        fake_voyage.embed.return_value = MagicMock(embeddings=[[1, 0]])  # idêntico -> score 1
        fake_anthropic = MagicMock()
        text_block = MagicMock(type="text", text="Resposta com base no artigo.")
        fake_anthropic.messages.create.return_value = MagicMock(content=[text_block])

        answer = rag.answer_question(
            "pergunta dentro do tema",
            index=index,
            voyage_client=fake_voyage,
            anthropic_client=fake_anthropic,
        )

        self.assertEqual(answer, "Resposta com base no artigo.")
        fake_anthropic.messages.create.assert_called_once()

    def test_refuses_when_index_is_empty(self):
        fake_voyage = MagicMock()
        fake_anthropic = MagicMock()
        answer = rag.answer_question(
            "qualquer pergunta", index=[], voyage_client=fake_voyage, anthropic_client=fake_anthropic
        )
        self.assertEqual(answer, rag.REFUSAL_MESSAGE)
        fake_voyage.embed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
