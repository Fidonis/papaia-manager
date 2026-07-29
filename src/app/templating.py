"""The shared Jinja2 environment.

Page routes and the application-level error handlers both render templates.
Sharing one environment keeps a single template search path and one compiled
template cache, instead of each module standing up its own.
"""
from __future__ import annotations

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")
