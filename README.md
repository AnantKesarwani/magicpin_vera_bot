# Magicpin Vera AI Challenge — Submission Bot

A stateful FastAPI implementation of the Vera merchant-assistant contract.

## What it does
- Stores versioned category/merchant/customer/trigger contexts.
- Handles `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`.
- Uses deterministic trigger-specific composition to avoid hallucinating facts.
- Uses merchant-specific numbers, active offers, subscription state and trigger payloads.
- Supports customer-facing recall/bridal follow-up when customer context and consent permit.
- Deduplicates with `suppression_key`.
- Detects common WhatsApp canned auto-replies.
- Detects positive/negative/wait/question intent without forcing a qualification loop.
- Includes `/v1/teardown` for test-state cleanup.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.bot:app --host 0.0.0.0 --port 8080
```

Health check:

```bash
curl http://localhost:8080/v1/healthz
```

## Challenge notes
The uploaded seed package does not include the `categories/*.json` files required by the supplied `generate_dataset.py`. The bot itself does not require local category files: the judge supplies category contexts through `/v1/context`.

The implementation follows the supplied brief's four-context model and composition fields. It deliberately starts deterministic and does not require an LLM API key.

## Deployment
Expose port 8080 through any HTTPS-capable deployment. Submit the public base URL.
