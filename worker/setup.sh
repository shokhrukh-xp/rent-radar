#!/bin/sh
# Разовая настройка воркера Ra'no: два ключа вставляете вы, остальное Claude включит сам.
set -e
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"
cd "$(dirname "$0")"
echo ""
echo "1/2  Ключ Gemini — тот же, что в GetFit (https://aistudio.google.com/apikey)."
echo "     Вставьте его и нажмите Enter (символы не видны — так и должно быть):"
npx --yes wrangler secret put GEMINI_KEY
echo ""
echo "2/2  GitHub-токен (rent-radar, Actions: Read and write)."
echo "     Вставьте его и нажмите Enter:"
npx --yes wrangler secret put GH_TOKEN
echo ""
echo "✅ Готово. Напишите Claude «готово»."
