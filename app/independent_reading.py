"""Separate image-reading uncertainty from the amount of student reasoning.

Only fresh independent checks use this wire format. Projection into the saved
compact/M01 shape uses explicit reading evidence, never the student-work note.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from . import compact_reading as compact
from .m05.runtime import PromptError
from .strict_reading import decode_reading

VERSION = "independent-reading-scope-1.0.1"
TEXT = compact.TEXT


def schema(page_count):
    result = compact.schema(page_count)
    page = result["properties"]["pages"]["items"]
    del page["properties"]["coverage"]
    page["required"].remove("coverage")
    unread = compact.obj(dict(location=TEXT,
        impact=dict(enum=["affects_score", "nonessential", "unknown"]), reason=TEXT))
    page["properties"].update(
        page_reading=compact.obj(dict(status=dict(enum=["complete", "partial", "unreadable", "not_processed"]),
            reason=TEXT, unread_parts=compact.array(unread))),
        student_work=deepcopy(TEXT))
    page["required"].extend(["page_reading", "student_work"])
    result["properties"]["task_scope"] = compact.obj(dict(
        status=dict(enum=["single_task", "uncertain", "multiple_tasks"]), reason=TEXT))
    result["required"].append("task_scope")
    return result


SCOPE_INSTRUCTIONS = """Контур петли не является отдельным знаком «−» или «0». Если отдельного знака внутри
разборчивой петли нет, опиши пустую петлю; неразборчивость не выдавай за отсутствие знака.
Варианты чтения знака допускай только по видимым штрихам.
При частичном зачёркивании отдели зачёркнутые фрагменты от действующих записей; не
помечай всю строку зачёркнутой и не выдавай скрытый под зачёркиванием знак за действующий.
Не предлагай отсутствующий знак или другой знак неравенства только ради верного продолжения.
Раздели чтение изображения и объём написанного учеником.
На каждой странице page_reading описывает ТОЛЬКО чтение предоставленного изображения:
status=complete/partial/unreadable/not_processed, reason — что просмотрено, unread_parts —
конкретные непрочитанные участки. У каждого участка укажи location, impact и reason.
impact=affects_score/unknown блокирует балл; nonessential допустим только с явным
основанием, почему непрочитанное не влияет на балл. Не объявляй страницу unreadable
или not_processed несущественной. partial требует перечислить непрочитанные участки;
complete — фактический полный просмотр и пустой unread_parts, а не наличие ответа.
student_work отдельно кратко описывает видимое: например только ответ, несколько
строк, рисунок или пустая страница. Это НЕ заключение о достаточности доказательства.
Не проверяй, каких математических шагов не хватает: это задача оценщика.
Если pages_complete=confirmed, пользователь подтвердил, что передал весь комплект.
Отсутствие выкладок, доказательства, начала решения или промежуточных действий само
по себе НЕ непрочитанный участок и НЕ повод требовать ещё страницу/черновик.
Полностью просмотренный лист только с ответом или пустой лист: complete, unread_parts=[].
task_scope относится к выбору одной задачи: single_task/uncertain/multiple_tasks и reason.
Сохраняй uncertain при конкретном видимом обрезании существенного содержания или
неясной принадлежности записей; multiple_tasks — при нескольких самостоятельных задачах.
Подпункты а/б и продолжения — одна задача. Отсутствие ученических выкладок само по себе
не делает task_scope неопределённым. Подтверждение комплекта не доказывает читаемость
фотографий: реально неразборчивые существенные участки всё равно перечисли явно.
Не возвращай прежнее поле coverage: его собирает код из page_reading и task_scope.
"""
_before, _rest = compact.INSTRUCTIONS.split("coverage обязателен на КАЖДОЙ странице:", 1)
_, _after = _rest.split("В interpretations", 1)
INSTRUCTIONS = _before + SCOPE_INSTRUCTIONS + "В interpretations" + _after
STRICT_INSTRUCTIONS = compact.STRICT_INSTRUCTIONS.replace(
    "Обязательные сведения, включая четыре поля coverage на каждой странице, сообщи явно.",
    "page_reading и student_work каждой страницы, а также общий task_scope сообщи явно.")


def build_reading(source, image_manifest):
    request = compact.build_reading(source, image_manifest)
    request.update(version=VERSION, system=INSTRUCTIONS,
        package_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        response_schema=schema(len(request["data"]["solution_pages"])))
    request["data"]["pages_complete"] = source["capture"]["pages_complete"]
    return request


def decode(value, page_count):
    value = decode_reading(value, schema(page_count))
    if [p["page"] for p in value["pages"]] != list(range(1, page_count + 1)):
        raise PromptError("compact_reading_pages")
    for page in value["pages"]:
        reading = page["page_reading"]
        if (reading["status"] == "complete" and
                (reading["unread_parts"] or any(e["reading"] == "unreadable" for e in page["entries"]))):
            raise PromptError("reading_scope_inconsistent")
        if reading["status"] == "partial" and not reading["unread_parts"]:
            raise PromptError("reading_scope_inconsistent")
    return value


def project(value):
    """Map explicit reading status/impact to old coverage; never parse prose."""
    value = decode(value, len(value["pages"]))
    result = deepcopy(value)
    scope = result.pop("task_scope")
    for page in result["pages"]:
        reading = page.pop("page_reading")
        page.pop("student_work")  # Only literal entries go to the grader.
        blockers = [p for p in reading["unread_parts"] if p["impact"] != "nonessential"]
        if scope["status"] != "single_task":
            accounted, reason = False, scope["reason"]
        elif reading["status"] in {"unreadable", "not_processed"}:
            accounted, reason = False, reading["reason"]
        elif blockers:
            accounted = False
            reason = "; ".join(p["location"] + ": " + p["reason"] for p in blockers)
        else:
            accounted = True
            reason = "; ".join(p["location"] + ": " + p["reason"] for p in reading["unread_parts"]) or reading["reason"]
        page["coverage"] = dict(status=reading["status"], reason=reading["reason"],
            score_scope="accounted" if accounted else "unresolved", score_scope_reason=reason)
    return result
