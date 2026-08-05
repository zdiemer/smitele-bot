"""The win-probability model, and a numpy copy of its forward pass.

The model estimates P(win | god, role, matchup, skill, build). A build
recommendation is then a search: hold the context fixed, vary the items, take
the highest-scoring legal build. That is what "the build most likely to give an
edge in this matchup" means operationally — the model never emits a build
directly, it ranks them.

Two deliberate choices:

Compositions are mean-pooled embeddings rather than concatenated slots, so the
model sees a team as a set. Enemy draft order carries no meaning and
concatenating would make the model learn the same matchup five times over.

Inference is reimplemented in numpy. The forward pass is a handful of embedding
lookups and two matrix multiplies, and keeping it dependency-free means the bot
image doesn't carry torch (~800MB) just to score candidate builds. Training
exports plain arrays; `NumpyScorer` is checked against the torch module during
training so the two cannot silently diverge.
"""

from __future__ import annotations

from typing import Dict

import numpy as np

EMBEDDING_DIM: int = 32
HIDDEN_DIM: int = 64

# Order matters: it fixes the layout of the concatenated feature vector, and
# the numpy scorer has to agree with the torch module about it.
CONTEXT_FIELDS = ["god", "opponent", "role"]
POOLED_FIELDS = ["allies", "enemies", "items", "relics"]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


class NumpyScorer:
    """The trained model's forward pass, without torch.

    Weights come from `WinProbabilityModel.export`.
    """

    def __init__(self, weights: Dict[str, np.ndarray], meta: Dict):
        self.w = {k: np.asarray(v) for k, v in weights.items()}
        self.meta = meta

    def __call__(self, batch: Dict[str, np.ndarray]) -> np.ndarray:
        parts = []
        for field in CONTEXT_FIELDS:
            table = self.w[f"emb_{field}"]
            parts.append(table[np.asarray(batch[field], dtype=np.int64)])

        for field in POOLED_FIELDS:
            table = self.w[f"emb_{field}"]
            indices = np.asarray(batch[field], dtype=np.int64)
            vectors = table[indices]
            # Index 0 is "absent"; masking keeps padding out of the mean so a
            # four-slot ally list isn't diluted toward the zero vector.
            mask = (indices > 0)[..., None].astype(vectors.dtype)
            total = (vectors * mask).sum(axis=1)
            count = np.clip(mask.sum(axis=1), 1.0, None)
            parts.append(total / count)

        parts.append(np.asarray(batch["skill"], dtype=np.float32))
        features = np.concatenate(parts, axis=1)

        hidden = features @ self.w["fc1_w"].T + self.w["fc1_b"]
        hidden = np.maximum(hidden, 0.0)
        logits = hidden @ self.w["fc2_w"].T + self.w["fc2_b"]
        return _sigmoid(logits).ravel()

    def save(self, path: str) -> None:
        np.savez_compressed(path, meta=np.array(repr(self.meta)), **self.w)

    @staticmethod
    def load(path: str) -> "NumpyScorer":
        import ast

        data = np.load(path, allow_pickle=False)
        meta = ast.literal_eval(str(data["meta"]))
        weights = {k: data[k] for k in data.files if k != "meta"}
        return NumpyScorer(weights, meta)


def build_torch_model(vocab_sizes: Dict[str, int], skill_width: int):
    """Construct the trainable module. Imported lazily so the bot never needs torch."""
    import torch
    from torch import nn

    class WinProbabilityModel(nn.Module):
        def __init__(self):
            super().__init__()
            # Keys are prefixed because ModuleDict refuses names that collide
            # with dict's own attributes, and one of the fields is "items".
            self.embeddings = nn.ModuleDict(
                {
                    f"e_{field}": nn.Embedding(
                        vocab_sizes[field], EMBEDDING_DIM, padding_idx=0
                    )
                    for field in CONTEXT_FIELDS + POOLED_FIELDS
                }
            )
            width = EMBEDDING_DIM * len(CONTEXT_FIELDS + POOLED_FIELDS) + skill_width
            self.fc1 = nn.Linear(width, HIDDEN_DIM)
            self.fc2 = nn.Linear(HIDDEN_DIM, 1)

        def forward(self, batch):
            parts = [self.embeddings[f"e_{f}"](batch[f]) for f in CONTEXT_FIELDS]
            for field in POOLED_FIELDS:
                indices = batch[field]
                vectors = self.embeddings[f"e_{field}"](indices)
                mask = (indices > 0).unsqueeze(-1).float()
                parts.append(
                    (vectors * mask).sum(1) / mask.sum(1).clamp(min=1.0)
                )
            parts.append(batch["skill"])
            features = torch.cat(parts, dim=1)
            hidden = torch.relu(self.fc1(features))
            # Logits, not probabilities: training uses BCEWithLogitsLoss, which
            # is the numerically stable pairing.
            return self.fc2(hidden).squeeze(-1)

        def export(self):
            weights = {
                f"emb_{name[len('e_'):]}": module.weight.detach().cpu().numpy()
                for name, module in self.embeddings.items()
            }
            weights["fc1_w"] = self.fc1.weight.detach().cpu().numpy()
            weights["fc1_b"] = self.fc1.bias.detach().cpu().numpy()
            weights["fc2_w"] = self.fc2.weight.detach().cpu().numpy()
            weights["fc2_b"] = self.fc2.bias.detach().cpu().numpy()
            return weights

    return WinProbabilityModel()
