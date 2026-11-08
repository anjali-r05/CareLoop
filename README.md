# CareLoop AI V21 — CarePilot + Gnani Prisma v3

## Run on Windows
1. Extract this ZIP into a new folder.
2. Open PowerShell in this folder.
3. Run:

```powershell
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

4. Create the local environment file and edit it:

```powershell
Copy-Item .env.example .env
notepad .env
```

Put your own Gnani API key after `GNANI_API_KEY=` (no quotes). If `.env` is missing, CareLoop creates it from `.env.example` on first start; you must still paste your key and restart the server. Never share `.env` or screenshots that reveal the key.
5. Start the app:

```powershell
python app.py
```

6. Open http://127.0.0.1:5002/ and sign in. For Prisma testing, open CarePilot, choose an audio file, then click **Transcribe with Gnani Prisma**. A successful transcript is inserted into the request box; then click **Analyze request** and review all proposed fields before saving.

## Gnani Prisma integration
This release calls Gnani's documented Prisma v3 REST endpoint directly with `requests`: `POST https://api.vachana.ai/stt/v3`, header `X-API-Key-ID`, and multipart fields `audio_file`, `model=prisma-v3.0`, `language_code=indic-auto`, and `format=codemix`. This avoids SDK/request-serialization differences. The API key remains server-side. Prisma v3 REST recordings are limited to 30 seconds. Supported formats: WAV, MP3, FLAC, OGG, M4A and AAC. The multipart `Content-Type` header is deliberately left to `requests` so the boundary is correct.

The app marks Gnani as connected only after a live transcription succeeds in the current server session. A configured key alone is not treated as proof of connectivity. Provider error details are surfaced in the UI to help diagnose 400/401/402/403/429 responses.

Official references:
- https://docs.gnani.ai/api/STT/speech-to-text
- https://github.com/Gnani-AI-Mintlify/gnani-vachana

## CarePilot typed requests
Typed requests are interpreted by CarePilot's local bilingual intent and scheduling engine. Gnani Prisma handles audio transcription only; it does not generate task plans or analyse typed text. Audio transcript is placed into the request box, after which CarePilot performs its own analysis. Multi-action requests are editable proposals and are only saved after human confirmation.

## Safety
CareLoop is a care-coordination tool, not a medical device. It does not diagnose, prescribe, verify medicine use, send notifications, or book appointments. Verify every transcript, date, owner and proposed task before saving.
