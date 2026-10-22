"""worker/app/app.html → worker/src/app.js (строка для воркера). Запуск: python3 tools/build_app.py"""
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
html = (root / "worker/app/app.html").read_text(encoding="utf-8")
out = ("// Сгенерировано tools/build_app.py из worker/app/app.html — правьте app.html и пересоберите.\n"
       "export const APP_HTML = " + json.dumps(html, ensure_ascii=False) + ";\n")
(root / "worker/src/app.js").write_text(out, encoding="utf-8")
print("app.js:", len(out), "bytes")
