---
trigger: model_decision
description: Secrets and security standard — never hard-code API keys/tokens, always go through .env/config, never log sensitive data.
---

# Secrets & Security

- Never hard-code API keys, tokens, passwords, connection strings, or any credential directly in source code — not even "temporarily for testing." Always read them from environment variables or a config module that reads from environment variables.
- Every project that needs secrets must have a `.env.example` (with placeholder values, never real ones) committed to the repo, and the real `.env` must be listed in `.gitignore`.
- Never print or log full secret values, tokens, or personally identifiable information (raw emails, phone numbers, full credit card numbers, etc.). If a value must appear in a log for debugging, mask/truncate it (e.g. show only the last 4 characters).
- Never commit sample data, fixtures, or test files that contain real credentials or real personal data. Use synthetic/fake data for tests.
- When writing code that calls an external API, read the API key from config at call time — don't pass secrets as plain function default arguments, and don't embed them in URLs that might end up in logs.
- If asked to fix or add a feature to code that already contains a hard-coded secret, flag it and suggest moving it to `.env`/config rather than leaving it as-is or copying the pattern elsewhere.