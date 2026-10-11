#!/bin/bash
# Doppelklick startet die Zugbindung-App im Browser.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv (Python-Paketmanager) fehlt und wird jetzt einmalig installiert ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "Installation fehlgeschlagen."; read -r -p "Enter zum Schließen"; exit 1; }
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "Starte Zugbindung ... (beim ersten Start werden Pakete installiert)"
uv run --quiet zugbindung app || read -r -p "Es ist ein Fehler aufgetreten. Enter zum Schließen"
