from __future__ import annotations

import re
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.studio.models import ContentKind, Importance, SourceKind
from src.studio.service import StudioService
from src.web.app import create_app


def _image_bytes(color: str = "blue") -> bytes:
    image = Image.new("RGB", (800, 1000), color=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_archives_deduplicates_analyzes_and_renders(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("Jacket")
    content = _image_bytes()
    asset, duplicate = service.import_asset(project.id, "front.png", content, 5_000_000)
    duplicate_asset, duplicate = service.import_asset(project.id, "copy.png", content, 5_000_000)
    assert duplicate
    assert duplicate_asset.id == asset.id
    assert service.resolve_asset_path(project.id, asset.id, "original").read_bytes() == content
    assert service.resolve_asset_path(project.id, asset.id, "thumbnail").is_file()
    analysis = service.analyze_asset(project.id, asset.id)
    assert service.analyze_asset(project.id, asset.id).id == analysis.id
    rendered = service.render_annotations(project.id, asset.id)
    assert rendered.is_file()
    assert service.resolve_asset_path(project.id, asset.id, "original").read_bytes() == content


def test_overrides_and_compiler_excludes_competitor_facts(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("Details")
    asset, _ = service.import_asset(project.id, "detail.png", _image_bytes(), 5_000_000)
    analysis = service.analyze_asset(project.id, asset.id)
    region = analysis.detail_regions[0]
    service.update_analysis(
        project.id, asset.id, SourceKind.OWN_CAPTURE, ContentKind.DETAIL, ["pocket"]
    )
    service.update_region(
        project.id,
        asset.id,
        region.id,
        detail_type="pocket",
        importance=Importance.CRITICAL,
        label="Zipper pocket",
        confirmed=True,
    )
    spec = service.compile_product_spec(project.id)
    assert spec.facts[0].key == "pocket"
    service.update_analysis(project.id, asset.id, SourceKind.COMPETITOR_REFERENCE)
    assert service.compile_product_spec(project.id).facts == []
    assert asset.id in service.compile_reference_bundle(project.id).style_asset_ids


def test_bbox_validation_and_invalid_images(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("Safe")
    with pytest.raises(ValueError):
        service.import_asset(project.id, "bad.png", b"not image", 10)
    with pytest.raises(ValueError):
        service.import_asset(project.id, "large.png", _image_bytes(), 10)
    from src.studio.models import NormalizedBBox

    with pytest.raises(ValueError):
        NormalizedBBox(x=0.9, y=0.1, width=0.2, height=0.2)


def test_twenty_asset_offline_dry_run(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("Twenty uploads")
    assets = [
        service.import_asset(
            project.id, f"detail-{index}.png", _image_bytes(f"#{index:06x}"), 5_000_000
        )[0]
        for index in range(20)
    ]
    assert len(service.get_record(project.id).assets) == 20
    analysis = service.analyze_asset(project.id, assets[0].id)
    assert analysis.analyzer_name == "mock"
    assert service.render_annotations(project.id, assets[0].id).is_file()
    assert service.compile_reference_bundle(project.id).detail_board_path


def test_studio_pages_require_auth_and_create_project(temp_config) -> None:
    client = TestClient(create_app(temp_config))
    assert client.get("/studio", follow_redirects=False).status_code == 307
    login = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    client.post(
        "/login", data={"username": "admin", "password": "test-password", "csrf_token": csrf}
    )
    page = client.get("/studio")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/studio/projects",
        data={"name": "Web project", "target_platform": "temu", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    project_url = response.headers["location"]
    project = client.get(project_url)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', project.text).group(1)
    upload = client.post(
        f"{project_url}/assets",
        data={"csrf_token": csrf},
        files={"files": ("detail.png", _image_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    assert "Archive images" in client.get(project_url).text
