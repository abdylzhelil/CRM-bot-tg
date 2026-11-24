"""Ключи запроса aiohttp (общие для server.py и api.py)."""
from aiohttp import web

from app.models import User

USER_KEY = web.RequestKey("user", User)
