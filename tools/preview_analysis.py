"""Как выглядит анализ на живой базе: python3 tools/preview_analysis.py <sale.db> <key> [<key>…]
Печатает текст сообщения без HTML-тегов. База — копия состояния (там телефоны: не коммитить)."""
import json, re, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import market  # noqa: E402
import rent_radar as rr  # noqa: E402

st = rr.Store(Path(sys.argv[1]))
cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
for key in sys.argv[2:]:
    row = st.conn.execute("SELECT data FROM listings WHERE key=?", (key,)).fetchone()
    l = json.loads(row[0])
    t = market.format_analysis(st, l, cfg)
    print("=" * 70, key, len(t), "символов")
    print(re.sub(r"<[^>]+>", "", t))
