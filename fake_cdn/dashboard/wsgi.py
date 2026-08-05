"""Gunicorn 使用的 Dashboard WSGI 入口。"""

from fake_cdn.dashboard.app import create_app

dash_app = create_app()
application = dash_app.server
