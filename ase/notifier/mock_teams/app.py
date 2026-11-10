"""FastAPI mock Teams + Feedback API tối giản (NOTIFIER_PLAN §3.5)."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from ase.common.config import Settings, get_settings
from ase.common.transport import Clock, WallClock
from ase.dashboard.api import router as dashboard_api_router
from ase.dashboard.pages import router as dashboard_pages_router

FeedbackAction = Literal["confirm", "wrong_category", "resolved"]

ACTION_LABELS: dict[str, str] = {
    "confirm": "Xác nhận đúng",
    "wrong_category": "Sai phân loại",
    "resolved": "Đã xử lý",
}

CHANNELS: list[tuple[str, str]] = [
    ("it-ops", "IT Ops"),
    ("field-ops", "Field Ops"),
    ("payment-ops", "Payment Ops"),
    ("business-ops", "Business Ops"),
    ("battery-ops", "Battery Ops"),
    ("cskh", "CSKH"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    incident_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL,
    card_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    action TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


async def _ensure_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


def _extract_incident_and_revision(
    payload: dict[str, object],
) -> tuple[str, int, dict[str, object]]:
    """Trích incident_id, revision và card dict từ payload gửi vào webhook."""
    card = payload
    if isinstance(payload.get("card"), dict):
        card = payload["card"]  # type: ignore[assignment]

    inc_id = payload.get("incident_id") or card.get("incident_id")
    rev_raw = payload.get("revision") or card.get("revision") or 1
    revision = int(rev_raw) if isinstance(rev_raw, (int, str)) else 1

    if not isinstance(inc_id, str) or not inc_id:
        raw_str = json.dumps(card)
        m = re.search(r"/feedback/([^/\"']+)/", raw_str)
        inc_id = m.group(1) if m else "INC-UNKNOWN"

    return inc_id, revision, card


def create_app(
    db_path: Path | None = None,
    clock: Clock | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    app = FastAPI(title="ASE Mock Microsoft Teams & NOC Dashboard")
    app.state.db_path = db_path
    app.state.clock = clock or WallClock()
    app.state.settings = settings

    static_dir = Path(__file__).resolve().parents[2] / "dashboard" / "static"
    app.mount(
        "/static/dashboard",
        StaticFiles(directory=str(static_dir)),
        name="dashboard-static",
    )

    @app.middleware("http")
    async def _drop_empty_dashboard_params(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Form lọc gửi `as_of=` (rỗng) khi bấm "Cuối dữ liệu"; coi tham số rỗng như không có."""
        qs = request.scope.get("query_string", b"")
        if request.url.path.startswith(("/dashboard", "/api/dashboard")) and qs:
            pairs = parse_qsl(qs.decode("latin-1"), keep_blank_values=True)
            kept = [(k, v) for k, v in pairs if v != ""]
            if len(kept) != len(pairs):
                request.scope["query_string"] = urlencode(kept).encode("latin-1")
        return await call_next(request)

    app.include_router(dashboard_api_router)
    app.include_router(dashboard_pages_router)

    def _resolve_db_path() -> Path:
        configured: Path | None = app.state.db_path
        if configured is not None:
            return configured
        cfg_settings: Settings | None = app.state.settings
        return (cfg_settings or get_settings()).notifier_db

    def _resolve_clock() -> Clock:
        c: Clock = app.state.clock
        return c

    @app.post("/webhook/{channel}")
    async def receive_webhook(channel: str, request: Request) -> JSONResponse:
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"error": "payload must be a JSON object"}, status_code=400)

        incident_id, revision, card = _extract_incident_and_revision(payload)
        card_json = json.dumps(card, ensure_ascii=False)
        now_iso = _resolve_clock().now().isoformat()
        target_db = _resolve_db_path()
        await _ensure_db(target_db)

        async with aiosqlite.connect(target_db) as db:
            # Revision mới thay card cũ và hiện lên đầu kênh (xoá rồi chèn lại → id mới).
            await db.execute("DELETE FROM cards WHERE incident_id = ?", (incident_id,))
            await db.execute(
                """
                INSERT INTO cards (channel, incident_id, revision, card_json, received_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    channel = excluded.channel,
                    revision = excluded.revision,
                    card_json = excluded.card_json,
                    received_at = excluded.received_at
                """,
                (channel, incident_id, revision, card_json, now_iso),
            )
            await db.commit()

        return JSONResponse(
            {
                "status": "ok",
                "channel": channel,
                "incident_id": incident_id,
                "revision": revision,
            }
        )

    @app.get("/feedback/{incident_id}/{action}", response_class=HTMLResponse)
    async def confirm_feedback_page(incident_id: str, action: FeedbackAction) -> HTMLResponse:
        label = ACTION_LABELS[action]
        esc_id = html.escape(incident_id)
        esc_label = html.escape(label)
        esc_action = html.escape(action)
        page = (
            "<!DOCTYPE html>\n"
            '<html lang="vi">\n<head>\n  <meta charset="utf-8">\n'
            "  <title>Xác nhận phản hồi — ASE</title>\n</head>\n"
            "<body style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',"
            ' sans-serif; padding: 32px; max-width: 560px; margin: 0 auto;">\n'
            '  <div style="border: 1px solid #3b82f6; border-left: 6px solid #2563eb;'
            ' border-radius: 6px; padding: 20px; background: #eff6ff;">\n'
            '    <h2 style="margin-top: 0; color: #1e40af;">Xác nhận gửi phản hồi</h2>\n'
            f"    <p>Bạn sắp gửi phản hồi của OPS cho incident <code>{esc_id}</code>:</p>\n"
            '    <p style="font-size: 16px;">'
            f"<strong>{esc_label}</strong> (<code>{esc_action}</code>)</p>\n"
            f'    <form method="POST" action="/feedback/{esc_id}/{esc_action}" '
            'style="margin-top: 20px;">\n'
            '      <button type="submit" style="background: #2563eb; color: #ffffff; border: none;'
            ' padding: 10px 20px; border-radius: 6px; font-weight: 600; cursor: pointer;">\n'
            f"        Xác nhận: {esc_label}\n"
            "      </button>\n"
            '      <a href="/" style="margin-left: 12px; color: #4b5563; '
            'text-decoration: none;">Huỷ</a>\n'
            "    </form>\n"
            "  </div>\n</body>\n</html>"
        )
        return HTMLResponse(page)

    @app.post("/feedback/{incident_id}/{action}", response_class=HTMLResponse)
    async def record_feedback(incident_id: str, action: FeedbackAction) -> HTMLResponse:
        now_iso = _resolve_clock().now().isoformat()
        target_db = _resolve_db_path()
        await _ensure_db(target_db)

        async with aiosqlite.connect(target_db) as db:
            await db.execute(
                "INSERT INTO feedback (incident_id, action, at) VALUES (?, ?, ?)",
                (incident_id, action, now_iso),
            )
            await db.commit()

        label = ACTION_LABELS[action]
        esc_id = html.escape(incident_id)
        esc_label = html.escape(label)
        esc_action = html.escape(action)
        page = (
            "<!DOCTYPE html>\n"
            '<html lang="vi">\n<head>\n  <meta charset="utf-8">\n'
            "  <title>Đã ghi nhận phản hồi — ASE</title>\n</head>\n"
            "<body style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',"
            ' sans-serif; padding: 32px; max-width: 560px; margin: 0 auto;">\n'
            '  <div style="border: 1px solid #16a34a; border-left: 6px solid #16a34a;'
            ' border-radius: 6px; padding: 20px; background: #f0fdf4;">\n'
            '    <h2 style="margin-top: 0; color: #166534;">Đã ghi nhận</h2>\n'
            f"    <p>Hệ thống đã lưu phản hồi của OPS cho incident <code>{esc_id}</code>:</p>\n"
            f"    <p><strong>{esc_label}</strong> (<code>{esc_action}</code>)"
            f" lúc <code>{html.escape(now_iso)}</code>.</p>\n"
            '    <p style="margin-bottom: 0;"><a href="/" style="color: #2563eb;'
            ' text-decoration: none; font-weight: 600;">← Về trang chủ</a></p>\n'
            "  </div>\n</body>\n</html>"
        )
        return HTMLResponse(page)

    @app.get("/", response_class=HTMLResponse, response_model=None)
    async def index(channel: str | None = None) -> Response:
        cfg: Settings = app.state.settings or get_settings()
        if not cfg.mock_teams_ui:
            # Tạm tắt giao diện mock Teams: "/" mở dashboard. Bật lại: ASE_MOCK_TEAMS_UI=true
            return RedirectResponse("/dashboard", status_code=307)
        target_db = _resolve_db_path()
        await _ensure_db(target_db)

        async with aiosqlite.connect(target_db) as db:
            db.row_factory = aiosqlite.Row
            if channel:
                c_cur = await db.execute(
                    "SELECT * FROM cards WHERE channel = ? ORDER BY id DESC", (channel,)
                )
            else:
                c_cur = await db.execute("SELECT * FROM cards ORDER BY id DESC")
            cards_rows = [dict(r) for r in await c_cur.fetchall()]

            count_cur = await db.execute(
                "SELECT channel, COUNT(*) AS cnt FROM cards GROUP BY channel"
            )
            counts = {str(r["channel"]): int(r["cnt"]) for r in await count_cur.fetchall()}

            f_cur = await db.execute(
                """
                SELECT f.incident_id, f.action, f.at
                FROM feedback f
                INNER JOIN (
                    SELECT incident_id, MAX(id) AS max_id
                    FROM feedback
                    GROUP BY incident_id
                ) latest ON f.id = latest.max_id
                """
            )
            latest_feedback = {
                str(r["incident_id"]): (str(r["action"]), str(r["at"]))
                for r in await f_cur.fetchall()
            }

        channel_map = dict(CHANNELS)
        all_cls = "active" if not channel else ""
        sidebar_items: list[str] = [
            f'<a href="/" class="ch-link {all_cls}">Tất cả kênh ({sum(counts.values())})</a>'
        ]
        for slug, name in CHANNELS:
            cnt = counts.get(slug, 0)
            active = "active" if channel == slug else ""
            esc_slug = html.escape(slug)
            sidebar_items.append(
                f'<a href="/?channel={esc_slug}" class="ch-link {active}"'
                f' data-channel="{esc_slug}"># {html.escape(name)}'
                f' <span class="badge">{cnt}</span></a>'
            )

        card_blocks: list[str] = []
        for idx, row in enumerate(cards_rows):
            inc_id = str(row["incident_id"])
            ch_slug = str(row["channel"])
            ch_name = channel_map.get(ch_slug, ch_slug)
            rev = int(row["revision"])
            raw_json = str(row["card_json"])
            received_at = str(row["received_at"])

            try:
                parsed = json.loads(raw_json)
            except json.JSONDecodeError:
                parsed = {}
            is_p1 = parsed.get("severity") == "P1"

            fb = latest_feedback.get(inc_id)
            if fb is not None:
                fb_action, fb_at = fb
                fb_label = ACTION_LABELS.get(fb_action, fb_action)
                esc_act = html.escape(fb_action)
                fb_html = (
                    f'<span class="fb-status fb-{esc_act}">'
                    f"Phản hồi mới nhất: {html.escape(fb_label)} ({esc_act})"
                    f" lúc {html.escape(fb_at)}</span>"
                )
            else:
                fb_html = '<span class="fb-status fb-none">Chưa có phản hồi OPS</span>'

            safe_json = raw_json.replace("</", "<\\/")
            border_cls = "card-p1" if is_p1 else "card-normal"
            esc_inc = html.escape(inc_id)
            esc_ch = html.escape(ch_slug)
            card_blocks.append(
                f'<div class="teams-card {border_cls}" data-incident-id="{esc_inc}"'
                f' data-channel="{esc_ch}" data-revision="{rev}">\n'
                '  <div class="card-meta">\n'
                f'    <span class="ch-tag">#{html.escape(ch_name)} ({esc_ch})</span>\n'
                f'    <span class="inc-tag">{esc_inc} · r{rev}</span>\n'
                f'    <span class="time-tag">{html.escape(received_at)}</span>\n'
                f"    {fb_html}\n"
                "  </div>\n"
                f'  <div id="ac-target-{idx}" class="ac-container"></div>\n'
                f'  <script type="application/json" id="ac-data-{idx}">{safe_json}</script>\n'
                "</div>"
            )

        cards_html = (
            "\n".join(card_blocks)
            if card_blocks
            else '<div class="empty">Chưa có cảnh báo nào trong kênh này.</div>'
        )

        ac_cdn = "https://cdn.jsdelivr.net/npm/adaptivecards@3.0.4/dist/adaptivecards.min.js"
        # Teams thật render Markdown (in đậm, link) trong TextBlock/FactSet; mock dùng markdown-it
        # (html=false: HTML trong text không được render, link `javascript:` bị chặn).
        md_cdn = "https://cdn.jsdelivr.net/npm/markdown-it@13.0.2/dist/markdown-it.min.js"
        page = f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <title>ASE Mock Microsoft Teams</title>
  <script src="{md_cdn}"></script>
  <script src="{ac_cdn}"></script>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f3f4f6; color: #111827; display: flex; min-height: 100vh;
    }}
    .sidebar {{
      width: 250px; background: #1e1b4b; color: #e0e7ff;
      padding: 20px 12px; box-sizing: border-box; flex-shrink: 0;
    }}
    .sidebar h1 {{ font-size: 16px; margin: 0 8px 16px; color: #ffffff; }}
    .ch-link {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 8px 12px; color: #c7d2fe; text-decoration: none;
      border-radius: 6px; margin-bottom: 4px; font-size: 14px;
    }}
    .ch-link:hover, .ch-link.active {{ background: #312e81; color: #ffffff; font-weight: 600; }}
    .badge {{
      background: #4338ca; color: #ffffff; border-radius: 10px;
      padding: 2px 8px; font-size: 12px;
    }}
    .main {{ flex: 1; padding: 24px; max-width: 920px; }}
    .teams-card {{
      background: #ffffff; border: 1px solid #d1d5db; border-left: 5px solid #4f46e5;
      border-radius: 8px; padding: 16px; margin-bottom: 16px;
    }}
    .teams-card.card-p1 {{
      border-left-color: #dc2626; border-color: #fca5a5; background: #fffafa;
    }}
    .card-meta {{
      display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
      font-size: 12px; color: #4b5563; margin-bottom: 12px;
      padding-bottom: 8px; border-bottom: 1px solid #e5e7eb;
    }}
    .ch-tag {{ font-weight: 700; color: #3730a3; }}
    .inc-tag {{
      font-family: monospace; background: #f3f4f6; padding: 2px 6px; border-radius: 4px;
    }}
    .fb-status {{ margin-left: auto; padding: 2px 8px; border-radius: 4px; font-weight: 600; }}
    .fb-confirm {{ background: #dcfce7; color: #166534; }}
    .fb-wrong_category {{ background: #fee2e2; color: #991b1b; }}
    .fb-resolved {{ background: #dbeafe; color: #1e40af; }}
    .fb-none {{ background: #f3f4f6; color: #6b7280; }}
    .empty {{
      padding: 40px; text-align: center; color: #6b7280;
      background: #ffffff; border-radius: 8px; border: 1px dashed #d1d5db;
    }}
  </style>
</head>
<body>
  <aside class="sidebar">
    <h1>ASE Mock Teams</h1>
    <a href="/dashboard" class="ch-link"
       style="background: #312e81; margin-bottom: 12px; font-weight: 600;">
      📊 Mở NOC Dashboard →
    </a>
    {"".join(sidebar_items)}
  </aside>
  <main class="main">
    {cards_html}
  </main>
  <script>
    if (typeof AdaptiveCards !== 'undefined' && typeof markdownit !== 'undefined') {{
      var md = markdownit({{ html: false, linkify: false, breaks: true }});
      AdaptiveCards.AdaptiveCard.onProcessMarkdown = function(text, result) {{
        result.outputHtml = md.render(text);
        result.didProcess = true;
      }};
    }}
    var scripts = document.querySelectorAll('script[type="application/json"][id^="ac-data-"]');
    scripts.forEach(function(el) {{
      var idx = el.id.replace('ac-data-', '');
      var target = document.getElementById('ac-target-' + idx);
      if (!target || typeof AdaptiveCards === 'undefined') return;
      try {{
        var cardJson = JSON.parse(el.textContent);
        var adaptiveCard = new AdaptiveCards.AdaptiveCard();
        adaptiveCard.onExecuteAction = function(action) {{
          if (action instanceof AdaptiveCards.OpenUrlAction && action.url) {{
            window.location.href = action.url;
          }}
        }};
        adaptiveCard.parse(cardJson);
        var rendered = adaptiveCard.render();
        if (rendered) target.appendChild(rendered);
      }} catch (e) {{
        target.textContent = el.textContent;
      }}
    }});
  </script>
</body>
</html>"""
        return HTMLResponse(page)

    return app


app = create_app()
