from __future__ import annotations

import pytest

from src.core.models import ProductInfo


def test_product_schema_valid() -> None:
    data = {
        "sku": "F116-Black",
        "product": {"category": "jacket", "color": "black"},
        "fabric": {"name": "Ripstop Fabric"},
        "features": [{"id": "pocket", "title": "Pocket", "subtitle": "Storage"}],
        "images": {"front": "originals/front.png"},
        "sizes": {"M": {"chest_cm": 116, "length_cm": 74}},
    }
    product = ProductInfo(**data)
    assert product.sku == "F116-Black"
    assert product.product["category"] == "jacket"


def test_product_sku_validation_empty() -> None:
    with pytest.raises(ValueError):
        ProductInfo(sku="")


def test_product_sku_validation_path_traversal() -> None:
    with pytest.raises(ValueError):
        ProductInfo(sku="../etc/passwd")
    with pytest.raises(ValueError):
        ProductInfo(sku="F116\\Black")
