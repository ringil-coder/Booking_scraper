"""Usuwanie identyfikatorów sesji Booking przed zapisem czegokolwiek, co trafia na GitHub.

Repozytorium jest publiczne: gałąź danych, logi i artefakty Actions (diagnostyka,
pilotaż) może pobrać każdy. Wyrenderowana strona oferty zawiera identyfikator sesji
(`sid`, `b_sid`), token CSRF i etykietę śledzącą (`label`) — w linkach, w ukrytych
polach formularzy i w skryptach. Parser dostaje oryginalny HTML; zapisujemy kopię
po `scrub_html`, a adres strony po `scrub_url`.
"""

from __future__ import annotations

import re

REDACTED = "REDACTED"
# Parametry adresu z danymi sesji lub śledzenia.
SESSION_URL_PARAMS: tuple[str, ...] = ("sid", "srpvid", "label", "token")
# Ukryte pola formularzy z danymi sesji.
SESSION_INPUT_NAMES: tuple[str, ...] = ("sid", "label", "token", "csrf_token", "b_csrf_token")
# Wartości krótsze nie są zamieniane w całym dokumencie (ryzyko trafień przypadkowych).
_MIN_GLOBAL_VALUE_LENGTH = 8

_URL_PARAM_RE = re.compile(
    r"(?<=[?&;])(" + "|".join(SESSION_URL_PARAMS) + r")=([^&;\"'\s<>#]*)", re.IGNORECASE
)
# `b_sid: '…'`, `"b_sid":"…"`, `sid = '…'`, `b_csrf_token: '…'`, `token = '…'`.
_SCRIPT_RE = re.compile(
    r"(\b(?:b_)?(?:sid|csrf_token|session_id|token)[\"']?\s*[:=]\s*)([\"'])([^\"'\n]*)\2"
)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_INPUT_NAME_RE = re.compile(
    r"\bname\s*=\s*[\"'](?:" + "|".join(SESSION_INPUT_NAMES) + r")[\"']", re.IGNORECASE
)
_INPUT_VALUE_RE = re.compile(r"(\bvalue\s*=\s*)([\"'])([^\"']*)\2", re.IGNORECASE)


def _session_values(html: str) -> set[str]:
    values = {m.group(2) for m in _URL_PARAM_RE.finditer(html)}
    values |= {m.group(3) for m in _SCRIPT_RE.finditer(html)}
    for tag in _INPUT_RE.findall(html):
        if _INPUT_NAME_RE.search(tag) and (value := _INPUT_VALUE_RE.search(tag)):
            values.add(value.group(3))
    return {v for v in values if len(v) >= _MIN_GLOBAL_VALUE_LENGTH and v != REDACTED}


def _scrub_input(match: re.Match[str]) -> str:
    tag = match.group(0)
    if not _INPUT_NAME_RE.search(tag):
        return tag
    return _INPUT_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", tag)


def scrub_url(url: str) -> str:
    """Adres bez wartości parametrów sesji (`sid`, `label`, …); reszta bez zmian."""
    return _URL_PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", url)


def scrub_html(html: str) -> str:
    """Kopia HTML bez identyfikatora sesji, tokenów i etykiet śledzących.

    Najpierw zbiera wartości z linków, skryptów i ukrytych pól, potem zamienia każde
    ich wystąpienie w dokumencie (także w miejscach, których wzorce nie obejmują).
    """
    for value in sorted(_session_values(html), key=len, reverse=True):
        html = html.replace(value, REDACTED)
    html = scrub_url(html)
    html = _SCRIPT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", html)
    return _INPUT_RE.sub(_scrub_input, html)
