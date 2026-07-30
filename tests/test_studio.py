from __future__ import annotations

import re
import threading
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.studio.analyzers import AnalyzerNotConfigured, MockAssetAnalyzer
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
    analysis = service.analyze_asset(project.id, asset.id, MockAssetAnalyzer())
    assert service.analyze_asset(project.id, asset.id, MockAssetAnalyzer()).id == analysis.id
    rendered = service.render_annotations(project.id, asset.id)
    assert rendered.is_file()
    assert service.resolve_asset_path(project.id, asset.id, "original").read_bytes() == content


def test_overrides_and_compiler_excludes_competitor_facts(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("Details")
    asset, _ = service.import_asset(project.id, "detail.png", _image_bytes(), 5_000_000)
    analysis = service.analyze_asset(project.id, asset.id, MockAssetAnalyzer())
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
    analysis = service.analyze_asset(project.id, assets[0].id, MockAssetAnalyzer())
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


def test_production_analyzer_is_not_silently_mocked(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("No implicit mock")
    asset, _ = service.import_asset(project.id, "asset.png", _image_bytes(), 5_000_000)
    with pytest.raises(AnalyzerNotConfigured):
        service.analyze_asset(project.id, asset.id)


def test_projects_cannot_read_or_modify_each_others_assets(temp_config) -> None:
    service = StudioService(temp_config)
    first = service.create_project("First")
    second = service.create_project("Second")
    asset, _ = service.import_asset(second.id, "asset.png", _image_bytes(), 5_000_000)
    service.analyze_asset(second.id, asset.id, MockAssetAnalyzer())
    with pytest.raises(KeyError):
        service.resolve_asset_path(first.id, asset.id, "original")
    with pytest.raises(KeyError):
        service.update_analysis(first.id, asset.id, SourceKind.OWN_CAPTURE)


def test_reference_roles_keep_clean_details_separate_from_annotations(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("References")
    asset, _ = service.import_asset(project.id, "detail.png", _image_bytes(), 5_000_000)
    analysis = service.analyze_asset(project.id, asset.id, MockAssetAnalyzer())
    service.render_annotations(project.id, asset.id)
    bundle = service.compile_reference_bundle(project.id)
    by_role = {item.role.value: item for item in bundle.references if item.asset_id == asset.id}
    assert by_role["detail_reference_clean"].relative_path is None
    assert by_role["human_annotation_preview"].relative_path.endswith(f"{asset.id}.png")
    assert Image.open(service.reference_board_path(project.id)).getpixel((0, 0)) == (255, 255, 255)
    assert analysis.asset_id == asset.id


def test_concurrent_project_updates_do_not_lose_changes(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("Concurrent")
    first, _ = service.import_asset(project.id, "one.png", _image_bytes("red"), 5_000_000)
    second, _ = service.import_asset(project.id, "two.png", _image_bytes("green"), 5_000_000)
    service.analyze_asset(project.id, first.id, MockAssetAnalyzer())
    service.analyze_asset(project.id, second.id, MockAssetAnalyzer())
    barrier = threading.Barrier(2)

    def update(asset_id: str, content: ContentKind) -> None:
        barrier.wait()
        service.update_analysis(project.id, asset_id, content_kind=content)

    left = threading.Thread(target=update, args=(first.id, ContentKind.PRODUCT_FULL_FRONT))
    right = threading.Thread(target=update, args=(second.id, ContentKind.PRODUCT_FULL_BACK))
    left.start()
    right.start()
    left.join()
    right.join()
    record = service.get_record(project.id)
    effective = {item.asset_id: item.content_kind.effective_value for item in record.analyses}
    assert effective[first.id] == ContentKind.PRODUCT_FULL_FRONT
    assert effective[second.id] == ContentKind.PRODUCT_FULL_BACK


def test_product_spec_is_invalidated_and_confirmed_evidence_wins(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("Facts")
    first, _ = service.import_asset(project.id, "first.png", _image_bytes("red"), 5_000_000)
    second, _ = service.import_asset(project.id, "second.png", _image_bytes("green"), 5_000_000)
    for asset in (first, second):
        analysis = service.analyze_asset(project.id, asset.id, MockAssetAnalyzer())
        service.update_analysis(project.id, asset.id, SourceKind.OWN_CAPTURE, ContentKind.DETAIL)
        service.update_region(
            project.id,
            asset.id,
            analysis.detail_regions[0].id,
            detail_type="pocket",
            importance=Importance.CRITICAL,
            label="zip pocket" if asset.id == first.id else "button pocket",
            confirmed=asset.id == first.id,
        )
    spec = service.compile_product_spec(project.id)
    assert [(fact.value, fact.status) for fact in spec.facts] == [("zip pocket", "strong")]
    service.update_analysis(project.id, first.id, SourceKind.COMPETITOR_REFERENCE)
    assert service.get_record(project.id).product_spec is None
