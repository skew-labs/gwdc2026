"""Trainable financial decision architecture for versioned research episodes.

The V2 and typed V3 decision heads train on synthetic data. A separate TRON
research run trains only the temporal branch on observed USDD history; it has
not established allocation skill or been merged into the decision checkpoint.
Outputs remain predictions; exact allocations and external actions require
separate checks. Text embeddings come from an independent encoder, not Qwen.
"""

from dataclasses import dataclass

import torch
from torch import Tensor, nn


@dataclass(frozen=True)
class Config:
    width: int = 256
    heads: int = 8
    layers: int = 4
    plans: int = 4
    numeric_features: int = 16
    time_features: int = 3
    asset_features: int = 16
    goal_features: int = 16
    constraint_features: int = 16
    text_width: int = 384
    relation_types: int = 6
    question_fields: int = 16

    def __post_init__(self):
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("All dimensions must be positive")
        if self.width % self.heads:
            raise ValueError("width must be divisible by heads")


def feedforward(width: int) -> nn.Sequential:
    return nn.Sequential(
        nn.LayerNorm(width), nn.Linear(width, width * 4), nn.GELU(),
        nn.Linear(width * 4, width),
    )


class CrossAttention(nn.Module):
    def __init__(self, cfg: Config):
        super().__init__()
        self.query_norm = nn.LayerNorm(cfg.width)
        self.memory_norm = nn.LayerNorm(cfg.width)
        self.attention = nn.MultiheadAttention(
            cfg.width, cfg.heads, dropout=0.0, batch_first=True,
        )

    def forward(self, query: Tensor, memory: Tensor, valid: Tensor) -> Tensor:
        # Caller supplies at least one valid memory entry (including null tokens).
        normalized = self.memory_norm(memory)
        result, _ = self.attention(
            self.query_norm(query), normalized, normalized,
            key_padding_mask=~valid, need_weights=False,
        )
        return query + result


class FinancialBlock(nn.Module):
    """Joint asset, relationship, evidence, and constraint representation."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.heads = cfg.heads
        self.norm = nn.LayerNorm(cfg.width)
        self.relation_bias = nn.Linear(cfg.relation_types, cfg.heads, bias=False)
        self.asset_attention = nn.MultiheadAttention(
            cfg.width, cfg.heads, dropout=0.0, batch_first=True,
        )
        self.read_evidence = CrossAttention(cfg)
        self.constraints_read_assets = CrossAttention(cfg)
        self.assets_read_constraints = CrossAttention(cfg)
        self.asset_ff = feedforward(cfg.width)
        self.constraint_ff = feedforward(cfg.width)

    def forward(self, assets, valid_assets, relations, evidence,
                valid_evidence, constraints, valid_constraints):
        batch, count, _ = assets.shape
        # Multi-edge features permit shared-token AND shared-protocol relations.
        bias = self.relation_bias(relations).permute(0, 3, 1, 2)
        bias = bias.masked_fill(~valid_assets[:, None, None, :], float("-inf"))
        bias = bias.reshape(batch * self.heads, count, count)
        normalized = self.norm(assets)
        mixed, _ = self.asset_attention(
            normalized, normalized, normalized, attn_mask=bias,
            need_weights=False,
        )
        assets = assets + mixed
        assets = self.read_evidence(assets, evidence, valid_evidence)
        constraints = self.constraints_read_assets(
            constraints, assets, valid_assets,
        )
        constraints = constraints + self.constraint_ff(constraints)
        assets = self.assets_read_constraints(assets, constraints, valid_constraints)
        assets = assets + self.asset_ff(assets)
        return assets.masked_fill(~valid_assets[..., None], 0), constraints


class FinancialDecisionCore(nn.Module):
    """Prototype forward pass; default initialization has NO financial skill.

    Floating inputs must be finite, padded entries zeroed, and masks Boolean.
    Temporal entries must be chronological; feature transforms are fitted only
    on training data. All feature axes require a versioned meaning/unit contract.

    Shapes, with B=batch, N=assets, T=time, E=evidence, C=constraints:
      history: [B,N,T,numeric_features]; history_valid: [B,N,T]
      times: [B,N,T,time_features]; asset_features: [B,N,asset_features]
      asset_valid: [B,N]; relations: [B,N,N,relation_types]
      evidence_embeddings: [B,E,text_width]; evidence_valid: [B,E]
      constraints: [B,C,constraint_features]; constraint_valid: [B,C]
      goal_features: [B,goal_features]; user_embedding: [B,text_width]

    Text/evidence collection and financial feature construction are not included.
    """

    def __init__(self, cfg: Config = Config()):
        super().__init__()
        self.cfg = cfg
        self.history_projection = nn.Linear(cfg.numeric_features + cfg.time_features, cfg.width)
        self.asset_projection = nn.Linear(cfg.asset_features, cfg.width)
        self.goal_projection = nn.Linear(cfg.goal_features, cfg.width)
        self.text_projection = nn.Linear(cfg.text_width, cfg.width)
        self.constraint_projection = nn.Linear(cfg.constraint_features, cfg.width)
        self.temporal_attention = nn.MultiheadAttention(
            cfg.width, cfg.heads, dropout=0.0, batch_first=True,
        )
        self.temporal_norm = nn.LayerNorm(cfg.width)
        self.temporal_null = nn.Parameter(torch.randn(1, 1, cfg.width) * 0.02)
        self.evidence_null = nn.Parameter(torch.randn(1, 1, cfg.width) * 0.02)
        self.cash_token = nn.Parameter(torch.randn(1, 1, cfg.width) * 0.02)
        self.plan_queries = nn.Parameter(torch.randn(1, cfg.plans, cfg.width) * 0.02)
        self.blocks = nn.ModuleList(FinancialBlock(cfg) for _ in range(cfg.layers))
        self.plan_attention = CrossAttention(cfg)
        self.plan_constraint_attention = CrossAttention(cfg)
        self.allocation_query = nn.Linear(cfg.width, cfg.width, bias=False)
        self.allocation_key = nn.Linear(cfg.width, cfg.width, bias=False)
        self.evidence_query = nn.Linear(cfg.width, cfg.width, bias=False)
        self.dependency_query = nn.Linear(cfg.width, cfg.width, bias=False)
        self.plan_value = nn.Linear(cfg.width, 1)
        self.mode = nn.Linear(cfg.width, 3)  # propose, ask, abstain; not authorization
        self.question = nn.Linear(cfg.width, cfg.question_fields)

    def temporal_encode(self, history: Tensor, times: Tensor, valid: Tensor) -> Tensor:
        batch, assets, steps, _ = history.shape
        embedded = self.history_projection(torch.cat((history, times), dim=-1))
        embedded = embedded.masked_fill(~valid[..., None], 0)
        embedded = embedded.reshape(batch * assets, steps, self.cfg.width)
        memory = torch.cat((self.temporal_null.expand(batch * assets, -1, -1), embedded), dim=1)
        valid = torch.cat((
            torch.ones(batch * assets, 1, dtype=torch.bool, device=history.device),
            valid.reshape(batch * assets, steps),
        ), dim=1)
        causal_mask = torch.ones(steps + 1, steps + 1, dtype=torch.bool,
                                 device=history.device).triu(1)
        normalized = self.temporal_norm(memory)
        attended, _ = self.temporal_attention(
            normalized, normalized, normalized, attn_mask=causal_mask,
            key_padding_mask=~valid, need_weights=False,
        )
        memory = (memory + attended).masked_fill(~valid[..., None], 0)
        pooled = memory.sum(1) / valid.sum(1, keepdim=True).to(memory.dtype)
        return pooled.reshape(batch, assets, self.cfg.width)

    def forward(self, *, history, history_valid, times, asset_features,
                asset_valid, relations, evidence_embeddings, evidence_valid,
                constraints, constraint_valid, goal_features, user_embedding):
        batch, count = asset_valid.shape
        goal = self.goal_projection(goal_features) + self.text_projection(user_embedding)
        assets = self.temporal_encode(history, times, history_valid)
        assets = assets + self.asset_projection(asset_features) + goal[:, None, :]
        # Index N is the wallet-hold candidate; no interest/safety is implied.
        assets = torch.cat((assets, self.cash_token.expand(batch, -1, -1) + goal[:, None, :]), dim=1)
        valid_assets = torch.cat((asset_valid, torch.ones(batch, 1, dtype=torch.bool,
                                                       device=assets.device)), dim=1)
        padded_relations = nn.functional.pad(relations, (0, 0, 0, 1, 0, 1))
        evidence = torch.cat((self.text_projection(evidence_embeddings),
                              self.evidence_null.expand(batch, -1, -1)), dim=1)
        valid_evidence = torch.cat((evidence_valid, torch.ones(batch, 1, dtype=torch.bool,
                                                             device=assets.device)), dim=1)
        # Appended goal token also handles the empty-constraint case.
        constraint_memory = torch.cat((self.constraint_projection(constraints), goal[:, None, :]), dim=1)
        valid_constraints = torch.cat((constraint_valid, torch.ones(batch, 1, dtype=torch.bool,
                                                                  device=assets.device)), dim=1)
        for block in self.blocks:
            assets, constraint_memory = block(
                assets, valid_assets, padded_relations, evidence, valid_evidence,
                constraint_memory, valid_constraints,
            )
        plans = self.plan_attention(self.plan_queries.expand(batch, -1, -1), assets, valid_assets)
        plans = self.plan_constraint_attention(plans, constraint_memory, valid_constraints)
        scale = self.cfg.width ** -0.5
        allocation = torch.einsum("bkd,bnd->bkn", self.allocation_query(plans),
                                  self.allocation_key(assets)) * scale
        evidence_logits = torch.einsum("bkd,bed->bke", self.evidence_query(plans), evidence) * scale
        dependency_logits = torch.einsum("bkd,bcd->bkc", self.dependency_query(plans), constraint_memory) * scale
        summary = plans.mean(dim=1)
        return {
            "allocation_logits": allocation.masked_fill(~valid_assets[:, None, :], float("-inf")),
            "evidence_logits": evidence_logits.masked_fill(~valid_evidence[:, None, :], float("-inf")),
            "dependency_logits": dependency_logits.masked_fill(~valid_constraints[:, None, :], float("-inf")),
            "plan_scores": self.plan_value(plans).squeeze(-1),
            "mode_logits": self.mode(summary),
            "question_field_logits": self.question(summary),
        }
