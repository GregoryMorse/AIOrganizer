from __future__ import annotations

from ai_organizer.application.inventory_query import InventoryQueryService

ITEMS = [
    {
        "id": "root-doc",
        "root_id": "root",
        "relative_path": "report.docx",
        "parent_path": "",
        "extension": ".docx",
        "size": 100,
        "modified_ns": 10,
    },
    {
        "id": "nested-doc",
        "root_id": "root",
        "relative_path": "a/b/notes.docx",
        "parent_path": "a/b",
        "extension": ".docx",
        "size": 200,
        "modified_ns": 20,
    },
    {
        "id": "tex",
        "root_id": "root",
        "relative_path": "paper/main.tex",
        "parent_path": "paper",
        "extension": ".tex",
        "size": 50,
        "modified_ns": 30,
    },
]


def test_recursive_glob_includes_root_and_nested_files() -> None:
    result = InventoryQueryService(ITEMS).search("/**/*.docx")

    assert result["total"] == 2
    assert {item["id"] for item in result["items"]} == {"root-doc", "nested-doc"}


def test_single_star_does_not_cross_directories() -> None:
    result = InventoryQueryService(ITEMS).search("*.docx")

    assert [item["id"] for item in result["items"]] == ["root-doc"]


def test_summary_counts_extensions() -> None:
    result = InventoryQueryService(ITEMS, cache_stats={"fresh": 3}).summary()

    assert result["by_extension"] == {".docx": 2, ".tex": 1}
    assert result["total_file_bytes"] == 350
    assert result["metadata_cache"] == {"fresh": 3}


def test_organization_taxonomy_is_available_as_approved_tool_context() -> None:
    context = {"categories": [{"id": "research"}], "folder_depth_policy": {"maximum_depth": 3}}

    result = InventoryQueryService(ITEMS, organization_context=context).organization_taxonomy()

    assert result == context


def test_folder_tree_is_bounded_and_preserves_parent_depth() -> None:
    items = [
        {
            "id": "folder-a",
            "root_id": "root",
            "relative_path": "a",
            "is_dir": True,
            "child_folder_count": 1,
        },
        {
            "id": "folder-b",
            "root_id": "root",
            "relative_path": "a/b",
            "is_dir": True,
            "child_file_count": 2,
        },
        {
            "id": "folder-c",
            "root_id": "root",
            "relative_path": "a/b/c",
            "is_dir": True,
        },
    ]

    result = InventoryQueryService(items).folder_tree(max_depth=2)

    assert [value["path"] for value in result["folders"]] == ["a", "a/b"]
    assert result["folders"][1]["parent_path"] == "a"
    assert result["folders"][1]["depth"] == 2
