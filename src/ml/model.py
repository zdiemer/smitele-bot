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
#
# These are the defaults, not the definition. A model records the fields it was
# actually trained with in its own metadata and the scorer reads them back from
# there, because the two games do not have the same ones — Smite 2 adds a
# starter and an Aspect. A model file written before that metadata existed has
# exactly these fields, which is why the fallback is the Smite 1 list.
CONTEXT_FIELDS = ["god", "opponent", "role"]
POOLED_FIELDS = ["allies", "enemies", "items", "relics"]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _logit(p: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    clipped = np.clip(p, eps, 1.0 - eps)
    return np.log(clipped / (1.0 - clipped))


class NumpyScorer:
    """The trained model's forward pass, without torch.

    Weights come from `WinProbabilityModel.export`.
    """

    def __init__(self, weights: Dict[str, np.ndarray], meta: Dict):
        self.w = {k: np.asarray(v) for k, v in weights.items()}
        self.meta = meta
        self.context_fields = list(meta.get("context_fields") or CONTEXT_FIELDS)
        self.pooled_fields = list(meta.get("pooled_fields") or POOLED_FIELDS)

    def __call__(self, batch: Dict[str, np.ndarray]) -> np.ndarray:
        parts = []
        for field in self.context_fields:
            table = self.w[f"emb_{field}"]
            parts.append(table[np.asarray(batch[field], dtype=np.int64)])

        for field in self.pooled_fields:
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

    @property
    def is_calibrated(self) -> bool:
        return bool(self.meta.get("calibration"))

    def calibrate(self, scores: np.ndarray) -> np.ndarray:
        """Raw sigmoid -> a number that can be shown to someone as a percentage.

        The training objective makes the raw output a good *ranking* and says
        nothing about whether 0.62 means 62%. It generally does not: the model
        sees only complete six-item builds, whose base rate is not the base rate
        of the games anyone is asking about, and eight epochs of embeddings on
        millions of rows drift confident. `/edge` printed the raw sigmoid as
        "wins N% of the time" and that sentence was never checked.

        Platt scaling — one slope and one intercept on the logit, fitted on a
        window held out from both training and the reported test — is the whole
        correction. Two parameters cannot repair a bad ranking and are not meant
        to; they map a good one onto the right scale, and they cannot reorder
        anything, so the AUC is identical before and after by construction.

        Uncalibrated models return their raw score unchanged rather than a
        guess, so a caller can ask `is_calibrated` before quoting a number.
        """
        calibration = self.meta.get("calibration")
        if not calibration:
            return np.asarray(scores, dtype=float)
        slope = float(calibration.get("slope", 1.0))
        intercept = float(calibration.get("intercept", 0.0))
        return _sigmoid(slope * _logit(np.asarray(scores, dtype=float)) + intercept)

    def save(self, path: str) -> None:
        np.savez_compressed(path, meta=np.array(repr(self.meta)), **self.w)

    @staticmethod
    def load(path: str) -> "NumpyScorer":
        import ast

        data = np.load(path, allow_pickle=False)
        meta = ast.literal_eval(str(data["meta"]))
        weights = {k: data[k] for k in data.files if k != "meta"}
        return NumpyScorer(weights, meta)


def fit_calibration(
    scores: np.ndarray, labels: np.ndarray, iterations: int = 50
) -> Dict[str, float]:
    """Platt scaling by Newton's method, in numpy so the bot could refit it.

    Fitting a two-parameter logistic regression of the label on the logit. The
    Hessian is 2x2, so Newton converges in a handful of steps and there is no
    step size to tune; the ridge term only keeps the solve from failing when a
    window happens to be separable or all one class.
    """
    x = _logit(np.asarray(scores, dtype=float))
    y = np.asarray(labels, dtype=float)
    design = np.stack([x, np.ones_like(x)], axis=1)
    weights = np.array([1.0, 0.0])

    for _ in range(iterations):
        probabilities = _sigmoid(design @ weights)
        gradient = design.T @ (probabilities - y)
        variance = np.clip(probabilities * (1.0 - probabilities), 1e-9, None)
        hessian = design.T @ (design * variance[:, None]) + 1e-6 * np.eye(2)
        step = np.linalg.solve(hessian, gradient)
        weights = weights - step
        if np.abs(step).max() < 1e-8:
            break

    return {"slope": float(weights[0]), "intercept": float(weights[1])}


def expected_calibration_error(
    scores: np.ndarray, labels: np.ndarray, bins: int = 10
) -> float:
    """Mean gap between predicted and realised probability, weighted by bin size.

    The single number behind `build_eval`'s reliability table: 0.0 means every
    bin's predictions came true at exactly the claimed rate.
    """
    scores = np.asarray(scores, dtype=float)
    labels = np.asarray(labels, dtype=float)
    if not len(scores):
        return float("nan")
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    for low, high in zip(edges[:-1], edges[1:]):
        inside = (scores >= low) & (scores < high if high < 1.0 else scores <= high)
        if not inside.any():
            continue
        gap = abs(scores[inside].mean() - labels[inside].mean())
        total += gap * inside.sum() / len(scores)
    return float(total)


def build_torch_model(
    vocab_sizes: Dict[str, int],
    skill_width: int,
    context_fields: list = None,
    pooled_fields: list = None,
):
    """Construct the trainable module. Imported lazily so the bot never needs torch."""
    import torch
    from torch import nn

    context_fields = list(context_fields or CONTEXT_FIELDS)
    pooled_fields = list(pooled_fields or POOLED_FIELDS)

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
                    for field in context_fields + pooled_fields
                }
            )
            width = EMBEDDING_DIM * len(context_fields + pooled_fields) + skill_width
            self.fc1 = nn.Linear(width, HIDDEN_DIM)
            self.fc2 = nn.Linear(HIDDEN_DIM, 1)

        def forward(self, batch):
            parts = [self.embeddings[f"e_{f}"](batch[f]) for f in context_fields]
            for field in pooled_fields:
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
