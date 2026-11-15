from __future__ import annotations

import math

import torch
from torch import nn
from torch.nn import functional as F


class FireA(nn.Module):
    def __init__(self, n_tok: int, n_ctx: int, d: int = 192, layers: int = 4, heads: int = 6, dropout: float = 0.1):
        super().__init__()
        self.config = {"n_tok": n_tok, "n_ctx": n_ctx, "d": d, "layers": layers, "heads": heads, "dropout": dropout}
        self.tok_in = nn.Sequential(nn.Linear(n_tok, d), nn.GELU(), nn.Linear(d, d))
        enc = nn.TransformerEncoderLayer(d, heads, dim_feedforward=4 * d, dropout=dropout, activation="gelu",
                                         batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(enc, layers, enable_nested_tensor=False)
        self.pool_q = nn.Parameter(torch.randn(1, 1, d) / math.sqrt(d))
        self.pool = nn.MultiheadAttention(d, heads, batch_first=True)
        self.norm = nn.LayerNorm(d)
        self.ctx_in = nn.Sequential(nn.Linear(n_ctx, d), nn.GELU(), nn.Linear(d, d))
        self.head = nn.Sequential(nn.LayerNorm(3 * d), nn.Linear(3 * d, d), nn.GELU(), nn.Dropout(dropout),
                                  nn.Linear(d, 2))

    def encode(self, tokens, mask):
        h = self.encoder(self.tok_in(tokens), src_key_padding_mask=mask)
        h = self.norm(h)
        q = self.pool_q.expand(h.shape[0], -1, -1)
        att, _ = self.pool(q, h, h, key_padding_mask=mask)
        keep = (~mask).unsqueeze(-1).float()
        mean = (h * keep).sum(1) / keep.sum(1).clamp(min=1.0)
        return torch.cat([att.squeeze(1), mean], dim=-1)

    def decode(self, z_tok, ctx):
        out = self.head(torch.cat([z_tok, self.ctx_in(ctx)], dim=-1))
        mu = F.softplus(out[:, 0]) + 1e-4
        alpha = F.softplus(out[:, 1]) + 1e-3
        return mu, alpha

    def forward(self, tokens, mask, ctx):
        return self.decode(self.encode(tokens, mask), ctx)


def nb_nll(y, mu, alpha):
    r = 1.0 / alpha
    ll = (torch.lgamma(y + r) - torch.lgamma(r) - torch.lgamma(y + 1.0)
          + r * torch.log(r / (r + mu)) + y * torch.log(mu / (r + mu)))
    return -ll.mean()


def nb_pmf(k_max: int, mu, alpha):
    mu = torch.as_tensor(mu, dtype=torch.float64).unsqueeze(-1)
    alpha = torch.as_tensor(alpha, dtype=torch.float64).unsqueeze(-1)
    r = 1.0 / alpha
    k = torch.arange(k_max + 1, dtype=torch.float64).unsqueeze(0)
    logp = (torch.lgamma(k + r) - torch.lgamma(r) - torch.lgamma(k + 1.0)
            + r * torch.log(r / (r + mu)) + k * torch.log(mu / (r + mu)))
    return logp.exp()


def count_params(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())
