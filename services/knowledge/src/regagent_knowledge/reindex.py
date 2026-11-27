"""Поставить повторную индексацию действующих документов в RabbitMQ."""

import argparse
import asyncio
from pathlib import Path

import asyncpg
from regagent_common.access import AccessLevels

from .consumer import IndexQueue
from .jobs import IndexRequest
from .registry import Registry, plan_jobs
from .settings import RuntimeSettings
from .watcher import FolderSource, IndexPublisher


async def queue_reindex(source: FolderSource, registry: Registry, publisher: IndexPublisher) -> int:
    manifest, hashes = await asyncio.to_thread(source.scan)
    if manifest.unidentified_error or manifest.invalid:
        raise ValueError("переиндексация требует валидного манифеста")
    if not manifest.entries:
        raise ValueError("в манифесте нет документов")
    records = {record.doc_id for record in await registry.all() if record.status != "deleted"}
    entries = [entry for doc_id, entry in manifest.entries.items() if doc_id in records]
    if missing := [entry.file for entry in entries if entry.file not in hashes]:
        raise ValueError(f"в папке документов нет файлов: {', '.join(sorted(missing))}")
    jobs = plan_jobs(entries, hashes, ())
    for job in jobs:
        await publisher.publish(IndexRequest(**job.model_dump(), force=True))
    return len(jobs)


async def _run(env_file: Path | None) -> int:
    settings = RuntimeSettings() if env_file is None else RuntimeSettings(_env_file=env_file)
    levels = AccessLevels.from_yaml(settings.access_levels_file)
    async with (
        asyncpg.create_pool(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.knowledge_db_user,
            password=settings.knowledge_db_password.get_secret_value(),
            database=settings.knowledge_db,
        ) as pool,
        IndexQueue(settings.rabbitmq_url.get_secret_value()) as queue,
    ):
        registry = Registry(pool)
        return await queue_reindex(FolderSource(settings.docs_dir, levels), registry, queue)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    print(f"Поставлено задач: {asyncio.run(_run(args.env_file))}")


if __name__ == "__main__":
    main()
