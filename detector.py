"""A free, local estimate of whether text reads as AI-written to a detector.

The model is Mozilla Fakespot's roberta-base-ai-text-detection-v1 (Apache-2.0), a
classifier trained on text from current language models. It was picked because it
agreed with GPTZero on the cases that mattered: a Headstart draft GPTZero called 100%
AI scored AI here too, and the same post after humanizeai.pro, which GPTZero called
100% human, scored human. A plain perplexity score from GPT-2 was tried first and
rejected: it rated that Headstart draft as more human than the average human answer.

It runs in numpy, not PyTorch, so the server needs no machine learning libraries: the
weights are one 500 MB safetensors file, fetched from Hugging Face the first time it is
used and kept in DATA_DIR, which is the Railway volume in production. One forward pass
over 512 tokens takes about a second on a CPU.

Loaded, the weights hold about 500 MB of RAM, and Railway bills RAM by the hour. So the
model is dropped from memory after IDLE_SECONDS without a score and read back from disk,
a few seconds, the next time it is needed. The file itself stays on the volume.

This is an estimate. GPTZero and Turnitin run their own models, so a pass here is
likely but not guaranteed there.
"""
import json
import os
import re
import sys
import threading
import time
from html import unescape

import httpx
import numpy as np

import db

REPO = "fakespot-ai/roberta-base-ai-text-detection-v1"
FILES = ("config.json", "tokenizer.json", "model.safetensors")
MODEL_DIR = os.path.join(db.DATA_DIR, "models", "fakespot-roberta")
MAX_TOKENS = 512
IDLE_SECONDS = 15 * 60

_lock = threading.Lock()
_model = None
_state = {"status": "absent", "error": None}   # absent, loading, ready, failed
_last_used = 0.0


def _download():
    os.makedirs(MODEL_DIR, exist_ok=True)
    for name in FILES:
        dest = os.path.join(MODEL_DIR, name)
        if os.path.exists(dest):
            continue
        url = f"https://huggingface.co/{REPO}/resolve/main/{name}"
        part = dest + ".part"
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
        except BaseException:
            if os.path.exists(part):  # a failed fetch should not keep holding disk space
                os.remove(part)
            raise
        os.replace(part, dest)        # never leave a half file where a whole one is expected


def _load():
    from safetensors.numpy import load_file
    from tokenizers import Tokenizer

    cfg = json.load(open(os.path.join(MODEL_DIR, "config.json")))
    w = load_file(os.path.join(MODEL_DIR, "model.safetensors"))
    tok = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
    tok.enable_truncation(MAX_TOKENS)
    tok.no_padding()

    def lin(prefix):
        # stored as (out, in). .T is a view, not a copy: the matrix multiply reads it
        # transposed in place, which keeps peak memory near the 500 MB of weights
        # instead of nearly doubling it on a small server.
        return (w[prefix + ".weight"].T, w[prefix + ".bias"])

    layers = []
    for i in range(cfg["num_hidden_layers"]):
        p = f"roberta.encoder.layer.{i}."
        layers.append({
            "q": lin(p + "attention.self.query"), "k": lin(p + "attention.self.key"),
            "v": lin(p + "attention.self.value"), "o": lin(p + "attention.output.dense"),
            "ln1": (w[p + "attention.output.LayerNorm.weight"], w[p + "attention.output.LayerNorm.bias"]),
            "up": lin(p + "intermediate.dense"), "down": lin(p + "output.dense"),
            "ln2": (w[p + "output.LayerNorm.weight"], w[p + "output.LayerNorm.bias"]),
        })
    return {
        "cfg": cfg, "tok": tok, "layers": layers,
        "word": w["roberta.embeddings.word_embeddings.weight"],
        "pos": w["roberta.embeddings.position_embeddings.weight"],
        "type": w["roberta.embeddings.token_type_embeddings.weight"][0],
        "ln0": (w["roberta.embeddings.LayerNorm.weight"], w["roberta.embeddings.LayerNorm.bias"]),
        "cls": lin("classifier.dense"), "out": lin("classifier.out_proj"),
    }


def prepare(background=True):
    """Fetch and load the model once. Safe to call on every request."""
    def work():
        global _model
        with _lock:
            if _model is not None:
                return
            _state.update(status="loading", error=None)
            try:
                _download()
                _model = _load()
                _touch()
                _state["status"] = "ready"
                threading.Thread(target=_unload_when_idle, daemon=True).start()
            except Exception as e:
                _state.update(status="failed", error=f"{type(e).__name__}: {e}")
                print(f"detector: could not load the model: {_state['error']}", file=sys.stderr)
    if _state["status"] == "ready":
        return
    if _state["status"] == "loading":
        if not background:
            with _lock:       # held for the whole fetch and load: this waits for it
                pass
        return
    if background:
        threading.Thread(target=work, daemon=True).start()
    else:
        work()


def _touch():
    global _last_used
    _last_used = time.monotonic()


def _unload_when_idle():
    """Drop the model once nothing has scored with it for IDLE_SECONDS. A score already
    running keeps its own reference, so it finishes on the weights it started with."""
    global _model
    while True:
        time.sleep(60)
        with _lock:
            if _model is None:
                return
            if time.monotonic() - _last_used >= IDLE_SECONDS:
                _model = None
                _state.update(status="absent", error=None)
                return


def status():
    return dict(_state)


# ---------------------------------------------------------------------------
# The forward pass
# ---------------------------------------------------------------------------
def _layer_norm(x, g, b, eps):
    mu = x.mean(-1, keepdims=True)
    var = ((x - mu) ** 2).mean(-1, keepdims=True)
    return (x - mu) / np.sqrt(var + eps) * g + b


def _erf(x):
    # Abramowitz and Stegun 7.1.26, good to about 1e-7, because numpy has no erf and the
    # model was trained with the exact GELU rather than the tanh approximation.
    s = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
               + 0.254829592) * t * np.exp(-x * x)
    return s * y


def _gelu(x):
    return 0.5 * x * (1.0 + _erf(x / np.sqrt(2.0)))


def _softmax(x):
    x = x - x.max(-1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(-1, keepdims=True)


def _ai_probability(m, ids):
    cfg = m["cfg"]
    eps = cfg["layer_norm_eps"]
    heads = cfg["num_attention_heads"]
    n = len(ids)
    pad = cfg["pad_token_id"]
    pos_ids = np.arange(pad + 1, pad + 1 + n)          # RoBERTa counts positions from padding_idx + 1
    x = m["word"][ids] + m["pos"][pos_ids] + m["type"]
    x = _layer_norm(x, *m["ln0"], eps).astype(np.float32)
    d = x.shape[1] // heads
    for L in m["layers"]:
        q = (x @ L["q"][0] + L["q"][1]).reshape(n, heads, d).transpose(1, 0, 2)
        k = (x @ L["k"][0] + L["k"][1]).reshape(n, heads, d).transpose(1, 0, 2)
        v = (x @ L["v"][0] + L["v"][1]).reshape(n, heads, d).transpose(1, 0, 2)
        a = _softmax(q @ k.transpose(0, 2, 1) / np.sqrt(d)) @ v
        a = a.transpose(1, 0, 2).reshape(n, heads * d)
        x = _layer_norm(x + a @ L["o"][0] + L["o"][1], *L["ln1"], eps)
        h = _gelu(x @ L["up"][0] + L["up"][1])
        x = _layer_norm(x + h @ L["down"][0] + L["down"][1], *L["ln2"], eps)
    c = np.tanh(x[0] @ m["cls"][0] + m["cls"][1])
    logits = c @ m["out"][0] + m["out"][1]
    return float(_softmax(logits)[1])                   # label 1 is "AI"


def clean_text(t):
    """The model card's own preprocessing (utils.py in the model repo): markdown and
    line breaks stripped, spaces collapsed. Scores drift without it."""
    t = re.sub(r"```.*?```", "", t, flags=re.DOTALL)
    t = re.sub(r"`[^`]*`", "", t)
    t = re.sub(r"!\[.*?\]\(.*?\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\(.*?\)", r"\1", t)
    t = re.sub(r"(\*\*|__)(.*?)\1", r"\2", t)
    t = re.sub(r"(\*|_)(.*?)\1", r"\2", t)
    t = re.sub(r"#+ ", "", t)
    t = re.sub(r"^>.*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"^(\s*[-*+]|\d+\.)\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*[-*_]{3,}\s*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"\|.*?\|", "", t)
    t = re.sub(r"<.*?>", "", t)
    t = unescape(t)
    for ch in ("\n", "\t", "\r"):
        t = t.replace(ch, " ")
    t = t.replace(" ,", ",")
    return re.sub(" +", " ", t).strip()


def score_each(paragraphs):
    """The AI probability of each paragraph on its own, or None if not ready."""
    if _model is None:
        prepare()
        return None
    m = _model
    _touch()
    return [round(_ai_probability(m, np.array(m["tok"].encode(clean_text(p)).ids)), 3)
            for p in paragraphs]


def score(text):
    """{"ai": 0..1 for the whole text, "paragraphs": [{"text", "ai"}]} or None if the
    model is not ready yet. A text longer than 512 tokens is scored in windows and
    averaged by length, since the model reads at most 512 at once."""
    if _model is None:
        prepare()
        return None
    m = _model
    _touch()
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]

    def one(t):
        enc_ids = m["tok"].encode(clean_text(t)).ids
        return _ai_probability(m, np.array(enc_ids)), len(enc_ids)

    # the whole text, in windows of whole paragraphs that fit
    windows, cur = [], ""
    for p in paras:
        trial = (cur + "\n\n" + p) if cur else p
        if cur and len(m["tok"].encode(clean_text(trial)).ids) >= MAX_TOKENS:
            windows.append(cur)
            cur = p
        else:
            cur = trial
    if cur:
        windows.append(cur)
    scored = [one(t) for t in windows] or [(0.0, 1)]
    total = sum(n for _, n in scored)
    overall = sum(p * n for p, n in scored) / total

    per = [{"text": p, "ai": round(one(p)[0], 3)} for p in paras] if len(paras) > 1 else []
    return {"ai": round(overall, 3), "paragraphs": per}
