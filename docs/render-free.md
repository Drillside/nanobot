# Your Telegram assistant on Render Free

This fork of [HKUDS/nanobot](https://github.com/HKUDS/nanobot) is prepared for Drillside's free Render web service. It uses Codex OAuth for your eligible ChatGPT Plus/Pro account, with no configured API-key fallback. Account eligibility, model availability and your plan's usage limits still apply. A completed model reply is the final account access check.

## Features

| Feature | Setup |
|---|---|
| Formatted Telegram replies, code blocks, files and photo input | Enable Telegram in Channels and pair your own account. The selected chat model must support images. |
| Image generation and editing | The Codex image provider is enabled; availability is subject to your account and the selected image model. |
| Spoken replies | The `speak` tool uses Microsoft Edge's online neural voice service and FFmpeg to create a Telegram-compatible Opus voice note. Ask for a voice reply. No speech API key is configured. This public voice service is best-effort, with no guaranteed quota or uptime. |
| Voice-note input | Set a transcription provider under Settings → Capabilities → Transcription and its credential under Settings → Models. For example, Groq offers a limited free developer tier; verify your current quota before enabling it. This is separate from ChatGPT subscription access. |
| Recent conversation history, memory and sign-in | Encrypted checkpoints in your own free Upstash Redis database. |

The upstream code is MIT-licensed. Normal text conversations do not depend on the speech service. This is a separate assistant: your existing ChatGPT conversations and this Codex thread are not automatically imported.

## First setup

1. Create a Redis database on the **Free** plan in [Upstash](https://console.upstash.com/). Keep the writable REST token private. Choose your own account-owned database, not an anonymous three-day database.
2. In this Render service's **Environment** page, privately enter `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`. Use the HTTPS REST URL, not a `redis://` connection string. Keep the existing `STATE_ENCRYPTION_KEY`, `STATE_NAMESPACE` and `NANOBOT_WEB_TOKEN` values unchanged, then save and deploy.
3. Open your service's `onrender.com` page. Enter the private `NANOBOT_WEB_TOKEN` from Render's Environment page to open the administration UI. Keep it out of chat, screenshots and source control.
4. In provider settings, connect **OpenAI Codex** using your own ChatGPT account. Complete the browser authorization promptly; the UI supports the remote/headless callback flow. Choose an account-supported model if the initial model is unavailable.
5. Create a fresh Telegram bot through [@BotFather](https://t.me/BotFather), then add its token privately under **Channels → Telegram**. Keep the channel in pairing-only mode: leave `allowFrom` empty, never `*`. Enable Telegram and complete pairing from your own private Telegram chat.
6. Send a normal text question, then a photo. Ask “Reply with a voice note” to test speech output. Enable transcription only after its provider is configured, then test a short incoming voice note. Image generation should be checked separately with a simple image request.

Before storage is configured, the deployment shows a setup page and starts no AI gateway. It does not store user sign-in credentials on an ephemeral-only disk.

## Free service behavior

The Render configuration creates **one Free web service and no paid disk, worker or database**. Upstash is an external service you configure separately. Stay within both services' free-tier allowances, and do not opt into a paid database plan if you want zero hosting charges. Existing ChatGPT subscription charges remain yours.

An uptime monitor can request `/healthz`. This reports the launcher's status, including `setup_required` and `starting`; `/readyz` returns HTTP 200 only after the actual gateway is ready. An uptime monitor cannot guarantee availability or preserve local files by itself.

The public launcher proxies the administration UI without bypassing its authentication. It starts only one gateway holding the Redis lease. A replacement launcher can serve a starting page while Render drains the old instance; it starts its gateway after the old instance checkpoints and releases the lease. Automatic deploys are disabled. Use a manual deploy for reviewed updates.

## What is backed up

The launcher checks for changes about every three seconds. It creates encrypted checkpoints containing configuration, Codex's renewable credentials, SQLite session databases, memory and the workspace. SQLite's backup API includes committed WAL transactions. Only the active lease owner can publish a checkpoint; the encrypted value is replaced atomically in Redis. Normal shutdown attempts a final checkpoint before releasing the lease.

This is checkpoint recovery, not a guarantee of zero data loss on abrupt termination. A hard kill can lose changes since the last successful checkpoint, and a kill during token renewal can require signing in again. Keep the encryption secret and namespace unchanged: changing either prevents normal recovery.

Checkpoints exclude logs, dependency caches, `.git`, `.venv`, symlinks, runtime lock/PID files and media files larger than 2 MiB. Large photos/documents may need to be sent again after a restart. Each checkpoint is bounded to 32 MiB before compression and 6 MiB after compression to stay below the storage service's request limit. This is intended for one person's small bot, not unbounded file hosting. Storage quota/size failures stop the gateway rather than silently switching to an ephemeral-only sign-in.

The encryption key remains in Render; Upstash receives the encrypted checkpoint. Anyone with both the database credential and encryption key can read stored credentials and history. Revoking access in ChatGPT Settings disables the connected account session. Never put any of these secrets into this repository.

## Relevant settings

| Environment variable | Purpose |
|---|---|
| `NANOBOT_RENDER_FREE=1` | Enable this launcher's checkpoint/lease/proxy flow. |
| `NANOBOT_WEB_TOKEN` | Private administration UI password. |
| `STATE_ENCRYPTION_KEY` | Strong random encryption secret. Keep the original value. |
| `STATE_NAMESPACE` | Redis key namespace for this one bot. Keep it stable. |
| `UPSTASH_REDIS_REST_URL` | Your account-owned database's HTTPS REST URL. |
| `UPSTASH_REDIS_REST_TOKEN` | Its writable REST token. |
| `PORT=8765` | Public launcher port; the gateway UI is private on port 8766. |

Speech is configured under `tools.speech`. It is enabled in this deployment profile and disabled by default in normal installations. Change its voice in the saved config if desired; its default is `en-US-AriaNeural`. Speech input is deliberately configured separately so no paid transcription fallback is implied.

## Verification

Focused checks exercise speech artifact delivery, bounded input, network policy, media path containment, encrypted checkpoint round-trips, live SQLite WAL recovery, malicious archive paths, lease handover, stale writer rejection, private UI authorization and WebSocket proxying. Your own ChatGPT inference, Telegram delivery and speech provider access still need live testing after private setup.

Official references: [Render Free](https://render.com/docs/free), [Upstash pricing](https://upstash.com/pricing/redis), [Nanobot Telegram setup](guides/telegram-ai-agent.md), [Codex provider setup](configuration.md#providers-and-models), [image generation](image-generation.md).
