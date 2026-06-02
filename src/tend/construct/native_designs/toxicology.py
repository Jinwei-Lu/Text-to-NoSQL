from __future__ import annotations

from typing import Any

from ..native_recipe import NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__


def build_native_recipe(source: Any, db_id: str) -> NativeMigrationRecipe:
    source.schema(db_id)
    return recipe(
        db_id,
        version=DESIGN_VERSION,
        design_goal=(
            "Represent molecular graphs with element-keyed atom counts, bond-type "
            "buckets, and polymorphic atom/bond components."
        ),
        collections=[
            collection(
                "molecule_graphs",
                purpose="Molecule documents with dynamic atom and bond summaries.",
                source_tables=["molecule", "atom", "bond", "connected"],
                transforms=[
                    transform(
                        "atom_counts_by_element",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="molecule",
                        child_table="atom",
                        join=join("molecule.molecule_id", "atom.molecule_id"),
                        target_field="atom_counts_by_element",
                        key=expr("atom.element", "atom.element"),
                        values={
                            "atom_count": expr("count(atom.atom_id)", "atom.atom_id"),
                            "molecule_count": expr(
                                "count(distinct atom.molecule_id)",
                                "atom.molecule_id",
                            ),
                        },
                    ),
                    transform(
                        "molecule_label_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="molecule_tags",
                        tags={
                            "carcinogenic": {
                                "condition": "molecule.label == '+'",
                                "provenance": ["molecule.label"],
                            },
                            "non_carcinogenic": {
                                "condition": "molecule.label == '-'",
                                "provenance": ["molecule.label"],
                            },
                        },
                    ),
                    transform(
                        "bond_counts_by_type",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="molecule",
                        child_table="bond",
                        join=join("molecule.molecule_id", "bond.molecule_id"),
                        target_field="bond_counts_by_type",
                        key=expr("bond.bond_type", "bond.bond_type"),
                        values={
                            "bond_count": expr("count(bond.bond_id)", "bond.bond_id"),
                        },
                    ),
                ],
            ),
            collection(
                "molecular_components",
                purpose="Typed atom and bond components for graph-native queries.",
                source_tables=["atom", "bond", "connected"],
                transforms=[
                    transform(
                        "component_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="component_type",
                        variants={
                            "atom": {
                                "source_table": "atom",
                                "fields": {
                                    "component_id": expr(
                                        "concat('atom:', atom.atom_id)",
                                        "atom.atom_id",
                                    ),
                                    "molecule_id": field_source("atom.molecule_id"),
                                    "element": field_source("atom.element"),
                                },
                            },
                            "bond": {
                                "source_table": "bond",
                                "fields": {
                                    "component_id": expr(
                                        "concat('bond:', bond.bond_id)",
                                        "bond.bond_id",
                                    ),
                                    "molecule_id": field_source("bond.molecule_id"),
                                    "bond_type": field_source("bond.bond_type"),
                                },
                            },
                        },
                    )
                ],
            ),
        ],
    )
