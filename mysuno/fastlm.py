"""Быстрый декодер Qwen3 (5Hz LM из ACE-Step) на CUDA Graphs.

Почему: PyTorch-бэкенд ACE-Step декодирует ~25 мс/токен — при 28 слоях это упирается в накладные расходы
запуска ядер (≈1000 мелких операций на токен), а не в пропускную способность памяти. vLLM/flash-attn/Triton
под Windows недоступны, поэтому шаг декодирования захватывается в CUDA Graph вручную.

Что делает:
* берёт веса уже загруженной HF-модели (Qwen3ForCausalLM) — доп. видеопамять только на KV-кэш;
* склеивает q/k/v и gate/up в одну матрицу (исходные веса становятся view'ами на неё — без копий);
* статический KV-кэш и маска, шаг декодирования — один CUDA Graph на (размер батча, ёмкость кэша);
* GQA считается без копирования KV (группы запросов складываются в «длину запроса»);
* семантика та же, что у HF: позиции = индексы в последовательности (включая левый паддинг).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from types import SimpleNamespace
from typing import Iterable

import torch
import torch.nn.functional as F
from loguru import logger

CAP_CLASSES = (1024, 2048, 4096, 8192)   # ёмкости KV-кэша (в токенах)
MARGIN = 8


def _pick_cap(need: int) -> int | None:
    for cap in CAP_CLASSES:
        if need <= cap:
            return cap
    return None


class _Runner:
    """Статические буферы и CUDA Graph для (batch, cap)."""

    def __init__(self, dec: "FastQwen3", batch: int, cap: int) -> None:
        self.dec, self.batch, self.cap = dec, batch, cap
        dev, dt = dec.device, dec.dtype
        with torch.inference_mode():
            shape = (batch, dec.kv_heads, cap, dec.head_dim)
            self.k = [torch.zeros(shape, device=dev, dtype=dt) for _ in range(dec.layers)]
            self.v = [torch.zeros(shape, device=dev, dtype=dt) for _ in range(dec.layers)]
            self.ids = torch.zeros((batch, 1), device=dev, dtype=torch.long)
            self.pos = torch.zeros((1,), device=dev, dtype=torch.long)
            # аддитивная маска: 0 — ключ виден, -inf — нет (паддинг и ещё не записанные позиции)
            self.mask = torch.full((batch, 1, 1, cap), float("-inf"), device=dev, dtype=torch.float32)
        self.graph: torch.cuda.CUDAGraph | None = None
        self.logits: torch.Tensor | None = None
        self.graph_codes: torch.cuda.CUDAGraph | None = None     # вариант с lm_head только по аудио-кодам
        self.logits_codes: torch.Tensor | None = None
        self.nbytes = 2 * dec.layers * batch * dec.kv_heads * cap * dec.head_dim * dec.dtype.itemsize

    def _capture_one(self, codes: bool) -> tuple[torch.cuda.CUDAGraph, torch.Tensor]:
        dec = self.dec
        side = torch.cuda.Stream()
        side.wait_stream(torch.cuda.current_stream())
        with torch.inference_mode(), torch.cuda.stream(side):
            for _ in range(3):  # прогрев cuBLAS вне захвата
                self.pos.fill_(1)
                self.mask.fill_(float("-inf"))
                dec._step(self, codes)
        torch.cuda.current_stream().wait_stream(side)
        torch.cuda.synchronize()
        graph = torch.cuda.CUDAGraph()
        # thread_local: вызовы CUDA из других потоков (HTTP-запросы с mem_get_info) не должны ронять захват
        with torch.inference_mode(), torch.cuda.graph(graph, capture_error_mode="thread_local"):
            logits = dec._step(self, codes)
        torch.cuda.synchronize()
        return graph, logits

    def capture(self) -> float:
        """Захватывает графы. ТОЛЬКО на свежем раннере: прогрев пишет мусор в KV-кэш и двигает позицию."""
        t0 = time.time()
        self.graph, self.logits = self._capture_one(codes=False)
        if self.dec.code_range is not None:
            self.graph_codes, self.logits_codes = self._capture_one(codes=True)
        return time.time() - t0


class FastQwen3:
    def __init__(self, hf_model, kv_budget_gb: float = 2.0) -> None:
        cfg = hf_model.config
        param = next(hf_model.parameters())
        if param.device.type != "cuda" or param.dtype not in (torch.bfloat16, torch.float16):
            raise RuntimeError("FastQwen3 требует CUDA и bf16/fp16")
        if getattr(cfg, "model_type", "") != "qwen3":
            raise RuntimeError(f"неподдерживаемая архитектура: {getattr(cfg, 'model_type', '?')}")
        self.device, self.dtype = param.device, param.dtype
        self.layers = cfg.num_hidden_layers
        self.heads, self.kv_heads = cfg.num_attention_heads, cfg.num_key_value_heads
        self.head_dim = getattr(cfg, "head_dim", cfg.hidden_size // cfg.num_attention_heads)
        self.hidden = cfg.hidden_size
        self.inter = cfg.intermediate_size
        self.eps = cfg.rms_norm_eps
        self.rep = self.heads // self.kv_heads
        self.kv_budget = int(kv_budget_gb * 2**30)

        m = hf_model.model
        if hf_model.lm_head.weight.data_ptr() != m.embed_tokens.weight.data_ptr():
            raise RuntimeError("untied lm_head не поддерживается")
        self.vocab = m.embed_tokens.weight.shape[0]
        self.embed = self._pad_vocab(m.embed_tokens.weight)   # tied с lm_head; строк кратно 128 для быстрого GEMM
        self.norm_w = m.norm.weight
        self.ln1 = [l.input_layernorm.weight for l in m.layers]
        self.ln2 = [l.post_attention_layernorm.weight for l in m.layers]
        # q_norm и k_norm считаем одним вызовом: нормировка без веса + умножение на [H+KV, D]-вес
        # (порядок округлений тот же, что у HF: нормировка → bf16 → умножение на вес)
        self.qk_w = [torch.cat([l.self_attn.q_norm.weight.expand(self.heads, -1),
                                l.self_attn.k_norm.weight.expand(self.kv_heads, -1)]).contiguous() for l in m.layers]
        self.o_w = [l.self_attn.o_proj.weight for l in m.layers]
        self.down_w = [l.mlp.down_proj.weight for l in m.layers]
        self.qkv_w: list[torch.Tensor] = []
        self.gu_w: list[torch.Tensor] = []
        self._fuse(m)

        # таблицы RoPE (как в HF: float32 → dtype модели)
        inv_freq = m.rotary_emb.inv_freq.to(device=self.device, dtype=torch.float32)
        pos = torch.arange(CAP_CLASSES[-1], device=self.device, dtype=torch.float32)
        freqs = pos[:, None] * inv_freq[None, :]
        scale = float(getattr(m.rotary_emb, "attention_scaling", 1.0))
        self.cos = (freqs.cos() * scale).to(self.dtype)         # [maxpos, D/2]
        self.sin = (freqs.sin() * scale).to(self.dtype)

        self._runners: OrderedDict[tuple[int, int], _Runner] = OrderedDict()
        self.stats = {"captures": 0, "capture_s": 0.0}
        self.code_range: tuple[int, int] | None = None   # [lo, hi) — id токенов аудио-кодов; задаётся set_code_range
        self.extra_ids: tuple[int, ...] = ()

    # ------------------------------------------------------------------ веса
    @torch.no_grad()
    def _pad_vocab(self, param: torch.nn.Parameter) -> torch.Tensor:
        """Дополняет матрицу эмбеддингов нулевыми строками до кратного 128 (V=217204 даёт медленное невыровненное
        GEMM для lm_head). Исходный параметр (общий для embed/lm_head) становится view'ом на дополненную матрицу."""
        v, h = param.shape
        padded = torch.zeros((-(-v // 128) * 128, h), device=param.device, dtype=param.dtype)
        padded[:v] = param
        param.data = padded[:v]
        return padded

    @torch.no_grad()
    def _fuse(self, m) -> None:
        for layer in m.layers:
            a, mlp = layer.self_attn, layer.mlp
            qkv = torch.cat([a.q_proj.weight, a.k_proj.weight, a.v_proj.weight], 0).contiguous()
            gu = torch.cat([mlp.gate_proj.weight, mlp.up_proj.weight], 0).contiguous()
            hq, hk = a.q_proj.out_features, a.k_proj.out_features
            a.q_proj.weight.data = qkv[:hq]
            a.k_proj.weight.data = qkv[hq:hq + hk]
            a.v_proj.weight.data = qkv[hq + hk:]
            mlp.gate_proj.weight.data = gu[:self.inter]
            mlp.up_proj.weight.data = gu[self.inter:]
            self.qkv_w.append(qkv)
            self.gu_w.append(gu)

    # ------------------------------------------------------------------ блоки
    def _rms(self, x: torch.Tensor, w: torch.Tensor) -> torch.Tensor:
        return F.rms_norm(x, (x.shape[-1],), w, self.eps)

    @staticmethod
    def _rope(x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor) -> torch.Tensor:
        half = x.shape[-1] // 2
        x1, x2 = x[..., :half], x[..., half:]
        return torch.cat((x1 * cos - x2 * sin, x2 * cos + x1 * sin), dim=-1)

    def _qkv(self, l: int, x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor):
        """x: [B,T,hidden] → q [B,T,H,D], k [B,T,KV,D], v [B,T,KV,D] (после норм и RoPE)."""
        B, T, _ = x.shape
        D = self.head_dim
        qkv = F.linear(self._rms(x, self.ln1[l]), self.qkv_w[l])
        hq, hk = self.heads * D, self.kv_heads * D
        qk = qkv[..., :hq + hk].reshape(B, T, self.heads + self.kv_heads, D)      # вид без копирования
        v = qkv[..., hq + hk:].reshape(B, T, self.kv_heads, D)
        qk = F.rms_norm(qk, (D,), None, self.eps) * self.qk_w[l]
        qk = self._rope(qk, cos, sin)
        return qk[:, :, :self.heads], qk[:, :, self.heads:], v

    def _mlp_out(self, l: int, x: torch.Tensor, attn: torch.Tensor) -> torch.Tensor:
        x = x + F.linear(attn, self.o_w[l])
        gu = F.linear(self._rms(x, self.ln2[l]), self.gu_w[l])
        gate, up = gu.chunk(2, dim=-1)
        return x + F.linear(F.silu(gate) * up, self.down_w[l])

    # ------------------------------------------------------------------ шаг декодирования (захватывается в граф)
    def _step(self, r: _Runner, codes: bool = False) -> torch.Tensor:
        """Один токен. codes=True: lm_head только по строкам аудио-кодов (контигуозный срез матрицы эмбеддингов,
        262 МБ вместо 890 МБ за шаг); остальной словарь = -inf. ACE в фазе кодов использует только эти токены."""
        B = r.batch
        x = F.embedding(r.ids, self.embed)                       # [B,1,hidden]
        cos = self.cos.index_select(0, r.pos).view(1, 1, 1, -1)
        sin = self.sin.index_select(0, r.pos).view(1, 1, 1, -1)
        r.mask.index_fill_(3, r.pos, 0.0)
        scale = self.head_dim ** -0.5
        for l in range(self.layers):
            q, k, v = self._qkv(l, x, cos, sin)
            r.k[l].index_copy_(2, r.pos, k.transpose(1, 2))
            r.v[l].index_copy_(2, r.pos, v.transpose(1, 2))
            # Внимание «вручную»: ядро SDPA mem-efficient на 2 строках запроса работает в ~8 раз дольше предела
            # памяти и линейно растёт с длиной кэша. Группа запросов на KV-голову = «длина запроса».
            qg = q.reshape(B, self.kv_heads, self.rep, self.head_dim) * scale
            s = torch.matmul(qg, r.k[l].transpose(-1, -2)).float() + r.mask       # [B,KV,rep,cap]
            p = torch.softmax(s, dim=-1).to(self.dtype)
            o = torch.matmul(p, r.v[l])                                            # [B,KV,rep,D]
            x = self._mlp_out(l, x, o.reshape(B, 1, self.heads * self.head_dim))
        x = self._rms(x, self.norm_w)
        if codes:
            lo, hi = self.code_range
            logits = torch.full((B, 1, self.vocab), float("-inf"), device=self.device, dtype=self.dtype)
            logits[..., lo:hi] = F.linear(x, self.embed[lo:hi])
            for t in self.extra_ids:   # EOS и т.п.: ACE разрешает их в фазе кодов, нужны настоящие логиты (иначе NaN в CFG)
                logits[..., t:t + 1] = F.linear(x, self.embed[t:t + 1])
        else:
            logits = F.linear(x, self.embed)[..., :self.vocab]                # [B,1,V]
        r.pos.add_(1)
        return logits

    # ------------------------------------------------------------------ prefill (eager)
    @torch.inference_mode()
    def _prefill(self, r: _Runner, ids: torch.Tensor, valid: torch.Tensor) -> torch.Tensor:
        B, L = ids.shape
        x = F.embedding(ids, self.embed)
        cos = self.cos[:L].view(1, L, 1, -1)
        sin = self.sin[:L].view(1, L, 1, -1)
        eye = torch.eye(L, device=self.device, dtype=torch.bool)
        causal = torch.tril(torch.ones(L, L, device=self.device, dtype=torch.bool))
        # строки паддинга не должны быть полностью замаскированы (иначе NaN) — разрешаем диагональ
        mask = (causal[None, None] & valid[:, None, None, :]) | eye[None, None]
        for l in range(self.layers):
            q, k, v = self._qkv(l, x, cos, sin)
            r.k[l][:, :, :L] = k.transpose(1, 2)
            r.v[l][:, :, :L] = v.transpose(1, 2)
            kr = k.transpose(1, 2).repeat_interleave(self.rep, dim=1)
            vr = v.transpose(1, 2).repeat_interleave(self.rep, dim=1)
            o = F.scaled_dot_product_attention(q.transpose(1, 2), kr, vr, attn_mask=mask)
            x = self._mlp_out(l, x, o.transpose(1, 2).reshape(B, L, self.heads * self.head_dim))
        logits = F.linear(self._rms(x[:, -1:], self.norm_w), self.embed)[..., :self.vocab]
        r.mask.fill_(float("-inf"))
        r.mask[:, 0, 0, :L] = torch.zeros((B, L), device=self.device).masked_fill(~valid, float("-inf"))
        r.pos.fill_(L)
        return logits

    # ------------------------------------------------------------------ управление раннерами
    def _runner(self, batch: int, cap: int) -> _Runner:
        key = (batch, cap)
        r = self._runners.get(key)
        if r is None:
            self._evict(extra=2 * self.layers * batch * self.kv_heads * cap * self.head_dim * self.dtype.itemsize)
            r = _Runner(self, batch, cap)
            dt = r.capture()
            self.stats["captures"] += 1
            self.stats["capture_s"] += dt
            logger.debug("CUDA Graph: batch={} cap={} захвачен за {:.2f} с", batch, cap, dt)
            self._runners[key] = r
        self._runners.move_to_end(key)
        return r

    def _evict(self, extra: int) -> None:
        total = sum(r.nbytes for r in self._runners.values()) + extra
        while total > self.kv_budget and self._runners:
            _, old = self._runners.popitem(last=False)
            total -= old.nbytes
            del old

    def set_code_range(self, lo: int, hi: int, extra: Iterable[int] = ()) -> None:
        """Включает режим «только аудио-коды» для lm_head: логиты считаются для токенов [lo, hi) и `extra`
        (EOS), остальной словарь = -inf. Должен совпадать с набором, который ACE разрешает в фазе кодов.
        Вызывать ДО warmup (графы захватываются с учётом диапазона)."""
        extra = tuple(int(t) for t in extra if not lo <= int(t) < hi)
        if not (0 <= lo < hi <= self.vocab) or any(not 0 <= t < self.vocab for t in extra):
            raise ValueError(f"некорректный диапазон кодов {lo}..{hi} / {extra}")
        self.code_range = (lo, hi)
        self.extra_ids = extra
        self._runners.clear()

    def warmup(self, shapes: Iterable[tuple[int, int]] = ((1, 1024), (2, 1024), (2, 2048))) -> float:
        t0 = time.time()
        for batch, cap in shapes:
            self._runner(batch, cap)
        return time.time() - t0

    def free(self) -> None:
        self._runners.clear()

    # ------------------------------------------------------------------ публичный API
    def prefill(self, ids: torch.Tensor, attention_mask: torch.Tensor | None, max_new: int):
        """Возвращает (logits[B,1,V], handle) или None, если запрос не помещается в кэш."""
        B, L = ids.shape
        cap = _pick_cap(L + int(max_new) + MARGIN)
        if cap is None:
            return None
        if 2 * self.layers * B * self.kv_heads * cap * self.head_dim * self.dtype.itemsize > self.kv_budget:
            return None   # не раздуваем VRAM большими батчами — штатный проход
        valid = attention_mask.bool() if attention_mask is not None else torch.ones_like(ids, dtype=torch.bool)
        r = self._runner(B, cap)
        logits = self._prefill(r, ids, valid)
        return logits, SimpleNamespace(runner=r, pos=L)

    @torch.inference_mode()
    def decode(self, handle, token_ids: torch.Tensor, codes: bool = False) -> torch.Tensor:
        """codes=True — фаза генерации аудио-кодов (логиты только по диапазону кодов, остальное -inf)."""
        r: _Runner = handle.runner
        if handle.pos >= r.cap:
            raise RuntimeError("KV-кэш быстрого декодера исчерпан")
        r.ids.copy_(token_ids)
        if codes and r.graph_codes is not None:
            r.graph_codes.replay()
            out = r.logits_codes
        else:
            r.graph.replay()
            out = r.logits
        handle.pos += 1
        return out.clone()
