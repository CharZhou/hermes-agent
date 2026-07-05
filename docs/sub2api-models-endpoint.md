# Sub2API `/v1/models` Response Contract

> Status: proposal for a Hermes-compatible relay catalog.
> This document describes the recommended response shape for a relay that
> fronts multiple model protocols, especially OpenAI Responses and Anthropic
> Messages.

## Goals

`/v1/models` should be the single catalog endpoint that lets clients answer
four questions without hardcoded model tables:

1. Which model IDs are available for this API key?
2. Which wire protocols can each model use?
3. What context and output-token limits should the client budget against?
4. What pricing and tool/reasoning capabilities should the picker display?

The response should remain OpenAI-compatible: unknown fields are allowed, and
clients that only understand `data[].id` should still work.

## Endpoint

```http
GET /v1/models
Authorization: Bearer <api-key>
```

Anthropic-style callers may also use:

```http
GET /v1/models
x-api-key: <api-key>
anthropic-version: 2023-06-01
```

The catalog should be scoped to the API key. If a key can only use Anthropic
accounts, the response should still return only the models that key can route
successfully.

## Top-Level Shape

Return a standard OpenAI-style list:

```json
{
  "object": "list",
  "data": [
    {
      "id": "claude-sonnet-4-6",
      "object": "model"
    }
  ]
}
```

`object` may be `"list"` or a relay-specific string, but `"list"` gives the
broadest OpenAI-client compatibility.

## Required Model Fields

Every entry should include:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Canonical model ID used in inference requests. |
| `object` | string | Prefer `"model"` for OpenAI compatibility. |

`id` must be stable. Do not return a model from `/v1/models` until the same ID
can be used successfully on at least one advertised inference endpoint.

## Strongly Recommended Fields

These fields are what Hermes and similar agent runtimes need for dynamic
routing and token budgeting.

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Human-friendly display name. |
| `context_length` | integer | Maximum input context window in tokens. |
| `max_output_tokens` | integer | Maximum generated output tokens for one response. |
| `supported_protocols` | string[] | Logical transports the model supports. |
| `supported_endpoints` | string[] | Concrete HTTP endpoints the model supports. |
| `supported_parameters` | string[] | Request fields/capabilities accepted by this model. |
| `pricing` | object | Per-token prices, OpenRouter-compatible. |

Hermes already recognizes these context aliases if you need to mirror another
upstream: `context_window`, `context_size`, `max_context_length`,
`max_position_embeddings`, `max_model_len`, `max_input_tokens`,
`max_sequence_length`, `max_seq_len`, `n_ctx_train`, `n_ctx`, and `ctx_size`.
Prefer `context_length` for new relay output.

Hermes recognizes these output-token aliases: `max_completion_tokens`,
`max_output_tokens`, and `max_tokens`. Prefer `max_output_tokens`.

## Protocols and Endpoints

Use `supported_protocols` for client transport selection:

```json
"supported_protocols": ["responses", "anthropic_messages"]
```

Recommended protocol values:

| Value | Meaning |
| --- | --- |
| `responses` | OpenAI-compatible `POST /v1/responses`. |
| `codex_responses` | Alias for Hermes' Responses transport. Optional if `responses` is present. |
| `anthropic_messages` | Anthropic-compatible `POST /v1/messages`. |
| `chat_completions` | OpenAI-compatible `POST /v1/chat/completions`. |
| `embeddings` | Embedding model, not suitable for an agent chat loop. |
| `rerank` | Reranking model, not suitable for an agent chat loop. |

Use `supported_endpoints` for concrete compatibility:

```json
"supported_endpoints": ["/responses", "/v1/messages"]
```

Recommended endpoint values:

| Value | Meaning |
| --- | --- |
| `/responses` | OpenAI Responses endpoint under the configured `/v1` base. |
| `/v1/responses` | Fully qualified path form. Either form is acceptable. |
| `/v1/messages` | Anthropic Messages endpoint. |
| `/chat/completions` | OpenAI Chat Completions endpoint under the configured `/v1` base. |
| `/v1/chat/completions` | Fully qualified path form. |
| `/embeddings` | OpenAI-compatible embeddings endpoint. |

For a model that supports both Responses and Anthropic Messages, advertise both
protocols and endpoints. The client should pick one provider/transport at
session start and keep it stable for the session.

## Supported Parameters

`supported_parameters` should list accepted request capabilities, not every
possible field. For agentic models, include at least:

```json
"supported_parameters": [
  "tools",
  "tool_choice",
  "parallel_tool_calls",
  "stream",
  "max_output_tokens"
]
```

Use these names when applicable:

| Parameter | Meaning |
| --- | --- |
| `tools` | Client-side function/tool definitions are accepted. |
| `tool_choice` | The caller can control tool use. |
| `parallel_tool_calls` | Responses-style parallel tool-call control is accepted. |
| `stream` | Streaming is supported. |
| `reasoning` | Responses-style reasoning object is accepted. |
| `include` | Responses-style include list is accepted. |
| `prompt_cache_key` | Responses prompt-cache routing key is accepted. |
| `temperature` | Sampling temperature is accepted. |
| `top_p` | Top-p sampling is accepted. |
| `response_format` | Structured output / response format is accepted. |
| `thinking` | Anthropic-style thinking parameter is accepted. |
| `context_management` | Anthropic context-management field is accepted. |

If a model cannot use tools, either omit it from agent-facing provider catalogs
or return an explicit list that does not include `tools`. Hermes treats a
missing `supported_parameters` field as unknown and permissive, but an explicit
list without `tools` means "do not show this model for tool-calling agents."

## Pricing

Use the OpenRouter-style `pricing` object. Values should be strings to avoid
floating-point representation drift:

```json
"pricing": {
  "prompt": "0.000003",
  "completion": "0.000015",
  "input_cache_read": "0.0000003",
  "input_cache_write": "0.00000375"
}
```

Prices are per token in USD unless your API explicitly documents otherwise.

Recommended keys:

| Key | Meaning |
| --- | --- |
| `prompt` | Input token price. |
| `completion` | Output token price. |
| `request` | Per-request price, if any. |
| `input_cache_read` | Cached input read token price. |
| `input_cache_write` | Cache creation/write token price. |

Hermes can also normalize common aliases such as `input`, `output`,
`input_cost_per_token`, `output_cost_per_token`, `prompt_token_cost`,
`completion_token_cost`, `cached_prompt`, `cache_read`, and `cache_write`.
Prefer the canonical keys above.

## Capabilities Object

For richer clients, add a nested `capabilities` object. This is optional but
useful when a model supports some advanced features only on a specific
protocol.

```json
"capabilities": {
  "type": "chat",
  "modalities": {
    "input": ["text", "image"],
    "output": ["text"]
  },
  "tools": true,
  "streaming": true,
  "reasoning": {
    "supported": true,
    "efforts": ["low", "medium", "high"]
  },
  "protocols": {
    "responses": {
      "tools": true,
      "reasoning": true,
      "parallel_tool_calls": true,
      "prompt_cache_key": true
    },
    "anthropic_messages": {
      "tools": true,
      "thinking": true,
      "context_management": true
    }
  }
}
```

Keep top-level `supported_protocols`, `supported_endpoints`, and
`supported_parameters` even when `capabilities` is present. The top-level fields
are easier for simple clients to consume.

## Recommended Full Example

```json
{
  "object": "list",
  "data": [
    {
      "id": "claude-sonnet-4-6",
      "object": "model",
      "name": "Claude Sonnet 4.6",
      "owned_by": "anthropic",
      "context_length": 200000,
      "max_output_tokens": 64000,
      "supported_protocols": ["responses", "anthropic_messages"],
      "supported_endpoints": ["/responses", "/v1/messages"],
      "supported_parameters": [
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "stream",
        "reasoning",
        "include",
        "prompt_cache_key",
        "thinking",
        "context_management"
      ],
      "pricing": {
        "prompt": "0.000003",
        "completion": "0.000015",
        "input_cache_read": "0.0000003",
        "input_cache_write": "0.00000375"
      },
      "capabilities": {
        "type": "chat",
        "modalities": {
          "input": ["text", "image"],
          "output": ["text"]
        },
        "tools": true,
        "streaming": true,
        "reasoning": {
          "supported": true,
          "efforts": ["low", "medium", "high"]
        },
        "protocols": {
          "responses": {
            "tools": true,
            "reasoning": true,
            "parallel_tool_calls": true,
            "prompt_cache_key": true
          },
          "anthropic_messages": {
            "tools": true,
            "thinking": true,
            "context_management": true
          }
        }
      }
    },
    {
      "id": "text-embedding-3-small",
      "object": "model",
      "name": "Text Embedding 3 Small",
      "owned_by": "openai",
      "context_length": 8191,
      "supported_protocols": ["embeddings"],
      "supported_endpoints": ["/embeddings"],
      "supported_parameters": [],
      "capabilities": {
        "type": "embedding"
      }
    }
  ]
}
```

## Minimal Acceptable Example

This keeps basic OpenAI-compatible clients working but is not enough for
dynamic provider routing or token budgeting:

```json
{
  "object": "list",
  "data": [
    {
      "id": "claude-sonnet-4-6",
      "object": "model"
    }
  ]
}
```

Use the minimal form only as a fallback. A relay intended for agent runtimes
should return the recommended fields.

## Filtering Rules for Hermes Providers

For a relay exposed as two Hermes provider slugs:

1. `sub2api-responses` should keep models where:
   - `supported_protocols` contains `responses` or `codex_responses`, or
   - `supported_endpoints` contains `/responses` or `/v1/responses`.
2. `sub2api-anthropic` should keep models where:
   - `supported_protocols` contains `anthropic_messages`, or
   - `supported_endpoints` contains `/v1/messages`.
3. If neither protocol nor endpoint fields are present, Hermes may keep the
   model for backward compatibility, but this should be treated as unknown.
4. If `supported_parameters` is present and does not include `tools`, Hermes
   should hide the model from tool-calling agent pickers.
5. Models with `capabilities.type` equal to `embedding`, `rerank`, `image`, or
   `audio` should not appear in chat-agent model pickers unless a dedicated
   workflow asks for that modality.

## Stability and Error Handling

- Do not list a model for an API key unless that key can route it.
- If an upstream account pool is exhausted, prefer removing the model from the
  key-scoped catalog or returning a model-level availability flag instead of
  listing a model that fails every request.
- Additive fields are safe; clients should ignore unknown keys.
- Avoid changing a model's `id`. If you add aliases, expose them through a
  separate field such as `aliases`, not by replacing the canonical ID.
- If a model is temporarily unavailable, consider:

```json
"availability": {
  "status": "degraded",
  "reason": "upstream_account_exhausted"
}
```

Clients can use that to hide or de-prioritize the model without losing catalog
metadata.

## Compatibility Checklist

Before treating a model as agent-ready, verify:

- `GET /v1/models` includes the model for the intended API key.
- `POST /v1/responses` succeeds if `responses` is advertised.
- `POST /v1/messages` succeeds if `anthropic_messages` is advertised.
- Streaming works when `stream` is listed.
- Tool calls work when `tools` is listed.
- Tool-result replay works for the advertised protocol:
  - Responses: `function_call` plus `function_call_output`.
  - Anthropic Messages: `tool_use` plus `tool_result`.
- `context_length` and `max_output_tokens` match the actual enforced upstream
  limits.
- `pricing` is in per-token USD units and matches billing.
