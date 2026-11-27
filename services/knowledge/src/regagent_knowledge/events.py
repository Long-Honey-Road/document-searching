"""События knowledge (D-52)."""

from contextvars import ContextVar

from regagent_common.events import EventLog

events = EventLog("knowledge")

# Задача индексации, внутри которой идут вызовы модели: LightRAG создаёт свои задачи
# внутри ainsert, они наследуют контекст и несут job_id в события llm.usage
CURRENT_JOB: ContextVar[str | None] = ContextVar("regagent_current_job", default=None)
