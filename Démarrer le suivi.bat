@echo off
cd /d "%~dp0"

:: ── Clé IA pour la Synthèse IA ─────────────────────────────────────────────
:: Option 1 — Groq (GRATUIT) : créez un compte sur console.groq.com → API Keys
set GROQ_API_KEY=COLLEZ_VOTRE_CLE_GROQ_ICI

:: Option 2 — Claude API (payant) : console.anthropic.com → API Keys
:: set ANTHROPIC_API_KEY=COLLEZ_VOTRE_CLE_CLAUDE_ICI
:: ───────────────────────────────────────────────────────────────────────────

start "" "http://localhost:7777"
python serveur.py
pause
