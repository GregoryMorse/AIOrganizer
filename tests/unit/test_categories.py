from __future__ import annotations

from pathlib import Path

from ai_organizer.domain.categories import (
    CategoryResolver,
    DestinationCandidate,
    DestinationRouter,
)
from ai_organizer.domain.models import (
    CategoryAssignment,
    CategoryDefinition,
    CloudPolicy,
    FolderRole,
    Sensitivity,
)


def test_most_restrictive_policy_and_protected_routing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    personal = CategoryDefinition(
        "Personal",
        id="personal",
        sensitivity=Sensitivity.RESTRICTED,
        cloud_policy=CloudPolicy.NONE,
    )
    assignments = [CategoryAssignment(root, {"personal"}, {FolderRole.INBOX}, id="a1")]
    policy = CategoryResolver().effective_policy(
        root / "file.pdf",
        assignments,
        {"personal": personal},
        CloudPolicy.TEXT_AND_IMAGES,
    )
    assert policy.cloud_policy == CloudPolicy.NONE
    assert policy.sensitivity == Sensitivity.RESTRICTED
    unprotected = DestinationCandidate(
        tmp_path / "documents",
        frozenset({"personal"}),
        frozenset({FolderRole.DESTINATION}),
    )
    protected = DestinationCandidate(
        tmp_path / "vault",
        frozenset({"personal"}),
        frozenset({FolderRole.DESTINATION, FolderRole.PROTECTED}),
    )
    ranked = DestinationRouter().rank(policy, [unprotected, protected])
    assert ranked[0].path == protected.path
    assert any("Protected" in reason for result in ranked for reason in result.blocked_reasons)


def test_unapproved_assignment_does_not_affect_policy(tmp_path: Path) -> None:
    assignment = CategoryAssignment(
        tmp_path,
        {"secret"},
        {FolderRole.PROTECTED},
        approved=False,
    )
    policy = CategoryResolver().effective_policy(
        tmp_path / "x", [assignment], {}, CloudPolicy.TEXT_AND_IMAGES
    )
    assert not policy.category_ids
    assert not policy.roles


def test_category_and_folder_tags_are_combined_in_effective_policy(tmp_path: Path) -> None:
    category = CategoryDefinition(
        "Research",
        id="research",
        default_tag_ids={"reference"},
    )
    assignment = CategoryAssignment(
        tmp_path,
        {"research"},
        {FolderRole.DESTINATION},
        tag_ids={"active"},
    )

    policy = CategoryResolver().effective_policy(
        tmp_path / "paper.pdf",
        [assignment],
        {"research": category},
        CloudPolicy.METADATA_ONLY,
    )

    assert policy.tag_ids == frozenset({"reference", "active"})
