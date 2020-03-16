import importlib
from collections import defaultdict
from datetime import datetime
from os.path import splitext
from typing import List, Optional, Any, Type, Set, Dict
from zipfile import ZipFile, ZIP_BZIP2

import mongoengine
from tqdm import tqdm


class PrePopulate:
    @classmethod
    def export_to_zip(
        cls, filename: str, experiments: List[str] = None, projects: List[str] = None
    ):
        with ZipFile(filename, mode="w", compression=ZIP_BZIP2) as zfile:
            cls._export(zfile, experiments, projects)

    @classmethod
    def import_from_zip(cls, filename: str, user_id: str = None):
        with ZipFile(filename) as zfile:
            cls._import(zfile, user_id)

    @staticmethod
    def _resolve_type(
        cls: Type[mongoengine.Document], ids: Optional[List[str]]
    ) -> List[Any]:
        ids = set(ids)
        items = list(cls.objects(id__in=list(ids)))
        resolved = {i.id for i in items}
        missing = ids - resolved
        for name_candidate in missing:
            results = list(cls.objects(name=name_candidate))
            if not results:
                print(f"ERROR: no match for `{name_candidate}`")
                exit(1)
            elif len(results) > 1:
                print(f"ERROR: more than one match for `{name_candidate}`")
                exit(1)
            items.append(results[0])
        return items

    @classmethod
    def _resolve_entities(
        cls, experiments: List[str] = None, projects: List[str] = None
    ) -> Dict[Type[mongoengine.Document], Set[mongoengine.Document]]:
        from database.model.project import Project
        from database.model.task.task import Task

        entities = defaultdict(set)

        if projects:
            print("Reading projects...")
            entities[Project].update(cls._resolve_type(Project, projects))
            print("--> Reading project experiments...")
            objs = Task.objects(
                project__in=list(set(filter(None, (p.id for p in entities[Project]))))
            )
            entities[Task].update(o for o in objs if o.id not in (experiments or []))

        if experiments:
            print("Reading experiments...")
            entities[Task].update(cls._resolve_type(Task, experiments))
            print("--> Reading experiments projects...")
            objs = Project.objects(
                id__in=list(set(filter(None, (p.project for p in entities[Task]))))
            )
            project_ids = {p.id for p in entities[Project]}
            entities[Project].update(o for o in objs if o.id not in project_ids)

        return entities

    @classmethod
    def _cleanup_task(cls, task):
        from database.model.task.task import TaskStatus

        task.completed = None
        task.started = None
        if task.execution:
            task.execution.model = None
            task.execution.model_desc = None
            task.execution.model_labels = None
        if task.output:
            task.output.model = None

        task.status = TaskStatus.created
        task.comment = "Auto generated by Allegro.ai"
        task.created = datetime.utcnow()
        task.last_iteration = 0
        task.last_update = task.created
        task.status_changed = task.created
        task.status_message = ""
        task.status_reason = ""
        task.user = ""

    @classmethod
    def _cleanup_entity(cls, entity_cls, entity):
        from database.model.task.task import Task
        if entity_cls == Task:
            cls._cleanup_task(entity)

    @classmethod
    def _export(
        cls, writer: ZipFile, experiments: List[str] = None, projects: List[str] = None
    ):
        entities = cls._resolve_entities(experiments, projects)

        for cls_, items in entities.items():
            if not items:
                continue
            filename = f"{cls_.__module__}.{cls_.__name__}.json"
            print(f"Writing {len(items)} items into {writer.filename}:{filename}")
            with writer.open(filename, "w") as f:
                f.write("[\n".encode("utf-8"))
                last = len(items) - 1
                for i, item in enumerate(items):
                    cls._cleanup_entity(cls_, item)
                    f.write(item.to_json().encode("utf-8"))
                    if i != last:
                        f.write(",".encode("utf-8"))
                    f.write("\n".encode("utf-8"))
                f.write("]\n".encode("utf-8"))

    @staticmethod
    def _import(reader: ZipFile, user_id: str = None):
        for file_info in reader.filelist:
            full_name = splitext(file_info.orig_filename)[0]
            print(f"Reading {reader.filename}:{full_name}...")
            module_name, _, class_name = full_name.rpartition(".")
            module = importlib.import_module(module_name)
            cls_: Type[mongoengine.Document] = getattr(module, class_name)

            with reader.open(file_info) as f:
                for item in tqdm(
                    f.readlines(),
                    desc=f"Writing {cls_.__name__.lower()}s into database",
                    unit="doc",
                ):
                    item = (
                        item.decode("utf-8")
                        .strip()
                        .lstrip("[")
                        .rstrip("]")
                        .rstrip(",")
                        .strip()
                    )
                    if not item:
                        continue
                    doc = cls_.from_json(item)
                    if user_id is not None and hasattr(doc, "user"):
                        doc.user = user_id
                    doc.save(force_insert=True)