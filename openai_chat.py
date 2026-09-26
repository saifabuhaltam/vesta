"""OpenAI's models for Headstart chats, beside Claude's.

Chats only. Syllabus import, Humanizer and the one-shot tools lean on Claude-only
features (PDF input, adaptive thinking, explicit cache breakpoints) and stay on Claude.

A chat turn here yields the same events as `ai.stream_claude_chat` (("thinking", s),
("text", s), then ("done", usage) or ("error", payload)), so the thread code and the
page read either one the same way. The standing instruction, the student's writing
sample and the course material go in `instructions`, which never changes within a
chat, so OpenAI's automatic prompt caching makes later turns cheaper the same way
Claude's cache breakpoint does.

The key is the server's, like the Anthropic one: OPENAI_API_KEY. Without it the models
are not offered, and a chat that still asks for one is refused before anything is sent.
"""
import os

import openai

# Short name the page sends -> OpenAI model id. Prices are in ai.MODEL_PRICES.
MODELS = {
    "gpt-sol": "gpt-6-sol",
    "gpt-luna": "gpt-6-luna",
    "gpt-astra": "gpt-6-astra",
}

# Reasoning counts against max_output_tokens, so leave room for it or a long think
# leaves nothing for the answer.
MIN_OUTPUT_TOKENS = 16000


def available():
    return bool(os.environ.get("OPENAI_API_KEY"))


def is_openai(model):
    return model in MODELS.values()


def stream_chat(conn, cfg, kind, system_blocks, history, fast=True, max_tokens=4000):
    """One thread turn from an OpenAI model, as the same events stream_claude_chat yields."""
    import ai   # imported here: ai imports this module

    request = dict(
        model=cfg["model"],
        instructions="\n\n".join(b["text"] for b in system_blocks),
        input=[{"role": m["role"], "content": m["content"]} for m in history],
        max_output_tokens=max(max_tokens, MIN_OUTPUT_TOKENS),
        # low answers at once; medium thinks first and shows a summary of it, as a
        # thorough Claude turn does
        reasoning={"effort": "low"} if fast else {"effort": "medium", "summary": "auto"},
        # Vesta keeps the conversation itself; nothing needs to live on OpenAI's side
        store=False,
        stream=True,
    )
    # OpenAI reports usage only at the end, so a reply stopped part way is recorded
    # with the input estimated from what was sent.
    est_in = ai.estimate_tokens(request["instructions"] + "".join(m["content"] for m in history))
    produced = 0
    finished = False
    try:
        stream = openai.OpenAI().responses.create(**request)
        final = None
        for ev in stream:
            if ev.type == "response.output_text.delta":
                produced += len(ev.delta)
                yield ("text", ev.delta)
            elif ev.type == "response.reasoning_summary_text.delta":
                produced += len(ev.delta)
                yield ("thinking", ev.delta)
            elif ev.type == "response.completed":
                final = ev.response
        u = getattr(final, "usage", None)
        total_in = getattr(u, "input_tokens", 0) or 0
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        tout = getattr(u, "output_tokens", 0) or ai.estimate_tokens("x" * produced)
        # Recorded the way Claude's are: input_tokens is the uncached part, and the
        # cached part is counted separately. OpenAI's input_tokens includes both.
        tin = max(0, total_in - cached)
        ai.record_usage(conn, kind, cfg["model"], tin, tout, cached, 0)
        finished = True
        yield ("done", {"inputTokens": tin, "outputTokens": tout, "cacheReadTokens": cached,
                        "cacheWriteTokens": 0, "model": cfg["model"],
                        "stopReason": getattr(final, "status", None)})
    except openai.AuthenticationError:
        yield ("error", {"error": "The OpenAI API key is missing or was rejected.", "status": 503})
    except openai.RateLimitError as e:
        out_of_credit = "quota" in str(e).lower()
        yield ("error", {"error": "The OpenAI account is out of credit. Top it up at platform.openai.com."
                         if out_of_credit else "Rate limited by OpenAI. Wait a moment and try again.",
                         "status": 429})
    except openai.APIConnectionError:
        yield ("error", {"error": "Lost the connection to OpenAI.", "status": 502})
    except openai.APIStatusError as e:
        yield ("error", {"error": f"OpenAI error: {e.message}", "status": 502})
    except Exception as e:
        yield ("error", {"error": f"AI call failed ({e}).", "status": 503})
    finally:
        if not finished and produced:
            # stopped part way: still billed for what was written
            ai.record_usage(conn, kind, cfg["model"], est_in, ai.estimate_tokens("x" * produced), 0, 0)
