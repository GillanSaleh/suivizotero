#!/bin/bash
cd "$(dirname "$0")"

# ── Clé IA pour la Synthèse IA ────────────────────────────────────────────────
# Option 1 — Groq (GRATUIT) : créez un compte sur console.groq.com → API Keys
export GROQ_API_KEY="COLLEZ_VOTRE_CLE_GROQ_ICI"

# Option 2 — Claude API (payant) : console.anthropic.com → API Keys
# export ANTHROPIC_API_KEY="COLLEZ_VOTRE_CLE_CLAUDE_ICI"
# ─────────────────────────────────────────────────────────────────────────────

sleep 2 && open "http://localhost:7777" &
python3 serveur.py
