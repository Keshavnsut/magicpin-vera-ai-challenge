# magicpin Vera AI Challenge Bot

This submission implements the challenge composer and stateful judge API. It routes by trigger type, grounds messages in the provided category, merchant, trigger, and optional customer contexts, and uses deterministic safeguards for customer consent, taboos, duplicate sends, auto-replies, opt-outs, and explicit intent. An OpenAI-compatible LLM is optional at runtime; without a key the bot uses its grounded deterministic composer.

## Run

```powershell
python -m pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Google Gemini is the configured deployment provider. Set `LLM_PROVIDER=gemini`, `GEMINI_API_KEY`, and `GEMINI_MODEL=gemini-3.5-flash-lite`; Gemini uses its OpenAI-compatible endpoint. Groq is also supported with `LLM_PROVIDER=groq`, `GROQ_API_KEY`, and optionally `GROQ_MODEL` (default `openai/gpt-oss-20b`). OpenAI is supported with `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and `OPENAI_MODEL`. Do not commit secrets. `TEAM_NAME`, `TEAM_MEMBERS`, and `CONTACT_EMAIL` configure judge metadata.

The Gemini API free tier currently has limited model and rate-limit availability, and Google states free-tier content may be used to improve its products. Review Google's current terms before sending anything other than synthetic challenge data.

Render logs report LLM composition/reply success or a safe fallback reason, plus provider/model and HTTP status when available. Logs do not include API keys, prompts, context payloads, or generated message bodies.

## Dataset and submission

The archive provides seed data and a deterministic expander. Rebuild the expanded dataset and canonical test set with:

```powershell
python dataset/generate_dataset.py --seed-dir dataset --out dataset/expanded
python generate_submission.py
```

This creates 50 merchants, 200 customers, 100 triggers, and `submission.jsonl` with the 30 generated canonical pairs. In the absence of an API key, submission generation is deterministic and offline.

To run the supplied judge simulator, start the bot in one terminal and configure a judge-provider key in `judge_simulator.py` in another. The simulator uses its own LLM to score responses; that key is separate from `OPENAI_API_KEY` used by the bot.

## Deploy

`render.yaml` defines a Render web service. Connect this repository in Render, provide the API key for the selected provider as a secret, and set `LLM_PROVIDER` plus team metadata in the service environment. For Gemini, set `LLM_PROVIDER=gemini`, add `GEMINI_API_KEY`, and use `GEMINI_MODEL=gemini-3.5-flash-lite` (or another model enabled for your AI Studio project). The app responds at `/v1/*`; synthetic challenge data is held in process memory and `/v1/teardown` clears it.

## Tradeoffs

The deterministic composer ensures the bot remains usable when the LLM service is unavailable and avoids inventing data by default. The optional model path improves phrasing but only uses supplied contexts and is validated for empty output, URLs, and category taboo terms. No non-LLM external data services are called. More local appointment availability, current inventory, and merchant-specific capacity would improve the accuracy of suggested next steps.
