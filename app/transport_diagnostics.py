"""Allowlisted failure metadata only; never serialize exception text or names."""
import errno
import http.client
import socket
import ssl
from urllib.error import HTTPError, URLError

VERSION = "transport-diagnostics-1.0.0"
LABELS = {
    "connection_timeout": "истёк срок ожидания сетевого ответа",
    "guard_timeout": "истёк общий срок ожидания запроса",
    "connection_error": "соединение оборвалось или не установилось",
    "dns_error": "не удалось определить сетевой адрес",
    "tls_error": "ошибка защищённого соединения",
    "http_error": "провайдер вернул ошибку HTTP",
    "response_json": "ответ не удалось разобрать как JSON",
    "invalid_envelope": "неверный формат ответа API",
    "invalid_usage": "неполные сведения об использованных токенах",
    "response_too_large": "ответ превышает допустимый размер",
    "response_validation": "ответ не прошёл проверку формата или связности",
    "invalid_transport_response": "адаптер не вернул ожидаемый ответ",
    "model_mismatch": "ответила другая модель",
    "truncated_response": "ответ оборван по лимиту",
    "terminal_response_error": "модель не завершила ответ штатно",
    "output_bound_breached": "ответ превысил согласованный лимит токенов",
    "unknown_cost": "стоимость запроса не установлена",
    "unexpected_error": "неопределённый технический сбой",
}


def detail(category, http_status=None):
    return dict(category=category if isinstance(category, str) and category in LABELS else "unexpected_error",
        http_status=http_status if type(http_status) is int and 100 <= http_status <= 599 else None)


class TransportFailure(ValueError):
    def __init__(self, code, *, diagnostic=None):
        super().__init__(code)
        self.diagnostic = detail(diagnostic.get("category"),diagnostic.get("http_status")) if isinstance(diagnostic,dict) else detail("unexpected_error")


def exception_detail(error, *, http_status=None):
    """No str/repr/args, traceback, URL, headers or dynamic exception-class name."""
    seen = set()
    for _ in range(5):
        if id(error) in seen:
            break
        seen.add(id(error))
        if isinstance(error, TransportFailure):
            saved = error.diagnostic
            return detail(saved.get("category"), http_status if http_status is not None else saved.get("http_status"))
        if isinstance(error, HTTPError):
            return detail("http_error", http_status if http_status is not None else error.code)
        if isinstance(error, URLError) and isinstance(error.reason, BaseException):
            error = error.reason
            continue
        if isinstance(error, (TimeoutError, socket.timeout)):
            return detail("connection_timeout", http_status)
        if isinstance(error, ssl.SSLError):
            return detail("tls_error", http_status)
        if isinstance(error, socket.gaierror):
            return detail("dns_error", http_status)
        if isinstance(error, (ConnectionError, http.client.RemoteDisconnected, http.client.IncompleteRead, EOFError)):
            return detail("connection_error", http_status)
        if isinstance(error, http.client.HTTPException):
            return detail("invalid_envelope", http_status)
        if isinstance(error, OSError):
            if error.errno == errno.ETIMEDOUT:
                return detail("connection_timeout", http_status)
            if error.errno in {errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED,
                              errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH}:
                return detail("connection_error", http_status)
        break
    return detail("unexpected_error", http_status)


def message_suffix(diagnostic):
    category = diagnostic.get("category") if isinstance(diagnostic, dict) else None
    if category == "response_validation":return ""  # Keep existing precise validator explanations.
    return " Тип сбоя: " + LABELS[category] + "." if isinstance(category, str) and category in LABELS else ""
