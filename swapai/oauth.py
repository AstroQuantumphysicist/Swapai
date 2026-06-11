"""Codex (ChatGPT) OAuth login with PKCE and a local callback server."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from . import config


@dataclass
class TokenSet:
    id_token: str
    access_token: str
    refresh_token: str
    account_id: str
    email: str
    plan: str


def _b64url(