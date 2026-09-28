"""Order-sensitive flat MLP comparison, deliberately without graph attention."""

import torch
from torch import nn

from .model import Config


class FlatMLP(nn.Module):
    def __init__(self, cfg: Config, dims: tuple[int, int, int], constraints: int = 3):
        super().__init__()
        assets, _time, evidence = dims
        self.cfg = cfg
        self.assets = assets
        self.evidence = evidence
        self.constraints = constraints
        width = (cfg.goal_features + 2 * cfg.text_width
                 + assets * (cfg.asset_features + cfg.numeric_features)
                 + cfg.constraint_features)
        self.trunk = nn.Sequential(
            nn.Linear(width, cfg.width), nn.ReLU(),
            nn.Linear(cfg.width, cfg.width), nn.ReLU(),
        )
        self.allocation = nn.Linear(cfg.width, cfg.plans * (assets + 1))
        self.evidence_head = nn.Linear(cfg.width, cfg.plans * (evidence + 1))
        self.dependency = nn.Linear(cfg.width, cfg.plans * (self.constraints + 1))
        self.plan_value = nn.Linear(cfg.width, cfg.plans)
        self.mode = nn.Linear(cfg.width, 3)
        self.question = nn.Linear(cfg.width, cfg.question_fields)

    def forward(self, *, history, history_valid, times, asset_features,
                asset_valid, relations, evidence_embeddings, evidence_valid,
                constraints, constraint_valid, goal_features, user_embedding):
        # Padded histories/evidence contribute zero. The model sees no order,
        # relation edge, or explicit time feature; this is the ablation point.
        count = history_valid.sum(-1, keepdim=True).clamp_min(1)
        historical = (history * history_valid[..., None]).sum(2) / count
        historical = historical.masked_fill(~asset_valid[..., None], 0)
        current = asset_features.masked_fill(~asset_valid[..., None], 0)
        evidence_count = evidence_valid.sum(1, keepdim=True).clamp_min(1)
        evidence_mean = (evidence_embeddings * evidence_valid[..., None]).sum(1) / evidence_count
        constraint_count = constraint_valid.sum(1, keepdim=True).clamp_min(1)
        constraint_mean = (constraints * constraint_valid[..., None]).sum(1) / constraint_count
        joined = torch.cat((goal_features, user_embedding, evidence_mean,
                            current.flatten(1), historical.flatten(1), constraint_mean), dim=1)
        hidden = self.trunk(joined)
        batch = hidden.shape[0]
        valid_assets = torch.cat((asset_valid,
                                  torch.ones(batch, 1, dtype=torch.bool,
                                             device=hidden.device)), dim=1)
        valid_evidence = torch.cat((evidence_valid,
                                    torch.ones(batch, 1, dtype=torch.bool,
                                               device=hidden.device)), dim=1)
        return {
            "allocation_logits": self.allocation(hidden).reshape(
                batch, self.cfg.plans, self.assets + 1).masked_fill(
                    ~valid_assets[:, None, :], float("-inf")),
            "evidence_logits": self.evidence_head(hidden).reshape(
                batch, self.cfg.plans, self.evidence + 1).masked_fill(
                    ~valid_evidence[:, None, :], float("-inf")),
            "dependency_logits": self.dependency(hidden).reshape(
                batch, self.cfg.plans, self.constraints + 1),
            "plan_scores": self.plan_value(hidden),
            "mode_logits": self.mode(hidden),
            "question_field_logits": self.question(hidden),
        }
