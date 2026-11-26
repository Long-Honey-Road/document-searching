import argparse
import os
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

import yaml
from docx import Document

SAMPLE_AREAS = {
    "dbrct-project-activity": "ДБРЦТ",
    "dib-presale-activity": "ДИБ",
    "dib-project-activity": "ДИБ",
    "project-activity-organization": "общий",
}
EFFECTIVE_FROM = date(2026, 11, 27)
AREAS = [
    {
        "code": "ДИБ",
        "title": "Дирекция информационной безопасности",
        "synonyms": ["Дирекция ИБ"],
    },
    {
        "code": "ДБРЦТ",
        "title": "Департамент бизнес-решений и цифровой трансформации",
        "synonyms": [],
    },
    {"code": "общий", "title": "Общие регламенты", "synonyms": ["общий порядок"]},
]
SYNTHETIC_DOCUMENTS = [
    (
        "subcontractor-selection",
        "Регламент подбора субподрядчика",
        "public",
        [
            "1. Подбор субподрядчика",
            (
                "1.1. Руководитель проекта формирует запрос на подбор, когда внутренних "
                "ресурсов для проектных работ недостаточно."
            ),
            (
                "1.2. В запрос включаются объём работ, сроки, требования к квалификации "
                "и ограничения по доступу к данным."
            ),
            (
                "1.3. Отдел закупок собирает предложения и передаёт сравнительную таблицу "
                "руководителю проекта."
            ),
        ],
    ),
    (
        "deal-margin-control",
        "Регламент контроля маржинальности сделок",
        "public",
        [
            "1. Передача расчёта на проверку",
            (
                "1.1. Владелец сделки направляет финансовому контролёру расчёт плановой "
                "маржинальности до согласования коммерческого предложения."
            ),
            (
                "1.2. Финансовый контролёр проверяет расчёт в течение {deadline} "
                "с момента получения полного комплекта документов."
            ),
            "1.3. В комплект входят расчёт проекта, план поставок и перечень затрат.",
        ],
    ),
    (
        "finance-documents",
        "Регламент архивирования финансовых заключений",
        "closed_fin",
        [
            "1. Финансовый архив",
            "1.1. Финансовый контролёр передаёт заключение по расходам в закрытый архив.",
            (
                "1.2. Срок передачи заключения по расходам в закрытый архив — семь рабочих дней "
                "после подписания."
            ),
            "1.3. Оригинал заключения хранится в финансовом отделе.",
        ],
    ),
]


def save_if_changed(path: Path, contents: bytes) -> None:
    if path.is_file() and path.read_bytes() == contents:
        return
    with NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        staged.write(contents)
    try:
        os.replace(staged.name, path)
    finally:
        Path(staged.name).unlink(missing_ok=True)


def render_docx(title: str, paragraphs: list[str]) -> bytes:
    doc = Document()
    doc.add_paragraph(title)
    for paragraph in paragraphs:
        doc.add_paragraph(paragraph)
    raw = BytesIO()
    doc.save(raw)

    # Keep hashes stable when the manifest changes but a document stays the same.
    normalized = BytesIO()
    with ZipFile(raw) as source, ZipFile(normalized, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            info = source.getinfo(name)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            target.writestr(info, source.read(name))
    return normalized.getvalue()


def build_corpus(samples_dir: Path, docs_dir: Path, revision: int = 1) -> Path:
    if revision not in (1, 2):
        raise ValueError("Поддерживаются редакции 1 и 2")

    samples = [(stem, (samples_dir / f"{stem}.docx").read_bytes()) for stem in SAMPLE_AREAS]
    docs_dir.mkdir(parents=True, exist_ok=True)
    entries = []

    def add_document(
        doc_id: str,
        title: str,
        area: str,
        access: str,
        contents: bytes,
        version: int = 1,
    ) -> None:
        filename = (
            f"{doc_id}-v{version}.docx" if doc_id == "deal-margin-control" else f"{doc_id}.docx"
        )
        save_if_changed(docs_dir / filename, contents)
        entry = {
            "file": filename,
            "doc_id": doc_id,
            "title": title,
            "areas": [area],
            "access": access,
            "version": version,
            "effective_from": EFFECTIVE_FROM,
        }
        if version > 1:
            entry["supersedes"] = f"{doc_id}@{version - 1}"
        entries.append(entry)

    for stem, contents in samples:
        original = Document(BytesIO(contents))
        title = next(
            paragraph.text.strip() for paragraph in original.paragraphs if paragraph.text.strip()
        )
        add_document(stem, title, SAMPLE_AREAS[stem], "public", contents)

    for stem, title, access, paragraphs in SYNTHETIC_DOCUMENTS:
        version = revision if stem == "deal-margin-control" else 1
        deadline = "1 рабочего дня" if version == 2 else "3 рабочих дней"
        contents = render_docx(
            title, [paragraph.format(deadline=deadline) for paragraph in paragraphs]
        )
        add_document(stem, title, "общий", access, contents, version)

    manifest = docs_dir / "manifest.yaml"
    contents = yaml.safe_dump(
        {"areas": AREAS, "documents": entries}, allow_unicode=True, sort_keys=False
    )
    save_if_changed(manifest, contents.encode("utf-8"))
    return manifest


def main() -> None:
    repository = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Собрать мини-корпус RegAgent для локального стенда."
    )
    parser.add_argument("--samples-dir", type=Path, default=repository / "data/samples")
    parser.add_argument("--docs-dir", type=Path, default=repository / "data/docs")
    parser.add_argument("--revision", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    print(build_corpus(args.samples_dir, args.docs_dir, args.revision))


if __name__ == "__main__":
    main()
