from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    CategoryDefinition,
    CloudPolicy,
    FolderRole,
    Sensitivity,
    TagDefinition,
    TagFacet,
)


@dataclass(frozen=True, slots=True)
class FolderDepthPolicy:
    preferred_depth: int = 2
    maximum_depth: int = 3
    adaptive: bool = True

    def validated(self) -> FolderDepthPolicy:
        preferred = max(1, min(12, int(self.preferred_depth)))
        maximum = max(preferred, min(12, int(self.maximum_depth)))
        return FolderDepthPolicy(preferred, maximum, bool(self.adaptive))


@dataclass(frozen=True, slots=True)
class OrganizationProfile:
    key: str
    name: str
    categories: tuple[CategoryDefinition, ...]
    tags: tuple[TagDefinition, ...]
    depth_policy: FolderDepthPolicy
    workspace_guidance: str


@dataclass(frozen=True, slots=True)
class SourcePolicyPreset:
    key: str
    name: str
    description: str
    roles: frozenset[FolderRole]
    category_keys: frozenset[str] = frozenset()
    tag_keys: frozenset[str] = frozenset()
    cloud_policy: CloudPolicy = CloudPolicy.LOCAL_ONLY
    max_hierarchy_depth: int | None = None
    exclusions: tuple[str, ...] = ()


def recommend_folder_depth(
    item_count: int, folder_count: int, policy: FolderDepthPolicy
) -> int:
    """Recommend a useful target; the maximum remains a hard policy ceiling."""
    policy = policy.validated()
    if not policy.adaptive:
        return policy.preferred_depth
    if item_count < 250 and folder_count < 40:
        return 1
    if item_count < 10_000 and folder_count < 1_000:
        return min(2, policy.maximum_depth)
    if item_count < 100_000 and folder_count < 10_000:
        return min(max(2, policy.preferred_depth), policy.maximum_depth)
    return policy.maximum_depth


def general_organization_profile() -> OrganizationProfile:
    tags = _general_tags()
    categories = _general_categories()
    return OrganizationProfile(
        key="general-v1",
        name="General purpose organization",
        categories=tuple(categories),
        tags=tuple(tags),
        depth_policy=FolderDepthPolicy(),
        workspace_guidance=(
            "Prefer clear, conventional names and the shallowest hierarchy that separates meaningful "
            "groups. Reuse existing vocabulary when it is consistent. Treat categories as semantic "
            "domains, tags as orthogonal properties, and source roles as workflow authority. Avoid "
            "single-item folders, speculative categories, and hierarchies deeper than the active policy. "
            "Preserve project, application, archive, backup, course, and submission bundles as units."
        ),
    )


def general_source_presets() -> tuple[SourcePolicyPreset, ...]:
    return (
        SourcePolicyPreset(
            "mixed-inbox",
            "Mixed inbox",
            "Untriaged downloads and incoming material.",
            frozenset({FolderRole.INBOX, FolderRole.DOWNLOADS}),
            frozenset({"uncategorized"}),
            frozenset({"incoming", "needs-review", "downloaded"}),
            CloudPolicy.METADATA_ONLY,
            2,
        ),
        SourcePolicyPreset(
            "software-downloads",
            "Software download library",
            "Retained installers, packages, drivers, archives, and images.",
            frozenset({FolderRole.DOWNLOADS, FolderRole.DESTINATION, FolderRole.ARCHIVE}),
            frozenset({"software.installers"}),
            frozenset({"installer", "downloaded", "reference", "security-review"}),
            CloudPolicy.METADATA_ONLY,
            3,
        ),
        SourcePolicyPreset(
            "portable-apps",
            "Portable applications",
            "Runnable application bundles whose mutable state must be preserved.",
            frozenset({FolderRole.DESTINATION, FolderRole.PROTECTED}),
            frozenset({"software.portable"}),
            frozenset({"portable-app", "active", "mutable-app-data"}),
            CloudPolicy.METADATA_ONLY,
            2,
        ),
        SourcePolicyPreset(
            "source-repositories",
            "First-party source repositories",
            "Canonical source repositories and substantial projects.",
            frozenset({FolderRole.DESTINATION, FolderRole.PROTECTED}),
            frozenset({"software.repositories"}),
            frozenset({"source-repository", "active"}),
            CloudPolicy.METADATA_ONLY,
            2,
            ("**/.git/**",),
        ),
        SourcePolicyPreset(
            "dependency-sources",
            "Dependency source libraries",
            "Third-party and vendored dependency source trees.",
            frozenset({FolderRole.DESTINATION, FolderRole.PROTECTED}),
            frozenset({"software.dependencies"}),
            frozenset({"dependency-source", "vendored", "reference"}),
            CloudPolicy.METADATA_ONLY,
            2,
        ),
        SourcePolicyPreset(
            "personal-records",
            "Protected personal records",
            "Personal, financial, health, identity, and household records.",
            frozenset({FolderRole.DESTINATION, FolderRole.PROTECTED}),
            frozenset({"personal"}),
            frozenset({"personal-record"}),
            CloudPolicy.LOCAL_ONLY,
            3,
        ),
        SourcePolicyPreset(
            "professional-work",
            "Professional work",
            "Client, engagement, operational, and professional deliverable material.",
            frozenset({FolderRole.DESTINATION, FolderRole.PROTECTED}),
            frozenset({"work"}),
            frozenset({"business-record", "professional", "active"}),
            CloudPolicy.METADATA_ONLY,
            3,
        ),
        SourcePolicyPreset(
            "research-reference",
            "Research reference library",
            "Downloaded publications, manuals, standards, data, and research notes.",
            frozenset({FolderRole.DESTINATION, FolderRole.ARCHIVE}),
            frozenset({"research"}),
            frozenset({"research-document", "reference", "downloaded"}),
            CloudPolicy.METADATA_ONLY,
            3,
        ),
        SourcePolicyPreset(
            "education",
            "Education and coursework",
            "Coursework, school projects, learning materials, and administration.",
            frozenset({FolderRole.DESTINATION, FolderRole.PROTECTED}),
            frozenset({"education"}),
            frozenset({"coursework", "active"}),
            CloudPolicy.METADATA_ONLY,
            3,
        ),
        SourcePolicyPreset(
            "teaching",
            "Teaching migration source",
            "Teaching material and student work awaiting reviewed migration.",
            frozenset({FolderRole.INBOX, FolderRole.PROTECTED}),
            frozenset({"education.teaching"}),
            frozenset({"teaching-material", "migration-candidate"}),
            CloudPolicy.METADATA_ONLY,
            3,
        ),
        SourcePolicyPreset(
            "legacy-projects",
            "Legacy project migration",
            "Older project trees awaiting repository detection and reviewed migration.",
            frozenset({FolderRole.INBOX, FolderRole.PROTECTED}),
            frozenset({"software.repositories", "software.small-projects"}),
            frozenset({"source-repository", "legacy", "migration-candidate"}),
            CloudPolicy.METADATA_ONLY,
            2,
        ),
    )


def _tag(
    key: str,
    name: str,
    facet: TagFacet,
    description: str,
    *,
    applies_to: set[str] | None = None,
) -> TagDefinition:
    return TagDefinition(
        name,
        facet,
        id=f"tag_general_{key.replace('-', '_')}",
        key=key,
        description=description,
        guidance=description,
        applies_to=applies_to or {"file", "folder", "software", "email"},
    )


def _general_tags() -> list[TagDefinition]:
    return [
        _tag("installer", "Installer", TagFacet.CONTENT, "Installers, packages, updates, and setup media."),
        _tag("portable-app", "Portable application", TagFacet.CONTENT, "Runnable application bundle that may also contain mutable user state."),
        _tag("source-repository", "Source repository", TagFacet.CONTENT, "First-party source project or repository; preserve its project boundary."),
        _tag("dependency-source", "Dependency source", TagFacet.CONTENT, "Third-party or vendored source dependency, distinct from first-party projects."),
        _tag("research-document", "Research document", TagFacet.CONTENT, "Paper, book, manual, standard, or other reference publication."),
        _tag("dataset", "Dataset", TagFacet.CONTENT, "Structured or collected research data and its documentation."),
        _tag("teaching-material", "Teaching material", TagFacet.CONTENT, "Course delivery, exercises, solutions, examinations, or instructional assets."),
        _tag("student-submission", "Student submission", TagFacet.CONTENT, "Submitted work whose provenance and original state may matter."),
        _tag("personal-record", "Personal record", TagFacet.CONTENT, "Identity, household, health, legal, career, or administrative record."),
        _tag("financial-record", "Financial record", TagFacet.CONTENT, "Statement, invoice, tax, account, transaction, or financial evidence."),
        _tag("business-record", "Business record", TagFacet.CONTENT, "Client, operational, contractual, administrative, or professional record."),
        _tag("deliverable", "Deliverable", TagFacet.CONTENT, "Reviewed output intended for a client, stakeholder, publication, or submission."),
        _tag("driver-firmware", "Driver or firmware", TagFacet.CONTENT, "Hardware driver, firmware, BIOS, or device support package."),
        _tag("media", "Media", TagFacet.CONTENT, "Image, audio, or video material."),
        _tag("incoming", "Incoming", TagFacet.LIFECYCLE, "New or untriaged material awaiting review."),
        _tag("active", "Active", TagFacet.LIFECYCLE, "Currently maintained or frequently used material."),
        _tag("reference", "Reference", TagFacet.LIFECYCLE, "Retained primarily for consultation rather than modification."),
        _tag("legacy", "Legacy", TagFacet.LIFECYCLE, "Older material requiring preservation, migration, or compatibility review."),
        _tag("archived", "Archived", TagFacet.LIFECYCLE, "Intentionally retained but no longer active."),
        _tag("generated", "Generated", TagFacet.STATE, "Reproducible generated output; cleanup still depends on project context."),
        _tag("build-output", "Build output", TagFacet.STATE, "Compiler, linker, packaging, or documentation build artifact."),
        _tag("cache", "Cache", TagFacet.STATE, "Regenerable tool or application cache."),
        _tag("vendored", "Vendored", TagFacet.STATE, "Externally sourced content intentionally stored inside another bundle."),
        _tag("mutable-app-data", "Mutable application data", TagFacet.STATE, "User configuration, databases, sessions, or work product stored beside an application."),
        _tag("needs-review", "Needs review", TagFacet.STATE, "Classification or disposition remains unresolved."),
        _tag("migration-candidate", "Migration candidate", TagFacet.STATE, "Likely belongs in a better canonical destination after review."),
        _tag("duplicate-candidate", "Duplicate candidate", TagFacet.STATE, "Metadata suggests another retained copy or version may exist."),
        _tag("stale-version", "Stale version", TagFacet.STATE, "Software or downloaded material may have a newer relevant version."),
        _tag("security-review", "Security review", TagFacet.STATE, "Requires malware, trust, provenance, or execution-risk review."),
        _tag("defender-detected", "Defender detected", TagFacet.STATE, "Windows Defender reported a detection for this item."),
        _tag("downloaded", "Downloaded", TagFacet.ORIGIN, "Acquired from the web or another external download channel."),
        _tag("cloud-synced", "Cloud synchronized", TagFacet.ORIGIN, "Stored in a synchronized cloud-backed location."),
        _tag("email-derived", "Email derived", TagFacet.ORIGIN, "Originated as a message or email attachment."),
        _tag("scanned", "Scanned", TagFacet.ORIGIN, "Captured from paper or an image-based document workflow."),
        _tag("backup", "Backup", TagFacet.STATE, "Backup or recovery copy whose retention policy differs from active material."),
        _tag("encrypted", "Encrypted", TagFacet.STATE, "Encrypted or password-protected container or document."),
        _tag("digitally-signed", "Digitally signed", TagFacet.STATE, "Contains a verifiable digital signature or publisher signature."),
        _tag("windows", "Windows", TagFacet.TECHNOLOGY, "Windows-specific software, project, or artifact."),
        _tag("linux", "Linux", TagFacet.TECHNOLOGY, "Linux-specific software, project, or artifact."),
        _tag("macos", "macOS", TagFacet.TECHNOLOGY, "macOS-specific software, project, or artifact."),
        _tag("python", "Python", TagFacet.TECHNOLOGY, "Python project, package, environment, or tooling."),
        _tag("dotnet", ".NET", TagFacet.TECHNOLOGY, ".NET project, package, assembly, or tooling."),
        _tag("java", "Java/JVM", TagFacet.TECHNOLOGY, "Java, Kotlin, JVM project, archive, or tooling."),
        _tag("native-code", "Native code", TagFacet.TECHNOLOGY, "C, C++, Rust, assembly, or native binary project."),
        _tag("web-project", "Web project", TagFacet.TECHNOLOGY, "Browser, server-side web, or JavaScript/TypeScript project."),
        _tag("coursework", "Coursework", TagFacet.AUDIENCE, "Created for learning, assessment, or a course requirement."),
        _tag("professional", "Professional", TagFacet.AUDIENCE, "Created for professional or employment use."),
    ]


def _category(
    key: str,
    name: str,
    description: str,
    *,
    parent: str | None = None,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    cloud: CloudPolicy = CloudPolicy.INHERIT,
    tags: set[str] | None = None,
    folder: bool = False,
) -> CategoryDefinition:
    return CategoryDefinition(
        name,
        id=f"cat_general_{key.replace('-', '_')}",
        parent_id=f"cat_general_{parent.replace('-', '_')}" if parent else None,
        semantic_key=key,
        description=description,
        guidance=description,
        sensitivity=sensitivity,
        cloud_policy=cloud,
        allowed_roles={FolderRole.DESTINATION, FolderRole.ARCHIVE},
        max_hierarchy_depth=3,
        default_tag_ids=set(tags or ()),
        suggest_as_folder=folder,
    )


def _general_categories() -> list[CategoryDefinition]:
    restricted: dict[str, Any] = {
        "cloud": CloudPolicy.LOCAL_ONLY,
        "sensitivity": Sensitivity.RESTRICTED,
    }
    confidential: dict[str, Any] = {
        "cloud": CloudPolicy.METADATA_ONLY,
        "sensitivity": Sensitivity.CONFIDENTIAL,
    }
    return [
        _category("personal", "Personal", "Private personal and household domains.", **confidential),
        _category("personal.identity-legal", "Identity & Legal", "Identity, immigration, legal, and official records.", parent="personal", tags={"tag_general_personal_record"}, folder=True, **restricted),
        _category("personal.finance", "Finance", "Banking, tax, investments, bills, insurance, and financial evidence.", parent="personal", tags={"tag_general_financial_record"}, folder=True, **restricted),
        _category("personal.health", "Health", "Medical, dental, insurance, and wellbeing records.", parent="personal", tags={"tag_general_personal_record"}, folder=True, **restricted),
        _category("personal.household", "Household & Property", "Home, utilities, vehicles, purchases, warranties, and household administration.", parent="personal", tags={"tag_general_personal_record"}, folder=True, **confidential),
        _category("personal.career", "Career & Employment", "Career, employment, applications, credentials, and professional records.", parent="personal", tags={"tag_general_personal_record", "tag_general_professional"}, folder=True, **confidential),
        _category("personal.travel", "Travel", "Travel planning, reservations, visas, and trip records.", parent="personal", tags={"tag_general_personal_record"}, folder=True, **confidential),
        _category("work", "Work", "Professional, client, engagement, and organizational material.", **confidential),
        _category("work.clients", "Clients & Engagements", "Client- or engagement-specific records and working material.", parent="work", tags={"tag_general_business_record", "tag_general_professional"}, folder=True, **confidential),
        _category("work.deliverables", "Deliverables", "Reviewed outputs intended for stakeholders, clients, or publication.", parent="work", tags={"tag_general_deliverable", "tag_general_professional"}, folder=True, **confidential),
        _category("work.operations", "Operations & Administration", "Contracts, planning, process, meetings, and organizational administration.", parent="work", tags={"tag_general_business_record", "tag_general_professional"}, folder=True, **confidential),
        _category("work.reference", "Professional Reference", "Standards, templates, reusable knowledge, and professional reference material.", parent="work", tags={"tag_general_reference", "tag_general_professional"}, folder=True, **confidential),
        _category("education", "Education", "Learning, teaching, courses, and institutional administration.", **confidential),
        _category("education.coursework", "Coursework", "Course notes, assignments, projects, and learning materials.", parent="education", tags={"tag_general_coursework"}, folder=True, **confidential),
        _category("education.teaching", "Teaching", "Teaching materials, assessments, classes, and student work.", parent="education", tags={"tag_general_teaching_material"}, folder=True, **confidential),
        _category("education.administration", "Admissions & Administration", "Applications, enrollment, transcripts, policies, and institutional records.", parent="education", tags={"tag_general_personal_record"}, folder=True, **restricted),
        _category("research", "Research", "Reference literature, research data, and knowledge work."),
        _category("research.publications", "Papers & Books", "Papers, books, manuals, standards, and downloaded publications.", parent="research", tags={"tag_general_research_document", "tag_general_reference"}, folder=True),
        _category("research.data", "Data & Datasets", "Datasets, measurements, corpora, and their documentation.", parent="research", tags={"tag_general_dataset"}, folder=True),
        _category("research.notes", "Notes & Bibliography", "Research notes, summaries, citations, and bibliographic material.", parent="research", folder=True),
        _category("software", "Software", "Applications, source projects, dependencies, and distribution material."),
        _category("software.repositories", "Source Projects", "First-party source repositories and substantial software projects.", parent="software", tags={"tag_general_source_repository"}, folder=True),
        _category("software.small-projects", "Small Projects", "Small, experimental, personal, or single-purpose projects.", parent="software", tags={"tag_general_source_repository"}, folder=True),
        _category("software.dependencies", "Dependency Sources", "Third-party source libraries and vendored dependencies.", parent="software", tags={"tag_general_dependency_source", "tag_general_vendored"}, folder=True),
        _category("software.portable", "Portable Applications", "Runnable portable application bundles and their associated state.", parent="software", tags={"tag_general_portable_app"}, folder=True),
        _category("software.installers", "Installers & Images", "Installers, packages, updates, archives, and disk images.", parent="software", tags={"tag_general_installer", "tag_general_downloaded"}, folder=True),
        _category("software.drivers", "Drivers & Firmware", "Device drivers, firmware, BIOS, and hardware support material.", parent="software", tags={"tag_general_driver_firmware"}, folder=True),
        _category("media", "Media", "Image, audio, and video collections."),
        _category("media.images", "Images", "Photographs, illustrations, screenshots, and graphics.", parent="media", tags={"tag_general_media"}, folder=True),
        _category("media.audio", "Audio", "Music, recordings, podcasts, and other audio.", parent="media", tags={"tag_general_media"}, folder=True),
        _category("media.video", "Video", "Films, episodes, recordings, and other video.", parent="media", tags={"tag_general_media"}, folder=True),
        _category("uncategorized", "Uncategorized", "Material whose semantic destination is not yet known.", tags={"tag_general_needs_review"}),
    ]
