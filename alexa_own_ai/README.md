# Alexa Own AI — low-memory multilingual assistant

This copy is maintained in the AlexaXbot repository so the bot and its connected microservice source can be reviewed together. The existing Music repository copy is left untouched; the currently deployed EthioDeploy service is not switched automatically.

A lightweight Python HTTP service designed for a small container. The service
keeps conversation state in SQLite and uses Cloudflare Workers AI for hosted
inference; model weights are not downloaded into the EthioDeploy container.

## Conversation behavior

- Local dialogue rules handle clear greetings, common small talk, and explicit
  conversation-repair messages.
- Open-ended questions and messages not confidently handled by local rules are
  routed to the configured generative model first.
- If model inference is unavailable, the existing local fallback remains active.
- Supports English, Roman Urdu/Hindi/Punjabi patterns, Urdu-script input, and
  Devanagari input. Language detection and transliteration are lightweight
  approximations, not full linguistic models.
- Keeps the latest 20 stored messages per conversation when a stable
  `conversation_id` is supplied.

## Hosted model configuration

Cloudflare Workers AI is the preferred provider for the 256 MB class container.
Set these variables in EthioDeploy:

- `CLOUDFLARE_ACCOUNT_ID`: Cloudflare account ID.
- `CLOUDFLARE_API_TOKEN`: secret token with Workers AI run permission.
- `ALEXA_CLOUDFLARE_MODEL`: optional model ID; defaults to
  `@cf/zai-org/glm-4.7-flash`.
- `ALEXA_LOCAL_MODEL_TIMEOUT`: optional inference timeout in seconds
  (default 12; bounded between 3 and 90).

An optional OpenAI-compatible inference server can be configured with
`ALEXA_LOCAL_MODEL_URL`, `ALEXA_LOCAL_MODEL_NAME`, and
`ALEXA_LOCAL_MODEL_TOKEN`. Cloudflare is attempted first.

## API

- `GET /health`
- `POST /chat`
- `POST /v1/chat/completions`

Example request:

```json
{
  "conversation_id": "telegram:chat_id:user_id",
  "message": "Ki haal aa?"
}
```

Always pass a stable conversation identifier scoped to the Telegram chat and
member. Without one, server-side memory cannot be separated safely between
users. The caller may also pass recent `history` as a fallback when the
service database is new or reset.

## Security

- Set `ALEXA_AI_TOKEN` in EthioDeploy and send the same value from AlexaXBot
  as a Bearer token. POST requests require authentication when this variable
  is configured.
- Never commit API tokens or print credentials in logs.
- Use a persistent volume for `ALEXA_AI_DB_PATH` if the host supports one.
  SQLite survives process restarts, but not replacement of an ephemeral
  container filesystem.

## Deployment

- Keep the service root directory as `alexa_own_ai`.
- Build using the included Dockerfile.
- The container starts with `bootstrap.py`, which installs AI-first routing
  while preserving the existing local dialogue and utility handlers.
- Bind to the platform-provided `PORT` (default 3000).
