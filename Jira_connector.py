"""
title: MCP Jira Integration
author: Gil
version: 1.2.0
license: MIT
description: Connect to MCP Atlassian server through LiteLLM proxy with per-user PAT authentication via User Valves. Provides access to Jira tools for issue management, search, comments, sprints, and more.
"""

import requests
import json
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional


class Tools:
    """
    MCP Jira Integration Tool for OpenWebUI

    This tool connects to an MCP Atlassian server through a LiteLLM proxy,
    allowing each user to authenticate with their own Jira Personal Access Token (PAT).
    """

    class Valves(BaseModel):
        """Admin-level configuration (set by OpenWebUI administrators)"""

        MCP_SERVER_URL: str = Field(
            default="https://<LITELLM_APU_URL>/mcp/",
            description="The LiteLLM MCP proxy endpoint URL",
        )
        MCP_SERVER_GROUP: str = Field(
            default="<MCP_GROUP>",
            description="MCP server group identifier (sent in x-mcp-servers header)",
        )
        ENABLED_TOOLS: str = Field(
            default="",
            description="Comma-separated list of enabled tools (leave empty for all). Example: jira_search,jira_get_issue,jira_create_issue",
        )
        READ_ONLY_MODE: bool = Field(
            default=True,
            description="If enabled, only read operations are available (no create/update/delete)",
        )

    class UserValves(BaseModel):
        """User-level configuration (each user sets their own credentials)"""

        LITELLM_API_KEY: str = Field(
            default="",
            description="Your LiteLLM API key for proxy authentication.",
        )
        JIRA_PAT: str = Field(
            default="",
            description="Your Jira Personal Access Token (PAT) for authentication. Get this from your Jira profile settings.",
        )
        REQUEST_TIMEOUT: int = Field(
            default=120,
            description="Timeout in seconds for MCP requests (default: 120s). Increase for complex searches.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.user_valves = self.UserValves()
        self.protocol_version = "2024-11-05"
        self.client_name = "OpenWebUI-MCP-Jira"
        self.client_version = "1.2.0"

    def _get_user_valves(self, __user__: dict):
        """Extract UserValves from __user__, handling both dict and UserValves instances"""
        if not __user__:
            return self.user_valves

        valves = __user__.get("valves")
        if valves is None:
            return self.user_valves

        # If already a UserValves instance, return it directly
        if isinstance(valves, self.UserValves):
            return valves

        # If it's a dict, create UserValves from it
        if isinstance(valves, dict):
            return self.UserValves(**valves)

        # Fallback
        return self.user_valves

    def _get_headers(self, user_valves, session_id: str = None) -> Dict[str, str]:
        """Build request headers with proper authentication"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream;q=0.5",
            "User-Agent": f"{self.client_name}/{self.client_version}",
            "Mcp-Protocol-Version": self.protocol_version,
            "x-mcp-servers": self.valves.MCP_SERVER_GROUP,
        }

        if user_valves.LITELLM_API_KEY:
            headers["x-litellm-api-key"] = f"Bearer {user_valves.LITELLM_API_KEY}"

        if user_valves.JIRA_PAT:
            headers["x-mcp-jira-authorization"] = f"Token {user_valves.JIRA_PAT}"

        if session_id:
            headers["Mcp-Session-Id"] = session_id

        return headers

    def _json_rpc_payload(
        self, method: str, params: Optional[Dict] = None, request_id: int = 1
    ) -> Dict:
        """Build JSON-RPC 2.0 payload"""
        payload = {"jsonrpc": "2.0", "method": method, "id": request_id}
        if params is not None:
            payload["params"] = params
        return payload

    def _handshake(self, user_valves) -> str:
        """Perform MCP initialization handshake and return session_id"""
        init_payload = self._json_rpc_payload(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        )

        handshake_timeout = max(30, user_valves.REQUEST_TIMEOUT // 4)

        response = requests.post(
            self.valves.MCP_SERVER_URL,
            headers=self._get_headers(user_valves),
            json=init_payload,
            timeout=handshake_timeout,
        )

        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text}")

        session_id = response.headers.get("Mcp-Session-Id")
        data = response.json()

        if "error" in data:
            raise Exception(f"MCP Error: {data['error']}")

        # Send initialized notification
        notify_payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        requests.post(
            self.valves.MCP_SERVER_URL,
            headers=self._get_headers(user_valves, session_id),
            json=notify_payload,
            timeout=10,
        )

        return session_id

    def _is_write_tool(self, tool_name: str) -> bool:
        """Check if a tool performs write operations"""
        write_tools = {
            "jira_create_issue",
            "jira_update_issue",
            "jira_delete_issue",
            "jira_batch_create_issues",
            "jira_add_comment",
            "jira_transition_issue",
            "jira_add_worklog",
            "jira_link_to_epic",
            "jira_create_sprint",
            "jira_update_sprint",
            "jira_create_issue_link",
            "jira_remove_issue_link",
            "jira_create_version",
            "jira_batch_create_versions",
            "jira_create_remote_issue_link",
        }
        return tool_name in write_tools

    def _read_only_message(self, operation: str) -> str:
        """Return a user-friendly message when write operations are blocked"""
        return f"🔒 **Read-Only Mode Active**\n\nI cannot {operation} because the Jira integration is currently in read-only mode. This is a safety setting configured by your administrator.\n\nIf you need to perform this action, please:\n1. Contact your administrator to enable write access, or\n2. Perform this action directly in Jira"

    def _call_tool(self, tool_name: str, arguments: dict, user_valves) -> str:
        """Internal method to call any MCP tool"""

        if not user_valves.JIRA_PAT:
            return json.dumps(
                {
                    "error": "Jira PAT not configured",
                    "message": "Please configure your Jira Personal Access Token in User Settings",
                }
            )

        try:
            session_id = self._handshake(user_valves)

            call_payload = self._json_rpc_payload(
                "tools/call", {"name": tool_name, "arguments": arguments}, request_id=3
            )

            response = requests.post(
                self.valves.MCP_SERVER_URL,
                headers=self._get_headers(user_valves, session_id),
                json=call_payload,
                timeout=user_valves.REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                return json.dumps(
                    {"error": f"HTTP {response.status_code}: {response.text}"}
                )

            data = response.json()

            if "error" in data:
                err = data["error"]
                err_msg = (
                    err.get("message", str(err)) if isinstance(err, dict) else str(err)
                )
                return json.dumps({"error": err_msg})

            content = data.get("result", {}).get("content", [])
            text_results = []

            for item in content:
                if item.get("type") == "text":
                    text_results.append(item.get("text", ""))
                elif item.get("type") == "resource":
                    resource = item.get("resource", {})
                    text_results.append(f"[Resource: {resource.get('uri', 'unknown')}]")

            return "\n".join(text_results) if text_results else json.dumps(data)

        except requests.exceptions.Timeout:
            return json.dumps(
                {
                    "error": f"Request timeout after {user_valves.REQUEST_TIMEOUT}s - try increasing REQUEST_TIMEOUT in user settings"
                }
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ==================== DISCOVERY ====================

    async def discover_jira_tools(
        self,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Discover all available Jira tools from the MCP server.
        Call this first to see what operations are available.

        :return: JSON list of available tools with their descriptions and input schemas
        """
        user_valves = self._get_user_valves(__user__)

        if not user_valves.JIRA_PAT:
            return json.dumps(
                {
                    "error": "Jira PAT not configured",
                    "message": "Please configure your Jira Personal Access Token in User Settings",
                }
            )

        try:
            session_id = self._handshake(user_valves)

            list_payload = self._json_rpc_payload("tools/list", {}, request_id=2)

            response = requests.post(
                self.valves.MCP_SERVER_URL,
                headers=self._get_headers(user_valves, session_id),
                json=list_payload,
                timeout=60,
            )

            if response.status_code != 200:
                return json.dumps(
                    {"error": f"HTTP {response.status_code}: {response.text}"}
                )

            result = response.json()
            if "error" in result:
                return json.dumps({"error": result["error"]})

            tools = result.get("result", {}).get("tools", [])

            jira_tools = []
            enabled_list = [
                t.strip() for t in self.valves.ENABLED_TOOLS.split(",") if t.strip()
            ]

            for tool in tools:
                name = tool.get("name", "")

                if not name.startswith("jira_"):
                    continue

                if enabled_list and name not in enabled_list:
                    continue

                # Skip write tools in discovery if read-only mode
                if self.valves.READ_ONLY_MODE and self._is_write_tool(name):
                    continue

                jira_tools.append(
                    {
                        "name": name,
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("inputSchema", {}),
                    }
                )

            return json.dumps(jira_tools, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({"error": f"Discovery failed: {str(e)}"})

    # ==================== SEARCH & READ ====================

    async def jira_search(
        self,
        jql: str,
        max_results: int = 20,
        fields: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Search for Jira issues using JQL (Jira Query Language).

        :param jql: JQL query string (e.g., "project = MYPROJ AND status = Open", "assignee = currentUser()", "updated >= -7d")
        :param max_results: Maximum number of results to return (default: 20)
        :param fields: Comma-separated list of fields to return (leave empty for default fields)
        :return: JSON containing matching issues
        """
        user_valves = self._get_user_valves(__user__)

        arguments = {"jql": jql, "limit": max_results}
        if fields:
            arguments["fields"] = fields

        return self._call_tool("jira_search", arguments, user_valves)

    async def jira_get_issue(
        self,
        issue_key: str,
        expand: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get detailed information about a specific Jira issue.

        :param issue_key: The issue key (e.g., "PROJ-123", "DEV-456")
        :param expand: Optional comma-separated list of fields to expand (e.g., "changelog,transitions")
        :return: JSON containing issue details including summary, description, status, assignee, etc.
        """
        user_valves = self._get_user_valves(__user__)

        arguments = {"issue_key": issue_key}
        if expand:
            arguments["expand"] = expand

        return self._call_tool("jira_get_issue", arguments, user_valves)

    async def jira_get_all_projects(
        self,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get a list of all Jira projects the user has access to.

        :return: JSON list of projects with their keys, names, and other metadata
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool("jira_get_all_projects", {}, user_valves)

    async def jira_get_project_issues(
        self,
        project_key: str,
        max_results: int = 50,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get all issues from a specific project.

        :param project_key: The project key (e.g., "PROJ", "DEV")
        :param max_results: Maximum number of results to return (default: 50)
        :return: JSON list of issues in the project
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_get_project_issues",
            {"project_key": project_key, "limit": max_results},
            user_valves,
        )

    async def jira_get_transitions(
        self,
        issue_key: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get available status transitions for an issue.
        Use this to see what status changes are possible.

        :param issue_key: The issue key (e.g., "PROJ-123")
        :return: JSON list of available transitions
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_get_transitions", {"issue_key": issue_key}, user_valves
        )

    async def jira_get_agile_boards(
        self,
        project_key: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get all agile boards, optionally filtered by project.

        :param project_key: Optional project key to filter boards
        :return: JSON list of agile boards
        """
        user_valves = self._get_user_valves(__user__)

        arguments = {}
        if project_key:
            arguments["project_key"] = project_key

        return self._call_tool("jira_get_agile_boards", arguments, user_valves)

    async def jira_get_sprints_from_board(
        self,
        board_id: str,
        state: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get sprints from a specific agile board.

        :param board_id: The board ID (get this from jira_get_agile_boards)
        :param state: Optional filter by state: "active", "closed", or "future"
        :return: JSON list of sprints
        """
        user_valves = self._get_user_valves(__user__)

        arguments = {"board_id": board_id}
        if state:
            arguments["state"] = state

        return self._call_tool("jira_get_sprints_from_board", arguments, user_valves)

    async def jira_get_sprint_issues(
        self,
        sprint_id: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get all issues in a specific sprint.

        :param sprint_id: The sprint ID (get this from jira_get_sprints_from_board)
        :return: JSON list of issues in the sprint
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_get_sprint_issues", {"sprint_id": sprint_id}, user_valves
        )

    async def jira_get_worklog(
        self,
        issue_key: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get worklog entries for a specific issue.

        :param issue_key: The issue key (e.g., "PROJ-123")
        :return: JSON list of worklog entries with time spent and comments
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_get_worklog", {"issue_key": issue_key}, user_valves
        )

    async def jira_get_user_profile(
        self,
        user_identifier: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get profile information for a Jira user.

        :param user_identifier: User email, username, or account ID
        :return: JSON containing user profile information
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_get_user_profile", {"user_identifier": user_identifier}, user_valves
        )

    async def jira_search_fields(
        self,
        keyword: str = "",
        limit: int = 10,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Search for available Jira fields by keyword.
        Useful for finding custom field IDs.

        :param keyword: Search keyword (leave empty to list all fields)
        :param limit: Maximum number of results (default: 10)
        :return: JSON list of matching fields with their IDs and names
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_search_fields", {"keyword": keyword, "limit": limit}, user_valves
        )

    async def jira_get_project_versions(
        self,
        project_key: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get all versions/releases for a project.

        :param project_key: The project key (e.g., "PROJ")
        :return: JSON list of project versions
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_get_project_versions", {"project_key": project_key}, user_valves
        )

    async def jira_get_board_issues(
        self,
        board_id: str,
        jql: str = "",
        max_results: int = 50,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get issues from a specific agile board, optionally filtered by JQL.

        :param board_id: The board ID
        :param jql: Optional JQL to filter issues
        :param max_results: Maximum number of results (default: 50)
        :return: JSON list of issues on the board
        """
        user_valves = self._get_user_valves(__user__)

        arguments = {
            "board_id": board_id,
            "jql": jql or "order by rank",
            "limit": max_results,
        }

        return self._call_tool("jira_get_board_issues", arguments, user_valves)

    async def jira_get_link_types(
        self,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Get all available issue link types (e.g., "blocks", "is blocked by", "relates to").

        :return: JSON list of link types
        """
        user_valves = self._get_user_valves(__user__)
        return self._call_tool("jira_get_link_types", {}, user_valves)

    # ==================== WRITE OPERATIONS ====================

    async def jira_create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str = "",
        assignee: str = "",
        priority: str = "",
        labels: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Create a new Jira issue.

        :param project_key: The project key (e.g., "PROJ")
        :param summary: Issue title/summary
        :param issue_type: Issue type (e.g., "Task", "Bug", "Story", "Epic")
        :param description: Detailed description (supports Jira wiki markup)
        :param assignee: Username or email to assign the issue to
        :param priority: Priority level (e.g., "High", "Medium", "Low")
        :param labels: Comma-separated list of labels
        :return: JSON with the created issue key and URL
        """
        if self.valves.READ_ONLY_MODE:
            return self._read_only_message("create a new Jira issue")

        user_valves = self._get_user_valves(__user__)

        arguments = {
            "project_key": project_key,
            "summary": summary,
            "issue_type": issue_type,
        }
        if description:
            arguments["description"] = description
        if assignee:
            arguments["assignee"] = assignee
        if priority:
            arguments["priority"] = priority
        if labels:
            arguments["labels"] = labels

        return self._call_tool("jira_create_issue", arguments, user_valves)

    async def jira_update_issue(
        self,
        issue_key: str,
        summary: str = "",
        description: str = "",
        assignee: str = "",
        priority: str = "",
        labels: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Update an existing Jira issue. Only provide fields you want to change.

        :param issue_key: The issue key (e.g., "PROJ-123")
        :param summary: New summary/title
        :param description: New description
        :param assignee: New assignee username or email
        :param priority: New priority level
        :param labels: New labels (comma-separated, replaces existing)
        :return: Confirmation of update
        """
        if self.valves.READ_ONLY_MODE:
            return self._read_only_message(f"update issue {issue_key}")

        user_valves = self._get_user_valves(__user__)

        fields = {}
        if summary:
            fields["summary"] = summary
        if description:
            fields["description"] = description
        if assignee:
            fields["assignee"] = assignee
        if priority:
            fields["priority"] = {"name": priority}
        if labels:
            fields["labels"] = [l.strip() for l in labels.split(",")]

        return self._call_tool(
            "jira_update_issue", {"issue_key": issue_key, "fields": fields}, user_valves
        )

    async def jira_transition_issue(
        self,
        issue_key: str,
        transition_id: str,
        comment: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Transition an issue to a new status.
        Use jira_get_transitions first to see available transitions and their IDs.

        :param issue_key: The issue key (e.g., "PROJ-123")
        :param transition_id: The transition ID (get from jira_get_transitions)
        :param comment: Optional comment to add during transition
        :return: Confirmation of transition
        """
        if self.valves.READ_ONLY_MODE:
            return self._read_only_message(f"transition issue {issue_key}")

        user_valves = self._get_user_valves(__user__)

        arguments = {"issue_key": issue_key, "transition_id": transition_id}
        if comment:
            arguments["comment"] = comment

        return self._call_tool("jira_transition_issue", arguments, user_valves)

    async def jira_add_comment(
        self,
        issue_key: str,
        comment: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Add a comment to a Jira issue.

        :param issue_key: The issue key (e.g., "PROJ-123")
        :param comment: Comment text (supports Jira wiki markup)
        :return: Confirmation of comment addition
        """
        if self.valves.READ_ONLY_MODE:
            return self._read_only_message(f"add a comment to issue {issue_key}")

        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_add_comment",
            {"issue_key": issue_key, "comment": comment},
            user_valves,
        )

    async def jira_add_worklog(
        self,
        issue_key: str,
        time_spent: str,
        comment: str = "",
        started: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Add a worklog entry to track time spent on an issue.

        :param issue_key: The issue key (e.g., "PROJ-123")
        :param time_spent: Time spent in Jira format (e.g., "2h", "30m", "1d 4h")
        :param comment: Optional worklog description
        :param started: Optional start datetime in ISO format
        :return: Confirmation of worklog addition
        """
        if self.valves.READ_ONLY_MODE:
            return self._read_only_message(f"add a worklog to issue {issue_key}")

        user_valves = self._get_user_valves(__user__)

        arguments = {"issue_key": issue_key, "time_spent": time_spent}
        if comment:
            arguments["comment"] = comment
        if started:
            arguments["started"] = started

        return self._call_tool("jira_add_worklog", arguments, user_valves)

    async def jira_link_to_epic(
        self,
        issue_key: str,
        epic_key: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Link an issue to an epic.

        :param issue_key: The issue key to link (e.g., "PROJ-123")
        :param epic_key: The epic key to link to (e.g., "PROJ-100")
        :return: Confirmation of link creation
        """
        if self.valves.READ_ONLY_MODE:
            return self._read_only_message(f"link issue {issue_key} to epic {epic_key}")

        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_link_to_epic",
            {"issue_key": issue_key, "epic_key": epic_key},
            user_valves,
        )

    async def jira_create_issue_link(
        self,
        link_type: str,
        inward_issue_key: str,
        outward_issue_key: str,
        comment: str = "",
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Create a link between two issues.
        Use jira_get_link_types to see available link types.

        :param link_type: The link type name (e.g., "Blocks", "Relates", "Duplicate")
        :param inward_issue_key: The source issue key
        :param outward_issue_key: The target issue key
        :param comment: Optional comment for the link
        :return: Confirmation of link creation
        """
        if self.valves.READ_ONLY_MODE:
            return self._read_only_message(
                f"create a link between {inward_issue_key} and {outward_issue_key}"
            )

        user_valves = self._get_user_valves(__user__)

        arguments = {
            "link_type": link_type,
            "inward_issue_key": inward_issue_key,
            "outward_issue_key": outward_issue_key,
        }
        if comment:
            arguments["comment"] = comment

        return self._call_tool("jira_create_issue_link", arguments, user_valves)

    async def jira_delete_issue(
        self,
        issue_key: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Delete a Jira issue. This action cannot be undone!

        :param issue_key: The issue key to delete (e.g., "PROJ-123")
        :return: Confirmation of deletion
        """
        if self.valves.READ_ONLY_MODE:
            return self._read_only_message(f"delete issue {issue_key}")

        user_valves = self._get_user_valves(__user__)
        return self._call_tool(
            "jira_delete_issue", {"issue_key": issue_key}, user_valves
        )

    # ==================== GENERIC TOOL CALL ====================

    async def call_mcp_tool(
        self,
        tool_name: str,
        arguments: str,
        __event_emitter__=None,
        __user__: dict = None,
    ) -> str:
        """
        Call any MCP tool by name with custom arguments.
        Use discover_jira_tools first to see available tools and their schemas.

        :param tool_name: The exact tool name (e.g., "jira_batch_get_changelogs")
        :param arguments: JSON string of arguments matching the tool's input schema
        :return: Tool execution result
        """
        if self.valves.READ_ONLY_MODE and self._is_write_tool(tool_name):
            return self._read_only_message(f"execute '{tool_name}'")

        user_valves = self._get_user_valves(__user__)

        try:
            args_dict = (
                json.loads(arguments) if isinstance(arguments, str) else arguments
            )
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON arguments: {str(e)}"})

        return self._call_tool(tool_name, args_dict, user_valves)
