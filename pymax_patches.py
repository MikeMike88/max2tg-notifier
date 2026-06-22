# -*- coding: utf-8 -*-
"""Runtime-патчи для maxapi-python (PyMax).

Накладывают точечные исправления на установленный из PyPI пакет, не вендоря его
исходники целиком. Применять нужно ОДИН раз, до создания ``Client``.

Сейчас здесь один патч — изменения PR #66 (parse bot account profiles,
https://github.com/MaxApiTeam/PyMax/pull/66), который ещё не попал в релиз PyPI:
MAX-боты присылают ``gender`` числовым кодом, а ``web_app`` — строкой-URL вместо
объекта; без патча ``User.model_validate`` падает с ``ValidationError`` прямо в
парсинге профиля, и бот-пользователь не загружается. Патч расширяет типы обоих
полей (gender: str | int, web_app: dict | str) и пересобирает pydantic-модель.

Патч завязан на внутреннюю структуру модели ``User``, поэтому версия
maxapi-python закреплена в ``requirements.txt``. Когда фикс выйдет в релизе
PyPI — этот модуль и вызов ``apply()`` можно удалить.
"""

from typing import Any

import pymax.types.domain.user as _user_mod

_applied = False


def apply() -> None:
    """Накладывает патчи PyMax. Идемпотентно: повторные вызовы — no-op."""
    global _applied
    if _applied:
        return

    user = _user_mod.User
    # PR #66: боты шлют gender как число и web_app как URL-строку.
    user.model_fields["gender"].annotation = str | int | None
    user.model_fields["web_app"].annotation = dict[str, Any] | str | None
    user.model_rebuild(force=True)

    _applied = True
