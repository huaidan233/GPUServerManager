"""Installed, smoke-tested runtime images; a missing image is never advertised as ready."""
import json
import os
from pathlib import Path
from fastapi import HTTPException
from gsm_api.domain import ImageRecord
from gsm_api.fixtures import IMAGES
from gsm_api.store import list_pulled_images
from gsm_api import typed_records


def catalog_images() -> list[ImageRecord]:
    configured = os.environ.get('GSM_ENVIRONMENT_CATALOG_PATH')
    path = Path(configured or '/etc/gpu-server-manager/inventory/environments.json')
    if not configured and not path.exists():
        # The legacy installer only builds the PyTorch image. The other fixtures
        # are demonstration data, not installed production environments.
        return IMAGES[:1] if os.environ.get('GSM_ENV') == 'production' else IMAGES
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('oversized catalog')
        document = json.loads(path.read_text(encoding='utf-8'))
        if document['version'] != 1 or not document['images']:
            raise ValueError('invalid catalog')
        images = [ImageRecord.model_validate(value) for value in document['images']]
        references = [f'{image.name}:{image.tag}' for image in images]
        if len(references) != len(set(references)):
            raise ValueError('duplicate image reference')
        for image in images:
            if image.environment and (image.environment not in {'python','go','cuda','ubuntu','python-base'} or not image.runtime_version
                or not image.server_ids or not image.ssh_ready or not image.trusted
                or not image.digest or not image.digest.startswith('sha256:') or len(image.digest) != 71):
                raise ValueError('incomplete prepared environment')
        return images
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise HTTPException(status_code=503,detail='环境镜像目录不可用，请联系管理员检查。') from exc


def available_images() -> list[dict]:
    images = [image.model_dump() for image in catalog_images()]
    for image in images:
        policy = typed_records.get("image_policy", image.get("digest") or f"{image['name']}:{image['tag']}") or {}
        image["creation_enabled"] = policy.get("creation_enabled", image["creation_enabled"])
        preferred = typed_records.get("image_preference", image.get("environment") or image["name"])
        if preferred:
            image["recommended"] = preferred.get("digest") == image.get("digest")
    known = {f"{image['name']}:{image['tag']}" for image in images}
    return images + [image for image in list_pulled_images()
                     if f"{image['name']}:{image.get('tag','latest')}" not in known]


def require_enabled(reference: str) -> None:
    image = next((item for item in available_images() if reference in {f"{item['name']}:{item.get('tag', 'latest')}", item['name'] if item.get('tag', 'latest') == 'latest' else ''}), None)
    if image and not image.get("creation_enabled", True):
        raise HTTPException(status_code=409, detail="该镜像版本已暂停新建，请选择其他版本；已有容器不受影响。")


def selected_environment(reference: str) -> ImageRecord | None:
    return next((image for image in catalog_images()
                 if reference == f'{image.name}:{image.tag}' or (image.tag == 'latest' and reference == image.name)),None)
