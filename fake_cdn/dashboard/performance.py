"""Dashboard HTTP 响应压缩与静态资源缓存策略。"""

from __future__ import annotations

from flask import Flask, request
from flask_compress import Compress


def configure_response_optimization(server: Flask) -> None:
    """启用 gzip，并为带版本标识的静态资源设置长期缓存。"""
    server.config.update(
        COMPRESS_ALGORITHM=["gzip"],
        COMPRESS_LEVEL=6,
        COMPRESS_MIN_SIZE=1024,
        COMPRESS_MIMETYPES=[
            "application/javascript",
            "application/json",
            "text/css",
            "text/html",
            "text/javascript",
            "text/xml",
        ],
    )
    Compress(server)

    @server.after_request
    def add_static_cache_headers(response):
        if request.method not in {"GET", "HEAD"} or response.status_code != 200:
            return response

        path = request.path
        if path.startswith("/_dash-component-suites/"):
            if ".v" in path:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=86400"
        elif path.startswith("/assets/"):
            if request.args.get("m"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=3600"
        return response
