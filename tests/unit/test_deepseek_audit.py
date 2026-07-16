from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from ai_organizer.adapters.providers import DeepSeekProvider, OpenRouterProvider
from ai_organizer.adapters.providers.deepseek_provider import (
    _parse_audit_proposals,
    _run_inventory_tool,
)
from ai_organizer.application.inventory_query import InventoryQueryService
from ai_organizer.application.update_research import PublicWebResearchClient


class FakeCompletions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if len(self.requests) == 1:
            function = SimpleNamespace(name="inventory_summary", arguments='{"glob":"**/*.docx"}')
            message = SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(id="call-1", function=function)],
            )
        else:
            message = SimpleNamespace(
                content=json.dumps(
                    {
                        "proposals": [
                            {
                                "target": "rename",
                                "pattern": "Repeated document convention",
                                "guidance": "Preserve the observed convention when evidence agrees.",
                                "evidence": "Two DOCX records",
                                "confidence": 0.8,
                            }
                        ]
                    }
                ),
                tool_calls=[],
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_deepseek_audit_uses_bounded_inventory_tools_before_proposing() -> None:
    completions = FakeCompletions()
    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    query = InventoryQueryService(
        [
            {
                "id": "doc",
                "root_id": "root",
                "relative_path": "document.docx",
                "extension": ".docx",
                "size": 10,
                "modified_ns": 1,
            }
        ]
    )

    proposals = provider.audit_inventory(query, {"root"})

    assert proposals[0]["target"] == "rename"
    assert len(completions.requests) == 2
    assert completions.requests[0]["tool_choice"] == "required"
    assert completions.requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    tool_names = {value["function"]["name"] for value in completions.requests[0]["tools"]}
    assert {
        "storage_list_volumes",
        "storage_list_directory",
        "inventory_list_file_issues",
        "submit_audit_proposals",
    }.issubset(tool_names)
    assert "version_regex" not in completions.requests[0]["messages"][1]["content"]
    assert any(message["role"] == "tool" for message in completions.requests[1]["messages"])


def test_deepseek_audit_recovers_from_invalid_then_fenced_json() -> None:
    class RecoveringCompletions:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def create(self, **request: Any) -> Any:
            self.requests.append(request)
            content = (
                "This is not JSON"
                if len(self.requests) == 1
                else '```json\n{"proposals":[{"target":"sources","guidance":"Use the data volume for archives","confidence":0.7}]}\n```'
            )
            message = SimpleNamespace(content=content, tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = RecoveringCompletions()
    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )

    proposals = provider.audit_inventory(InventoryQueryService([]), max_rounds=2)

    assert proposals[0]["target"] == "sources"
    assert len(completions.requests) == 2
    assert any(
        "corrected JSON only" in str(message.get("content", ""))
        for message in completions.requests[1]["messages"]
        if message.get("role") == "user"
    )


def test_deepseek_audit_accepts_strict_submission_tool() -> None:
    class SubmissionCompletions:
        def create(self, **request: Any) -> Any:
            function = SimpleNamespace(
                name="submit_audit_proposals",
                arguments=json.dumps(
                    {
                        "proposals": [
                            {
                                "target": "sources",
                                "pattern": "Uncovered data volume",
                                "guidance": "Review whether the data volume needs a source.",
                                "evidence": "Volume capacity and source coverage",
                                "confidence": 0.75,
                            }
                        ]
                    }
                ),
            )
            message = SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(id="submit", function=function)],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=SubmissionCompletions())
    )

    proposals = provider.audit_inventory(InventoryQueryService([]))

    assert proposals[0]["pattern"] == "Uncovered data volume"


def test_audit_parser_accepts_commentary_around_one_valid_json_object() -> None:
    result = _parse_audit_proposals(
        'Analysis complete. {"proposals":[{"target":"cleanup","guidance":"Review generated files","confidence":0.5}]} End.'
    )

    assert result[0]["target"] == "cleanup"


def test_audit_parser_accepts_only_in_scope_source_classification() -> None:
    result = _parse_audit_proposals(
        json.dumps(
            {
                "proposals": [
                    {
                        "proposal_type": "source_policy",
                        "root_id": "allowed-root",
                        "category_ids": ["personal"],
                        "tag_ids": ["financial"],
                        "roles": ["archive", "invalid-role"],
                        "pattern": "Long-lived statements",
                        "evidence": "PDF names and timestamps",
                        "confidence": 0.85,
                    },
                    {
                        "proposal_type": "source_policy",
                        "root_id": "outside-root",
                        "roles": ["inbox"],
                        "pattern": "Out of scope",
                        "evidence": "None",
                        "confidence": 1,
                    },
                ]
            }
        ),
        {"allowed-root"},
    )

    assert result == [
        {
            "proposal_type": "source_policy",
            "root_id": "allowed-root",
            "target": "sources",
            "category_ids": ["personal"],
            "tag_ids": ["financial"],
            "roles": ["archive"],
            "pattern": "Long-lived statements",
            "guidance": "",
            "evidence": "PDF names and timestamps",
            "confidence": 0.85,
        }
    ]


def test_inventory_folder_tool_is_unlimited_when_model_omits_depth() -> None:
    query = InventoryQueryService(
        [
            {
                "id": "deep-folder",
                "root_id": "root",
                "relative_path": "/".join(f"level-{value}" for value in range(1, 16)),
                "is_dir": True,
            }
        ]
    )

    result = _run_inventory_tool(query, "inventory_folder_tree", {}, {"root"})

    assert result["max_depth"] is None
    assert result["total"] == 1
    assert result["folders"][0]["depth"] == 15


class FakeFolderCompletions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if len(self.requests) == 1:
            function = SimpleNamespace(
                name="inventory_folder_tree",
                arguments='{"root_ids":["destination"],"max_depth":4}',
            )
            message = SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(id="folder-call", function=function)],
            )
        else:
            message = SimpleNamespace(
                content=json.dumps(
                    {
                        "proposals": [
                            {
                                "root_id": "destination",
                                "projected": "Clients/Acme",
                                "rationale": "Repeated client metadata",
                                "evidence": "DOCX and PDF counts",
                                "confidence": 0.82,
                            }
                        ]
                    }
                ),
                tool_calls=[],
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_deepseek_folder_plan_probes_hierarchy_and_returns_safe_paths() -> None:
    completions = FakeFolderCompletions()
    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    query = InventoryQueryService(
        [
            {
                "id": "clients",
                "root_id": "destination",
                "relative_path": "Clients",
                "is_dir": True,
            }
        ],
        [{"id": "destination", "name": "Destination", "roles": ["destination"]}],
    )

    proposals = provider.plan_folders(
        query, {"destination"}, {"destination"}, "Prefer client groupings"
    )

    assert proposals[0]["projected"] == "Clients/Acme"
    assert proposals[0]["confidence"] == 0.82
    assert completions.requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert any(message["role"] == "tool" for message in completions.requests[1]["messages"])


def test_openrouter_uses_shared_tool_contract_with_zero_data_retention() -> None:
    completions = FakeFolderCompletions()
    provider = object.__new__(OpenRouterProvider)
    provider.model = "openai/gpt-5.2"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    query = InventoryQueryService(
        [{"id": "clients", "root_id": "destination", "relative_path": "Clients", "is_dir": True}],
        [{"id": "destination", "name": "Destination", "roles": ["destination"]}],
    )

    proposals = provider.plan_folders(query, {"destination"}, {"destination"})

    assert proposals[0]["projected"] == "Clients/Acme"
    assert completions.requests[0]["extra_body"] == {"provider": {"zdr": True}}


def test_folder_plan_discards_proposals_beyond_root_depth_ceiling() -> None:
    completions = FakeFolderCompletions()
    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    query = InventoryQueryService(
        [{"id": "clients", "root_id": "destination", "relative_path": "Clients", "is_dir": True}],
        [{"id": "destination", "name": "Destination", "roles": ["destination"]}],
    )

    proposals = provider.plan_folders(
        query,
        {"destination"},
        {"destination"},
        max_depth_by_root={"destination": 1},
    )

    assert proposals == []


class FakeUpdateCompletions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if len(self.requests) == 1:
            function = SimpleNamespace(
                name="web_search", arguments='{"query":"Example official releases","limit":5}'
            )
            message = SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(id="web-call", function=function)],
            )
        else:
            message = SimpleNamespace(
                content=json.dumps(
                    {
                        "assessments": [
                            {
                                "entity_kind": "software",
                                "entity_key": "software_1",
                                "application_name": "Example",
                                "current_version": "1.0",
                                "latest_version": "2.0",
                                "update_available": True,
                                "latest_release_channel": "full_release",
                                "official_page_url": "https://example.com/releases",
                                "direct_download_url": None,
                                "preferred_url_kind": "web_page",
                                "result_status": "verified",
                                "confidence": 0.95,
                                "rationale": "Official release page",
                                "evidence": [],
                                "update_page_hint": {
                                    "url": "https://example.com/releases",
                                    "page_kind": "release_page",
                                    "version_locator": "Latest stable heading",
                                    # Mirrors a real DeepSeek response that used a
                                    # rejected nested quantifier. The provider must
                                    # convert it to the declarative local matcher.
                                    "version_regex": r"(2\.[0-9]+\.(?:[0-9]+)*)",
                                },
                            }
                        ]
                    }
                ),
                tool_calls=[],
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_deepseek_update_research_uses_web_tool_and_strict_assessment() -> None:
    completions = FakeUpdateCompletions()
    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    web = object.__new__(PublicWebResearchClient)
    web.search = lambda query, limit: {  # type: ignore[method-assign]
        "query": query,
        "limit": limit,
        "results": [{"url": "https://example.com/releases"}],
    }

    assessments = provider.research_updates(
        [
            {
                "entity_kind": "software",
                "entity_key": "software_1",
                "application_name": "Example",
                "current_version": "1.0",
            }
        ],
        web,
    )

    assert assessments[0].latest_version == "2.0"
    assert assessments[0].update_page_hint is not None
    assert assessments[0].update_page_hint.version_regex
    assert assessments[0].update_page_hint.version_prefix == "Example"
    assert assessments[0].update_page_hint.version_format == "dotted_numeric"
    assert completions.requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert any(message["role"] == "tool" for message in completions.requests[1]["messages"])


class RecoveringUpdateCompletions(FakeUpdateCompletions):
    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if len(self.requests) == 1:
            function = SimpleNamespace(
                name="web_fetch",
                arguments='{"url":"https://example.com/moved"}',
            )
            message = SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(id="failed-fetch", function=function)],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        # Reuse the valid final structured response from the base fake.
        self.requests.pop()
        return super().create(**request)


def test_deepseek_update_research_returns_fetch_failure_to_model() -> None:
    completions = RecoveringUpdateCompletions()
    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    web = object.__new__(PublicWebResearchClient)
    web.fetch = lambda _url: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("HTTP 404")
    )

    assessments = provider.research_updates(
        [
            {
                "entity_kind": "software",
                "entity_key": "software_1",
                "application_name": "Example",
                "current_version": "1.0",
            }
        ],
        web,
    )

    tool_result = next(
        message for message in completions.requests[1]["messages"] if message["role"] == "tool"
    )
    assert assessments[0].latest_version == "2.0"
    assert json.loads(tool_result["content"])["recoverable"] is True


class DiscoveryBudgetCompletions(FakeUpdateCompletions):
    def create(self, **request: Any) -> Any:
        tools = request.get("tools", [])
        if tools and tools[0]["function"]["name"] == "submit_update_assessments":
            self.requests.append(request)
            arguments = json.dumps(
                {
                    "assessments": [
                        {
                            "entity_kind": "software",
                            "entity_key": "software_1",
                            "application_name": "Example",
                            "current_version": "1.0",
                            "latest_version": "",
                            "update_available": False,
                            "latest_release_channel": "full_release",
                            "official_page_url": None,
                            "direct_download_url": None,
                            "preferred_url_kind": "none",
                            "result_status": "not_found",
                            "confidence": 0.2,
                            "rationale": "Discovery budget exhausted",
                            "evidence": [],
                        }
                    ]
                }
            )
            function = SimpleNamespace(name="submit_update_assessments", arguments=arguments)
            message = SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(id="submit", function=function)],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        self.requests.append(request)
        function = SimpleNamespace(
            name="web_search", arguments='{"query":"Example releases","limit":3}'
        )
        message = SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(id="keep-searching", function=function)],
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_deepseek_update_research_forces_final_result_after_discovery_budget() -> None:
    completions = DiscoveryBudgetCompletions()
    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    web = object.__new__(PublicWebResearchClient)
    web.search = lambda _query, _limit: {"results": []}  # type: ignore[method-assign]

    assessments = provider.research_updates(
        [
            {
                "entity_kind": "software",
                "entity_key": "software_1",
                "application_name": "Example",
                "current_version": "1.0",
            }
        ],
        web,
        max_rounds=1,
    )

    assert assessments[0].result_status == "not_found"
    assert completions.requests[-1]["tool_choice"] == "required"
    assert completions.requests[-1]["tools"][0]["function"]["name"] == ("submit_update_assessments")


def test_deepseek_update_research_accepts_single_assessment_envelope() -> None:
    completions = FakeUpdateCompletions()
    provider = object.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-flash"
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    original_create = completions.create

    def single_envelope(**request: Any) -> Any:
        response = original_create(**request)
        if not response.choices[0].message.tool_calls:
            payload = json.loads(response.choices[0].message.content)
            response.choices[0].message.content = json.dumps(
                {"assessment": payload["assessments"][0]}
            )
        return response

    completions.create = single_envelope  # type: ignore[method-assign]
    web = object.__new__(PublicWebResearchClient)
    web.search = lambda _query, _limit: {"results": []}  # type: ignore[method-assign]

    assessments = provider.research_updates(
        [
            {
                "entity_kind": "software",
                "entity_key": "software_1",
                "application_name": "Example",
                "current_version": "1.0",
            }
        ],
        web,
    )

    assert assessments[0].entity_key == "software_1"
