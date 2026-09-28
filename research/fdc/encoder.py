"""Pinned optional fixed multilingual encoder; no Qwen hidden-state assumption."""

import torch


MODEL_ID = "intfloat/multilingual-e5-small"
REVISION = "fd1525a9fd15316a2d503bf26ab031a61d056e98"
DIMENSION = 384


def encode_episodes(rows: list[dict]) -> dict[tuple[str, str], torch.Tensor]:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("install the optional encoder dependency on the remote host") from exc
    model = SentenceTransformer(MODEL_ID, revision=REVISION, device="cpu",
                                trust_remote_code=False)
    if model.get_embedding_dimension() != DIMENSION:
        raise RuntimeError("pinned encoder dimension changed")
    query_texts = sorted({row["text"] for row in rows})
    passage_texts = sorted({item["text"] for row in rows for item in row["evidence"]})
    result = {}
    for role, texts in (("query", query_texts), ("passage", passage_texts)):
        if not texts:
            continue
        prefixed = [role + ": " + value for value in texts]
        vectors = model.encode(prefixed, batch_size=32, normalize_embeddings=True,
                               convert_to_tensor=True, show_progress_bar=False)
        for value, vector in zip(texts, vectors):
            result[(role, value)] = vector.detach().cpu().float()
    return result
