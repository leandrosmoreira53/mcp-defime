"""Erros tipados. Toda falha que chega nas tools MCP vira uma destas."""

from __future__ import annotations


class DefimeError(Exception):
    """Base de todos os erros do projeto."""


class InvalidPoolId(DefimeError):
    pass


class PoolNotFound(DefimeError):
    pass


class UpstreamError(DefimeError):
    """Fonte respondeu com erro (5xx, timeout, resposta inválida)."""


class RateLimited(UpstreamError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamSchemaChanged(UpstreamError):
    """A fonte mudou o formato e um campo obrigatório sumiu."""


class NotFoundHTTP(DefimeError):
    """404 da fonte (uso interno; o client converte em PoolNotFound)."""


class OutOfScope(DefimeError):
    """Pool/chain fora do escopo configurado (SCOPE=rwa_bluechip)."""


class FeatureUnavailable(DefimeError):
    """Recurso que depende de uma fonte desligada ou paga."""
