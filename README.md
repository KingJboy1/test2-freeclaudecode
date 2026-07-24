# PCC (Personal Claude Code) — Proxy

**Local proxy** that connects Claude Code to any compatible AI provider — from cloud APIs (NVIDIA NIM, OpenRouter, GitHub Models, AWS Bedrock) to local servers (LM Studio, Ollama, llama.cpp).

## Features

- **25+ providers** — NVIDIA NIM, OpenRouter, LM Studio, Ollama, llama.cpp, GitHub Models, AWS Bedrock, Google Vertex, Groq, DeepSeek, and more.
- **Key pool** — assign multiple API keys per provider; the proxy cycles keys with automatic rate-limit and concurrency management.
- **Image / vision** — images are forwarded to vision-capable models; non-vision models receive a graceful refusal.
- **Streaming, tool use, reasoning** — fully preserved through the proxy.
- **Admin UI** — local web interface at `http://127.0.0.1:<port>/admin` for provider configuration, credential validation, model browsing, and model management.
- **My Models** — add, delete, and switch custom model refs live from the Admin UI without restarting the proxy.
- **Discord & Telegram bots** — run coding agents through messaging platforms.
- **Claude Code model picker** — preserved end-to-end.

## Quick Start

```bash
# Install (Linux / macOS)
curl -fsSL https://raw.githubusercontent.com/King-Jboy/kingjboy-claude-code/main/scripts/install.sh | sh

# Or on Windows (PowerShell):
# irm https://raw.githubusercontent.com/King-Jboy/kingjboy-claude-code/main/scripts/install.ps1 | iex

# Set your first provider key and start the server
export NVIDIA_NIM_API_KEY=nvapi-...
pcc-server
```

Open `http://127.0.0.1:8082/admin` (the port is printed at startup) to configure additional providers, set model mappings, and add your own models.

## Choose A Provider
| Provider | Type | Example model ref |
|---|---|---|
| [AWS Bedrock](https://aws.amazon.com/bedrock/) | cloud | `bedrock/anthropic.claude-v2` |
| [Cerebras](https://cerebras.ai) | cloud, fast | `cerebras/llama3.1-8b` |
| [Cloudflare](https://developers.cloudflare.com/workers-ai/) | cloud | `cloudflare/@cf/meta/llama-3.1-8b` |
| [Cohere](https://cohere.com) | cloud | `cohere/command-r-plus` |
| [DeepSeek](https://platform.deepseek.com) | cloud | `deepseek/deepseek-chat` |
| [Fireworks AI](https://fireworks.ai) | cloud, fast | `fireworks/accounts/fireworks/models/llama-v3p1-8b` |
| [Gemini](https://aistudio.google.com/apikey) | cloud | `gemini/gemini-2.0-flash-exp` |
| [GitHub Models](https://github.com/marketplace/models) | cloud | `github_models/gpt-4o` |
| [Groq](https://groq.com) | cloud, fast | `groq/llama3-70b-8192` |
| [Hugging Face](https://huggingface.co) | cloud | `huggingface/meta-llama/Meta-Llama-3.1-8B` |
| [Kimi](https://kimi.com) | cloud | `kimi/moonshot-v1-8k` |
| [Kimi Code](https://kimi.com/coding) | cloud | `kimi_code/kimi-coding-latest` |
| [llama.cpp](https://github.com/ggerganov/llama.cpp) | local | `llamacpp/model-name` |
| [LM Studio](https://lmstudio.ai) | local | `lmstudio/model-identifier` |
| [MiniMax](https://minimax.io) | cloud | `minimax/MiniMax-Text-01` |
| [Mistral AI](https://console.mistral.ai) | cloud | `mistral/mistral-large-latest` |
| [Mistral Codestral](https://codestral.mistral.ai) | cloud | `mistral_codestral/codestral-latest` |
| [NVIDIA NIM](https://build.nvidia.com/settings/api-keys) | cloud, vision | `nvidia_nim/meta/llama-3.1-8b-instruct` |
| [Ollama](https://ollama.com) | local | `ollama/llama3.1` |
| [Ollama Cloud](https://ollama.com) | cloud | `ollama_cloud/llama3.1` |
| [OpenRouter](https://openrouter.ai/keys) | cloud, vision, reasoning | `open_router/meta-llama/llama-3.1-8b-instruct` |
| [SambaNova](https://sambanova.ai) | cloud | `sambanova/Meta-Llama-3.1-8B-Instruct` |
| [Vercel AI Gateway](https://ai-gateway.vercel.sh) | proxy | `vercel/provider/model` |
| [Google Vertex](https://cloud.google.com/vertex-ai) | cloud | `vertex/anthropic-claude-sonnet` |
| [Wafer](https://wafer.ai) | cloud | `wafer/pass-1` |
| [Z.ai](https://z.ai) | cloud | `zai/glm-4-coding-plan` |


## Key Pool

The proxy supports **multi-key rotation** for rate-limited providers (NVIDIA NIM, OpenRouter). Configure multiple API keys as a JSON array:

```env
NVIDIA_NIM_API_KEYS='["nvapi-key1","nvapi-key2","nvapi-key3"]'
```

The `KeyPool` rounds requests across keys with per-key RPM pacing. If a key gets rate-limited (429), it's cooled down and the next key is used. Transient 5xx errors use a short 30-second cooldown instead of the full 3-hour cooldown, preventing brief upstream hiccups from disabling all keys.

## Image / Vision Support

Images are forwarded to vision-capable models automatically. When a model doesn't support vision, the proxy returns a friendly assistant reply asking the user to send the content as text — never a broken error.

## Admin UI

Start the proxy with `pcc-server` and open `http://127.0.0.1:8082/admin`. From the Admin UI you can:

- Configure provider API keys and base URLs
- Test provider connectivity
- Browse discovered models
- Set model routing (which model to use for Fable, Opus, Sonnet, Haiku)
- **Manage your own model list** — add custom model refs in the **My Models** tab, delete them, and they appear in every model combobox immediately
- Change provider and model settings without restarting the proxy

## Clients

| Client | Launch command |
|---|---|
| **[Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)** | `pcc-claude` |

## Messaging Integrations

Run coding agents through **Telegram** or **Discord**. Configure your bot tokens in the Admin UI or `.env` file.

## Uninstall

Removes the Free Claude Code uv tool, deletes ~/.fcc/, and verifies every FCC command is gone.

```bash
curl -fsSL https://raw.githubusercontent.com/King-Jboy/kingjboy-claude-code/main/scripts/uninstall.sh | sh
```

```powershell
& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/King-Jboy/kingjboy-claude-code/main/scripts/uninstall.ps1")))
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system design, request flow, provider abstraction, and configuration model.
