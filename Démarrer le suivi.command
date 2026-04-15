#!/bin/bash
cd "$(dirname "$0")"

sleep 2 && open "http://localhost:7777" &
python3 serveur.py
