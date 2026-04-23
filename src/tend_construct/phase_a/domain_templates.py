from __future__ import annotations

from pathlib import Path

from tend_core import DomainTemplate, load_json, write_json


def default_assets_root() -> Path:
    return Path(__file__).resolve().parents[3] / "assets" / "domain_templates"


def load_domain_templates(assets_root: Path | None = None) -> list[DomainTemplate]:
    root = assets_root or default_assets_root()
    templates: list[DomainTemplate] = []
    for path in sorted(root.glob("*.json")):
        templates.append(DomainTemplate.from_dict(load_json(path)))
    return templates


def build_domain_catalog(templates: list[DomainTemplate]) -> list[dict]:
    catalog: list[dict] = []
    for template in templates:
        catalog.append(
            {
                "domain_id": template.domain_id,
                "name": template.name,
                "entity_collections": [entity["collection"] for entity in template.entities],
                "f_topology_hints": template.f_topology_hints,
                "phenomenon_classes": sorted(
                    {item["phenomenon_class"] for item in template.phenomenon_blueprints}
                ),
            }
        )
    return catalog


def write_domain_catalog(templates: list[DomainTemplate], out_path: Path) -> None:
    write_json(out_path, build_domain_catalog(templates))
